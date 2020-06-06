#!/usr/bin/env python
# -*- coding: utf-8 -*-

import errno
import logging
import pprint
import sys
import json
from typing import Any

import trio
from trio .abc import Stream

from urllib.parse import unquote

from .linereader import TerminatedFrameReceiver

class NotConnectedError(Exception):
    pass


class OutboundSessionHasGoneAway(Exception):
    pass


class ESLEvent(object):
    def __init__(self, data):
        self.headers = {}
        if isinstance(data, bytes):
            data = data.decode()
        self.parse_data(data)

    def parse_data(self, data):
        data = unquote(data)
        data = data.strip().splitlines()
        last_key = None
        value = ''
        for line in data:
            if ': ' in line:
                key, value = line.split(': ', 1)
                last_key = key
            else:
                key = last_key
                value += '\n' + line
            self.headers[key.strip()] = value.strip()


class AsyncResult:

    def __init__(self):
        self._event = trio.Event()
        self._value = None
        self._exception = None
    
    def set(self, value: Any) -> None:
        self._value = value
        self._event.set()

    async def _coro(self) -> None:
        await self._event.wait()

    def set_exception(self, exc: Exception) -> None:
        self._exception = exc

    def __await__(self):
        yield from self._coro().__await__()
        return self._value


class ESLProtocol(object):
    def __init__(self):
        self._run = True
        self._EOL = b'\n'
        self._commands_sent = []
        self._auth_request_event = trio.Event()
        # self._receive_events_greenlet = None
        # self._process_events_greenlet = None
        self.event_handlers = {}
        self._esl_send_ch, self._esl_recv_ch = trio.open_memory_channel(0)
        self._process_esl_event_queue = True
        self._lingering = False
        self.connected = False
        self.stream: Stream = None
        self.recv: TerminatedFrameReceiver = None

        self.nursery: trio.Nursery = None

    def start_event_handlers(self):
        self.nursery.start_soon(self.receive_events)
        self.nursery.start_soon(self.process_events)

    def register_handle(self, name, handler):
        if name not in self.event_handlers:
            self.event_handlers[name] = []
        if handler in self.event_handlers[name]:
            return
        self.event_handlers[name].append(handler)

    def unregister_handle(self, name, handler):
        if name not in self.event_handlers:
            raise ValueError('No handlers found for event: %s' % name)
        self.event_handlers[name].remove(handler)
        if not self.event_handlers[name]:
            del self.event_handlers[name]

    async def receive_events(self):
        buf = b''
        while self._run:
            try:
                data = await self.recv.receive()
            except Exception:
                self._run = False
                self.connected = False
                await self.stream.aclose()
                # logging.exception("Error reading from socket.")
                break

            if not data:
                if self.connected:
                    logging.error("Error receiving data, is FreeSWITCH running?")
                    self.connected = False
                    self._run = False
                break
            # Empty line
            if data == self._EOL:
                event = ESLEvent(buf)
                buf = b''
                await self.handle_event(event)
                continue
            buf += data

    async def handle_event(self, event):
        if 'Content-Type' not in event.headers:
            event.headers['Content-Type'] = 'text/event-text'

        if event.headers['Content-Type'] == 'auth/request':
            self._auth_request_event.set()
        elif event.headers['Content-Type'] == 'command/reply':
            async_response = self._commands_sent.pop(0)
            event.data = event.headers['Reply-Text']
            async_response.set(event)
        elif event.headers['Content-Type'] == 'api/response':
            length = int(event.headers['Content-Length'])
            data = await self.recv.receive_exactly(length)
            event.data = data
            async_response = self._commands_sent.pop(0)
            async_response.set(event)
        elif event.headers['Content-Type'] == 'text/disconnect-notice':
            if event.headers.get('Content-Disposition') == 'linger':
                logging.debug('Linger activated')
                self._lingering = True
            else:
                self.connected = False
            # disconnect-notice is now a propagated event both for inbound
            # and outbound socket modes.
            # This is useful for outbound mode to notify all remaining
            # waiting commands to stop blocking and send a NotConnectedError
            await self._esl_send_ch.send(event)
        elif event.headers['Content-Type'] == 'text/rude-rejection':
            self.connected = False
            length = int(event.headers['Content-Length'])
            await self.recv.receive_exactly(length)
            self._auth_request_event.set()
        else:
            length = int(event.headers.get('Content-Length', '0'))

            if length:
                data = await self.recv.receive_exactly(length)
                if event.headers.get('Content-Type') == 'log/data':
                    event.data = data
                elif event.headers.get('Content-Type') == 'text/event-json':
                    event.headers.update(json.loads(data)) 
                else:
                    event.parse_data(data)
            await self._esl_send_ch.send(event)

    async def process_one_event(self, handlers, event):

        if hasattr(self, 'before_handle'):
            await self._safe_exec_handler(self.before_handle, event)

        async with trio.open_nursery() as nursery:
            for h in handlers:
                nursery.start_soon(self._safe_exec_handler, h, event)

        if hasattr(self, 'after_handle'):
            await self._safe_exec_handler(self.after_handle, event)

    async def _safe_exec_handler(self, handler, event):
        try:
            await handler(event)
        except:
            logging.exception('ESL %s raised exception.' % handler.__name__)
            logging.error(pprint.pformat(event.headers))

    async def process_events(self):
        logging.debug('Event Processor Running')

        async for event in self._esl_recv_ch:
            if not self._run:
                break

            if event.headers.get('Event-Name') == 'CUSTOM':
                handlers = self.event_handlers.get(event.headers.get('Event-Subclass'))
            else:
                handlers = self.event_handlers.get(event.headers.get('Event-Name'))

            if event.headers.get('Content-Type') == 'text/disconnect-notice':
                handlers = self.event_handlers.get('DISCONNECT')

            if not handlers and event.headers.get('Content-Type') == 'log/data':
                handlers = self.event_handlers.get('log')

            if not handlers and '*' in self.event_handlers:
                handlers = self.event_handlers.get('*')

            if not handlers:
                continue

            self.nursery.start_soon(self.process_one_event, handlers, event)

    async def send(self, data):
        EOL = '\n'
        if not self.connected:
            raise NotConnectedError()
        async_response = AsyncResult()
        self._commands_sent.append(async_response)
        raw_msg = (data + EOL * 2).encode('utf-8')
        await self.stream.send_all(raw_msg)
        return async_response

    async def stop(self):
        if self.connected:
            try:
                await self.send('exit')
            except (NotConnectedError,):
                pass
        self._run = False
        logging.info("Waiting for receive greenlet exit")
        self._receive_events_greenlet.join()
        logging.info("Waiting for event processing greenlet exit")
        self._process_events_greenlet.join()
        self.sock.close()
        self.sock_file.close()


class InboundESL(ESLProtocol):
    def __init__(self, host, port, password):
        super(InboundESL, self).__init__()
        self.host = host
        self.port = port
        self.password = password
        self.timeout = 5
        self.connected = False

    async def connect(self):

        self.token = trio.lowlevel.current_trio_token()
        self.stream = await trio.open_tcp_stream(self.host, self.port)
        self.recv = TerminatedFrameReceiver(stream=self.stream)
        self.connected = True

        async with trio.open_nursery() as self.nursery:
            self.start_event_handlers()
            await self._auth_request_event.wait()
            if not self.connected:
                raise NotConnectedError('Server closed connection, check '
                                        'FreeSWITCH config.')
            await self.authenticate()
            print("Ready to process events...")

    async def authenticate(self):
        ret = await self.send('auth %s' % self.password)
        response = await ret
        if response.headers['Reply-Text'] != '+OK accepted':
            raise ValueError('Invalid password.')

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()



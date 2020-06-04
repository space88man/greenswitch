#!/usr/bin/env python
# -*- coding: utf-8 -*-

import errno
import logging
import pprint
import sys
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
    
    def set(self, value: Any) -> None:
        self._value = value
        self._event.set()

    async def wait(self) -> Any:
        await self._event.wait()
        return self._value

    def set_exception(self, exc: Exception) -> None:
        self._exception = exc

    async def get(self, block=True, timeout=-1) -> Any:
        pass


class ESLProtocol(object):
    def __init__(self):
        self._run = True
        self._EOL = '\n'
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
        #self._receive_events_greenlet = gevent.spawn(self.receive_events)
        #self._process_events_greenlet = gevent.spawn(self.process_events)
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
                data = self.recv.receive()
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
                self.nursery.start_soon(self.handle_event, event)
                continue
            buf += data

    async def handle_event(self, event):
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
            await self._esl_event_send_ch.send(event)
        elif event.headers['Content-Type'] == 'text/rude-rejection':
            self.connected = False
            length = int(event.headers['Content-Length'])
            await self.recv.receive_exactly(length)
            self._auth_request_event.set()
        else:
            length = int(event.headers['Content-Length'])
            data = await self.recv.receive_exactly(length)
            if event.headers.get('Content-Type') == 'log/data':
                event.data = data
            else:
                event.parse_data(data)
            await self._esl_event_send_ch.send(event)

    async def _safe_exec_handler(self, handler, event):
        try:
            await handler(event)
        except:
            logging.exception('ESL %s raised exception.' % handler.__name__)
            logging.error(pprint.pformat(event.headers))

    async def process_events(self):
        logging.debug('Event Processor Running')

        async for event in self._esl.recv_ch:
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
        if not self.connected:
            raise NotConnectedError()
        async_response = AsyncResult()
        self._commands_sent.append(async_response)
        raw_msg = (data + self._EOL*2).encode('utf-8')
        await self.stream.send(raw_msg)
        return async_response

    async def stop(self):
        if self.connected:
            try:
                await self.send('exit')
            except (NotConnectedError, socket.error):
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

        self.stream = await trio.open_tcp_stream(self.host, self.port)
        self.recv = TerminatedFrameReceiver(stream = self.stream)
        self.connected = True

        with trio.open_nursery() as self.nursery:
            self.start_event_handlers()
            await self._auth_request_event.wait()
            if not self.connected:
                raise NotConnectedError('Server closed connection, check '
                                    'FreeSWITCH config.')
            await self.authenticate()

    async def authenticate(self):
        ret = await self.send('auth %s' % self.password)
        response = await ret.wait()
        if response.headers['Reply-Text'] != '+OK accepted':
            raise ValueError('Invalid password.')

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

class OutboundSession(ESLProtocol):
    def __init__(self, client_address, sock):
        super(OutboundSession, self).__init__()
        self.sock = sock
        self.sock_file = self.sock.makefile()
        self.connected = True
        self.session_data = None
        self.start_event_handlers()
        self.register_handle('*', self.on_event)
        self.register_handle('CHANNEL_HANGUP', self.on_hangup)
        self.register_handle('DISCONNECT', self.on_disconnect)
        self.expected_events = {}
        self._outbound_connected = False

    @property
    def uuid(self):
        return self.session_data.get('variable_uuid')

    @property
    def call_uuid(self):
        return self.session_data.get('variable_call_uuid')

    @property
    def caller_id_number(self):
        return self.session_data.get('Caller-Caller-ID-Number')

    def on_disconnect(self, event):
        if self._lingering:
            logging.debug('Socket lingering..')
        elif not self.connected:
            logging.debug('Socket closed: %s' % event.headers)
        logging.debug('Raising OutboundSessionHasGoneAway for all pending'
                      'results')
        for event_name in self.expected_events:
            for variable, value, async_result in \
                    self.expected_events[event_name]:
                async_result.set_exception(OutboundSessionHasGoneAway())

        for cmd in self._commands_sent:
            cmd.set_exception(OutboundSessionHasGoneAway())

    def on_hangup(self, event):
        logging.info('Caller %s has gone away.' % self.caller_id_number)

    def on_event(self, event):
        # FIXME(italo): Decide if we really need a list of expected events
        # for each expected event. Since we're interacting with the call from
        # just one greenlet we don't have more than one item on this list.
        event_name = event.headers.get('Event-Name')
        if event_name not in self.expected_events:
            return

        for expected_event in self.expected_events[event_name]:
            event_variable, expected_value, async_response = expected_event
            expected_variable = 'variable_%s' % event_variable
            if expected_variable not in event.headers:
                return
            elif expected_value == event.headers.get(expected_variable):
                async_response.set(event)
                self.expected_events[event_name].remove(expected_event)

    async def call_command(self, app_name, app_args=None):
        """Wraps app_name and app_args into FreeSWITCH Outbound protocol:
        Example:
                sendmsg
                call-command: execute
                execute-app-name: answer\n\n

        """
        # We're not allowed to send more commands.
        # lingering True means we already received a hangup from the caller
        # and any commands sent at this time to the session will fail
        if self._lingering:
            raise OutboundSessionHasGoneAway()

        command = "sendmsg\n" \
                  "call-command: execute\n" \
                  "execute-app-name: %s" % app_name
        if app_args:
            command += "\nexecute-app-arg: %s" % app_args

        return await self.send(command)

    async def connect(self):
        if self._outbound_connected:
            return self.session_data

        ret = await self.send('connect')
        resp = await ret.wait()
        self.session_data = resp.headers
        self._outbound_connected = True

    async def myevents(self):
        await self.send('myevents')

    async def answer(self):
        ret = await self.call_command('answer')
        resp = await ret.wait()
        return resp.data

    async def park(self):
        await self.call_command('park')

    async def linger(self):
        await self.send('linger')

    async def playback(self, path, block=True):
        if not block:
            await self.call_command('playback', path)
            return

        async_response = AsyncResult()
        expected_event = "CHANNEL_EXECUTE_COMPLETE"
        expected_variable = "current_application"
        expected_variable_value = "playback"
        self.register_expected_event(expected_event, expected_variable,
                                     expected_variable_value, async_response)
        await self.call_command('playback', path)
        event = await async_response.wait()
        # TODO(italo): Decide what we need to return.
        #   Returning whole event right now
        return event

    async def play_and_get_digits(self, min_digits=None, max_digits=None,
                            max_attempts=None, timeout=None, terminators=None,
                            prompt_file=None, error_file=None, variable=None,
                            digits_regex=None, digit_timeout=None,
                            transfer_on_fail=None, block=True,
                            response_timeout=30):
        args = "%s %s %s %s %s %s %s %s %s %s %s" % (min_digits, max_digits,
                                                     max_attempts, timeout,
                                                     terminators, prompt_file,
                                                     error_file, variable,
                                                     digits_regex,
                                                     digit_timeout,
                                                     transfer_on_fail)
        if not block:
            self.call_command('play_and_get_digits', args)
            return

        async_response = AsyncResult()
        expected_event = "CHANNEL_EXECUTE_COMPLETE"
        expected_variable = "current_application"
        expected_variable_value = "play_and_get_digits"
        self.register_expected_event(expected_event, expected_variable,
                                     expected_variable_value, async_response)
        self.call_command('play_and_get_digits', args)
        event = await async_response.wait()
        if not event:
            return
        digit = event.headers.get('variable_%s' % variable)
        return digit

    async def say(self, module_name='en', lang=None, say_type='NUMBER',
            say_method='pronounced', gender='FEMININE', text=None, block=True,
            response_timeout=30):
        if lang:
            module_name += ':%s' % lang

        args = "%s %s %s %s %s" % (module_name, say_type, say_method, gender,
                                   text)
        if not block:
            self.call_command('say', args)
            return

        async_response = AsyncResult()
        expected_event = "CHANNEL_EXECUTE_COMPLETE"
        expected_variable = "current_application"
        expected_variable_value = "say"
        self.register_expected_event(expected_event, expected_variable,
                                     expected_variable_value, async_response)
        self.call_command('say', args)
        #event = await async_response.get(block=True, timeout=response_timeout)
        event = await async_response.wait()
        return event

    def register_expected_event(self, expected_event, expected_variable,
                                expected_value, async_response):
        if expected_event not in self.expected_events:
            self.expected_events[expected_event] = []
        self.expected_events[expected_event].append((expected_variable,
                                                    expected_value,
                                                    async_response))

    async def hangup(self, cause='NORMAL_CLEARING'):
        await self.call_command('hangup', cause)

    async def uuid_break(self):
        # TODO(italo): Properly detect when send() method should fail or not.
        # Not sure if this is the best way to avoid sending
        # session related commands, but for now it's working.
        # Another idea is to create a property called _socket_mode where the
        # values can be inbound or outbound and when running in outbound
        # mode we can make sure we'll only send a few permitted commands when
        # lingering is activated.
        if self._lingering:
            raise OutboundSessionHasGoneAway
        await self.send('api uuid_break %s' % self.uuid)


class OutboundESLServer(object):
    def __init__(self, bind_address='127.0.0.1', bind_port=8000,
                 application=None, max_connections=100):
        self.bind_address = bind_address
        if not isinstance(bind_port, (list, tuple)):
            bind_port = [bind_port]
        if not bind_port:
            raise ValueError('bind_port must be a string or list with port '
                             'numbers')

        self.bind_port = bind_port
        self.max_connections = max_connections
        self.connection_count = 0
        if not application:
            raise ValueError('You need an Application to control your calls.')
        self.application = application
        self._greenlets = set()
        self._running = False
        self.server = None
        logging.info('Starting OutboundESLServer at %s:%s' %
                     (self.bind_address, self.bind_port))
        self.bound_port = None

    def listen(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        for port in self.bind_port:
            try:
                self.server.bind((self.bind_address, port))
                self.bound_port = port
                break
            except socket.error:
                logging.info('Failed to bind to port %s, '
                             'trying next in range...' % port)
                continue
        if not self.bound_port:
            logging.error('Could not bind server, no ports available.')
            sys.exit()
        logging.info('Successfully bound to port %s' % self.bound_port)
        self.server.setblocking(0)
        self.server.listen(100)
        self._running = True

        while self._running:
            try:
                sock, client_address = self.server.accept()
            except socket.error as error:
                if error.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    # no data available
                    gevent.sleep(0.1)
                    continue
                raise

            session = OutboundSession(client_address, sock)
            gevent.spawn(self._accept_call, session)

        logging.info('Closing socket connection...')
        self.server.shutdown(socket.SHUT_RD)
        self.server.close()

        logging.info('Waiting for calls to be ended. Currently, there are '
                     '%s active calls' % self.connection_count)
        gevent.joinall(self._greenlets)
        self._greenlets.clear()

        logging.info('OutboundESLServer stopped')

    def _accept_call(self, session):
        if self.connection_count >= self.max_connections:
            logging.info(
                'Rejecting call, server is at full capacity, current '
                'connection count is %s/%s' %
                (self.connection_count, self.max_connections))
            session.connect()
            session.stop()
            return

        self._handle_call(session)

    def _handle_call(self, session):
        session.connect()
        app = self.application(session)
        handler = gevent.spawn(app.run)
        self._greenlets.add(handler)
        handler.session = session
        handler.link(self._handle_call_finish)
        self.connection_count += 1
        logging.debug('Connection count %d' % self.connection_count)

    def _handle_call_finish(self, handler):
        logging.info('Call from %s ended' % handler.session.caller_id_number)
        self._greenlets.remove(handler)
        self.connection_count -= 1
        logging.debug('Connection count %d' % self.connection_count)
        handler.session.stop()

    def stop(self):
        self._running = False


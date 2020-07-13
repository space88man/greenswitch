#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import pprint
import json
from typing import Any
import contextvars


from anyio import Event, connect_tcp, run, create_task_group, create_queue, create_event
from anyio.abc import SocketStream, TaskGroup

from urllib.parse import unquote


LOG = logging.getLogger(__name__)

session_id = contextvars.ContextVar("session_id", default=42)


class NotConnectedError(Exception):
    pass


class ESLEvent(object):
    def __init__(self, data):
        self.headers = {}
        self.parse_data(data)

    def parse_data(self, data):
        if not isinstance(data, str):
            data = data.decode()
        data = unquote(data)
        data = data.strip().splitlines()
        last_key = None
        value = ""
        for line in data:
            if ": " in line:
                key, value = line.split(": ", 1)
                last_key = key
            else:
                key = last_key
                value += "\n" + line
            self.headers[key.strip()] = value.strip()


class AsyncResult:
    """Awaitable, to hold the result of an RPC call.
    The result is ready when the _event member
    has been set.
    """

    def __init__(self):
        self._event: Event = create_event()
        self._value: Any = None
        self._exception: Exception = None

    async def set(self, value: Any) -> None:
        self._value = value
        await self._event.set()

    async def _coro(self) -> None:
        await self._event.wait()

    def set_exception(self, exc: Exception) -> None:
        self._exception = exc

    def __await__(self) -> Any:
        yield from self._coro().__await__()
        if self._exception:
            raise self._exception
        return self._value


class ESLProtocol(object):
    def __init__(self):
        self._run = True
        self._EOL = b"\n"
        self._commands_sent = []
        self._auth_request_event = None
        self.event_handlers = {}
        self._esl_event_queue = None
        self._lingering = False
        self.connected = False
        self.client: SocketStream = None

        self.tg: TaskGroup = None

    def register_handle(self, name, handler):
        if name not in self.event_handlers:
            self.event_handlers[name] = []
        if handler in self.event_handlers[name]:
            return
        self.event_handlers[name].append(handler)

    def unregister_handle(self, name, handler):
        if name not in self.event_handlers:
            raise ValueError("No handlers found for event: %s" % name)
        self.event_handlers[name].remove(handler)
        if not self.event_handlers[name]:
            del self.event_handlers[name]

    async def receive_events(self):
        LOG.debug("Network task running")

        buf = b""
        while self._run:
            try:
                # anyio does not include delimiter in data
                data = await self.client.receive_until(self._EOL, 16384)
            except Exception:
                self._run = False
                self.connected = False
                await self.client.close()
                LOG.exception("Exception in receive_events()")
                break

            # Empty line
            if data == b"":
                event = ESLEvent(buf)
                await self.handle_event(event)
                buf = b""
                continue
            buf += data + self._EOL  # anyio does not include delimiter

    async def handle_event(self, event):
        if "Event-Name" in event.headers and "Content-Type" not in event.headers:
            event.headers["Content-Type"] = "text/event-plain"
            event.headers["Content-Length"] = "0"
        elif "Content-Type" not in event.headers:
            event.headers["Content-Type"] = "text/unknown"
            event.headers["Content-Length"] = "0"

        if event.headers["Content-Type"] == "auth/request":
            LOG.info("Authentication request received from FreeSWITCH")
            await self._auth_request_event.set()
        elif event.headers["Content-Type"] == "command/reply":
            async_response = self._commands_sent.pop(0)
            event.data = event.headers["Reply-Text"]
            await async_response.set(event)
        elif event.headers["Content-Type"] == "api/response":
            length = int(event.headers["Content-Length"])
            data = await self.client.receive_exactly(length)
            event.data = data
            async_response = self._commands_sent.pop(0)
            await async_response.set(event)
        elif event.headers["Content-Type"] == "text/disconnect-notice":
            if event.headers.get("Content-Disposition") == "linger":
                LOG.debug("Linger activated")
                self._lingering = True
            else:
                self.connected = False
            # disconnect-notice is now a propagated event both for inbound
            # and outbound socket modes.
            # This is useful for outbound mode to notify all remaining
            # waiting commands to stop blocking and send a NotConnectedError
            await self._esl_event_queue.put(event)
        elif event.headers["Content-Type"] == "text/rude-rejection":
            self.connected = False
            length = int(event.headers["Content-Length"])
            await self.client.receive_exactly(length)
            await self._auth_request_event.set()
        else:
            length = int(event.headers.get("Content-Length", "0"))

            if length:
                data = await self.client.receive_exactly(length)
                if event.headers.get("Content-Type") == "log/data":
                    event.data = data
                elif event.headers.get("Content-Type") == "text/event-json":
                    event.headers.update(json.loads(data))
                else:
                    event.parse_data(data)
            await self._esl_event_queue.put(event)

    async def process_one_event(self, handlers, event):
        if hasattr(self, "before_handle"):
            await self._safe_exec_handler(self.before_handle, event)

        async with create_task_group() as tg:
            for h in handlers:
                await tg.spawn(self._safe_exec_handler, h, event)

        if hasattr(self, "after_handle"):
            await self._safe_exec_handler(self.after_handle, event)

    async def _safe_exec_handler(self, handler, event):
        try:
            await handler(event)
        except:
            LOG.exception("ESL %s raised exception." % handler.__name__)
            LOG.error(pprint.pformat(event.headers))

    async def process_events(self):
        LOG.debug("Event processor running")

        async for event in self._esl_event_queue:

            if not self._run:
                break

            if event.headers.get("Event-Name") == "CUSTOM":
                handlers = self.event_handlers.get(event.headers.get("Event-Subclass"))
            else:
                handlers = self.event_handlers.get(event.headers.get("Event-Name"))

            if event.headers.get("Content-Type") == "text/disconnect-notice":
                handlers = self.event_handlers.get("DISCONNECT")

            if not handlers and event.headers.get("Content-Type") == "log/data":
                handlers = self.event_handlers.get("log")

            if not handlers and "*" in self.event_handlers:
                handlers = self.event_handlers.get("*")

            if not handlers:
                continue

            await self.tg.spawn(self.process_one_event, handlers, event)

    async def send(self, data):
        EOL = "\n"
        if not self.connected:
            raise NotConnectedError()
        async_response = AsyncResult()
        self._commands_sent.append(async_response)
        raw_msg = (data + EOL * 2).encode("utf-8")
        await self.client.send_all(raw_msg)
        return async_response

    async def stop(self):
        if self.connected:
            try:
                await (await self.send("exit"))
            except (NotConnectedError,):
                pass
            finally:
                self.connected = False
        self._run = False
        await self.tg.cancel_scope.cancel()


class InboundESL(ESLProtocol):
    def __init__(self, host, port, password):
        super(InboundESL, self).__init__()
        self.host = host
        self.port = port
        self.password = password
        self.timeout = 5
        self.connected = False

    async def run_inbound(self):

        # LOG.info("Starting InboundESL with %s", self.stream.socket.getpeername())
        # order of context managers is important here, as we must block in the
        # task group before the socket cleanup
        self._esl_event_queue = create_queue(0)
        self._auth_request_event = create_event()
        async with await connect_tcp(self.host, self.port) as self.client, create_task_group() as self.tg:

            self.connected = True
            await self.tg.spawn(self.receive_events)
            await self.tg.spawn(self.process_events)
            await self._auth_request_event.wait()
            await self.authenticate()
            LOG.info("Ready to process events...")

    async def authenticate(self):
        ret = await self.send("auth %s" % self.password)
        response = await ret
        if response.headers["Reply-Text"] != "+OK accepted":
            raise ValueError("Invalid password.")

        LOG.info("Authentication to FreeSWITCH succeeded")


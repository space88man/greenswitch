import logging
from anyio import create_task_group, create_tcp_server, create_queue
from anyio import Stream
from .esl import ESLProtocol, AsyncResult, session_id


LOG = logging.getLogger(__name__)


class OutboundSessionHasGoneAway(Exception):
    pass


class OutboundSession(ESLProtocol):
    def __init__(self, stream: Stream):
        super().__init__()
        self.client: Stream = stream
        self.connected = True
        self.session_data = None
        self.register_handle("*", self.on_event)
        self.register_handle("CHANNEL_HANGUP", self.on_hangup)
        self.register_handle("DISCONNECT", self.on_disconnect)
        self.expected_events = {}
        self._outbound_connected = False

    async def run_outbound(self, *args):
        """Creates the task group/nursery for each incoming connection.

        It starts:
        * event handling tasks
        * miscellaneous tasks, such as application run task
        """
        LOG.info(
            "Starting OutboundSession:%d tasks with %s",
            session_id.get(),
            self.client.peer_address,
        )

        self._esl_event_queue = create_queue(0)

        async with create_task_group() as self.tg:
            await self.tg.spawn(self.receive_events)
            await self.tg.spawn(self.process_events)
            LOG.info("Ready to process events...")
            for t in args:
                await self.tg.spawn(t)

        # async with trio.open_nursery() as self.nursery:
        #    self.start_event_handlers()
        # for t in args:
        #        self.nursery.start_soon(t)
        LOG.warning("Session %d is terminating", session_id.get())

    @property
    def uuid(self):
        return self.session_data.get("variable_uuid")

    @property
    def call_uuid(self):
        return self.session_data.get("variable_call_uuid")

    @property
    def caller_id_number(self):
        return self.session_data.get("Caller-Caller-ID-Number")

    async def on_disconnect(self, event):
        if self._lingering:
            LOG.debug("Socket lingering..")
        elif not self.connected:
            LOG.debug("Socket closed: %s" % event.headers)
        LOG.debug(
            "Raising OutboundSessionHasGoneAway for all pending" " results: %d",
            len(self.expected_events),
        )
        for event_name in self.expected_events:
            LOG.debug(
                "No. of events: %s %d",
                event_name,
                len(self.expected_events[event_name]),
            )
            for variable, value, async_result in self.expected_events[event_name]:
                async_result.set_exception(OutboundSessionHasGoneAway())

        LOG.debug(
            "Raising OutboundSessionHasGoneAway for pending commands: %d",
            len(self._commands_sent),
        )
        for cmd in self._commands_sent:
            cmd.set_exception(OutboundSessionHasGoneAway())

    async def on_hangup(self, event):
        LOG.info("Caller %s has gone away." % self.caller_id_number)

    async def on_event(self, event):
        # FIXME(italo): Decide if we really need a list of expected events
        # for each expected event. Since we're interacting with the call from
        # just one greenlet we don't have more than one item on this list.
        event_name = event.headers.get("Event-Name")
        if event_name not in self.expected_events:
            return

        for expected_event in self.expected_events[event_name]:
            event_variable, expected_value, async_response = expected_event
            # If event_variable is YYYYYYYY match on
            #     YYYYYYYY: xxxxxxxx or
            #     variable_YYYYYYYY: xxxxxxxx
            expected_variable = "variable_%s" % event_variable
            if (
                event_variable not in event.headers
                and expected_variable not in event.headers
            ):
                return
            elif expected_value == event.headers.get(
                event_variable, event.headers.get(expected_variable)
            ):
                await async_response.set(event)
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

        command = (
            "sendmsg\n" "call-command: execute\n" "execute-app-name: %s" % app_name
        )
        if app_args:
            command += "\nexecute-app-arg: %s" % app_args

        return await self.send(command)

    async def connect(self):
        """FreeSWITCH will reply with a CHANNEL_DATA event
        which contains information about the incoming call.
        """
        if self._outbound_connected:
            return self.session_data

        ret = await self.send("connect")
        resp = await ret  #  expect Event-Name: CHANNEL_DATA
        self.session_data = resp.headers
        self._outbound_connected = True

        LOG.debug(
            "Incoming call from %s to %s",
            resp.headers["Channel-Caller-ID-Name"],
            resp.headers["Channel-Destination-Number"],
        )

    async def myevents(self):
        await self.send("myevents")

    async def answer(self):
        ret = await self.call_command("answer")
        resp = await ret
        return resp.data

    async def park(self):
        await self.call_command("park")

    async def linger(self):
        await self.send("linger")

    async def playback(self, path, block=True):
        if not block:
            await self.call_command("playback", path)
            return

        # FreeSWITCH event contains both:
        #
        # variable_current_application: playback
        # Application: playback
        async_response = AsyncResult()
        expected_event = "CHANNEL_EXECUTE_COMPLETE"
        # match on Application: or variable_current_application:
        expected_variable = "Application"
        expected_variable_value = "playback"
        self.register_expected_event(
            expected_event, expected_variable, expected_variable_value, async_response
        )
        await self.call_command("playback", path)
        event = await async_response
        # TODO(italo): Decide what we need to return.
        #   Returning whole event right now
        return event

    async def play_and_get_digits(
        self,
        min_digits=None,
        max_digits=None,
        max_attempts=None,
        timeout=None,
        terminators=None,
        prompt_file=None,
        error_file=None,
        variable=None,
        digits_regex=None,
        digit_timeout=None,
        transfer_on_fail=None,
        block=True,
        response_timeout=30,
    ):
        args = "%s %s %s %s %s %s %s %s %s %s %s" % (
            min_digits,
            max_digits,
            max_attempts,
            timeout,
            terminators,
            prompt_file,
            error_file,
            variable,
            digits_regex,
            digit_timeout,
            transfer_on_fail,
        )
        if not block:
            await self.call_command("play_and_get_digits", args)
            return

        async_response = AsyncResult()
        expected_event = "CHANNEL_EXECUTE_COMPLETE"
        expected_variable = "current_application"
        expected_variable_value = "play_and_get_digits"
        self.register_expected_event(
            expected_event, expected_variable, expected_variable_value, async_response
        )
        await self.call_command("play_and_get_digits", args)
        event = await async_response
        if not event:
            return
        digit = event.headers.get("variable_%s" % variable)
        return digit

    async def say(
        self,
        module_name="en",
        lang=None,
        say_type="NUMBER",
        say_method="pronounced",
        gender="FEMININE",
        text=None,
        block=True,
        response_timeout=30,
    ):
        if lang:
            module_name += ":%s" % lang

        args = "%s %s %s %s %s" % (module_name, say_type, say_method, gender, text)
        if not block:
            await self.call_command("say", args)
            return

        async_response = AsyncResult()
        expected_event = "CHANNEL_EXECUTE_COMPLETE"
        expected_variable = "current_application"
        expected_variable_value = "say"
        self.register_expected_event(
            expected_event, expected_variable, expected_variable_value, async_response
        )
        await self.call_command("say", args)
        # event = await async_response.get(block=True, timeout=response_timeout)
        event = await async_response
        return event

    def register_expected_event(
        self, expected_event, expected_variable, expected_value, async_response
    ):
        if expected_event not in self.expected_events:
            self.expected_events[expected_event] = []
        self.expected_events[expected_event].append(
            (expected_variable, expected_value, async_response)
        )

    async def hangup(self, cause="NORMAL_CLEARING"):
        await self.call_command("hangup", cause)

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
        await self.send("api uuid_break %s" % self.uuid)


class OutboundESLServer(object):
    def __init__(
        self,
        bind_address="127.0.0.1",
        bind_port=8000,
        application=None,
        max_connections=100,
    ):
        self.bind_address = bind_address
        self.bind_port = bind_port

        self.max_connections = max_connections
        self.connection_count = 0
        if not application:
            raise ValueError("You need an Application to control your calls.")
        self.application = application

        self._running = False
        self.server = None
        self.bound_port = None
        self.idx = 0

    async def handler(self, stream: Stream) -> None:
        """This method handles each incoming connection.

        It launches the event session and user application and blocks
        until those terminate.
        """
        LOG.debug("Loading session %d %s", self.idx, stream.peer_address)
        session_id.set(self.idx)
        self.idx += 1
        session = OutboundSession(stream)
        app = self.application(session)
        await session.run_outbound(app.run)

    async def listen(self):
        """Creates the root task group.
        It blocks until the task group is cancelled.
        """

        LOG.info(
            "Starting OutboundESLServer at %s:%s" % (self.bind_address, self.bind_port)
        )

        async with create_task_group() as tg, await create_tcp_server(
            self.bind_port
        ) as server:
            async for client in server.accept_connections():
                await tg.spawn(self.handler, client)

        # async with trio.open_nursery() as nursery:
        #    self.listeners = await nursery.start(trio.serve_tcp, self.handler, self.bind_port)
        #    LOG.info('OutboundESLServer listeners started')

        LOG.info("OutboundESLServer stopped")

    async def stop(self):
        await self.tg.cancel_scope.cancel()
        self._running = False

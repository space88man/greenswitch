import errno
import logging
import socket

from .esl import ESLProtocol, OutboundSessionHasGoneAway, AsyncResult



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

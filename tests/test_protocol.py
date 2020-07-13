from anyio import create_queue, create_task_group, sleep
from trioswitch import esl
from unittest import mock

import pytest

@pytest.fixture
def protocol():
    protocol = esl.ESLProtocol()
    protocol.connected = True
    protocol.client = mock.AsyncMock()
    protocol.client.receive_until = mock.AsyncMock()
    protocol.client.receive_exactly = mock.AsyncMock()

    protocol._esl_event_queue = mock.AsyncMock()
    protocol._auth_request_event = mock.AsyncMock()
    protocol._run = True

    return protocol


class TestProtocol:
    @pytest.mark.anyio
    async def test_receive_events_io_error_handling(self, protocol):
        """
        `receive_events` will close the socket and stop running in
        case of error
        """
        protocol.client.receive_until.side_effect = Exception()

        await protocol.receive_events()

        assert protocol.client.close.called
        assert not protocol.connected

    # N/A def test_receive_events_without_data_but_connected(self):

    # N/A def test_handle_event_with_packet_loss(self):

    @pytest.mark.anyio
    async def test_handle_event_disconnect_with_linger(self, protocol):
        """
        `handle_event` handles a "text/disconnect-notice" content
        with "Content-Disposition" header as "linger" by not
        disconnecting the socket.
        """
        protocol._commands_sent.append(mock.AsyncMock())

        event = mock.AsyncMock()
        event.headers = {
            'Content-Type': 'text/disconnect-notice',
            'Content-Disposition': 'linger',
        }

        await protocol.handle_event(event)
        assert protocol.connected
        assert not protocol.client.close.called

    @pytest.mark.anyio
    async def test_handle_event_rude_rejection(self, protocol):
        """
        `handle_event` handles a "text/rude-rejection" content
        by disabling `connected` flag but still reading it.
        """
        protocol.client.receive_exactly.return_value = b'123'
        event = mock.AsyncMock()
        event.headers = {
            'Content-Type': 'text/rude-rejection',
            'Content-Length': '3',
        }

        await protocol.handle_event(event)
        assert not protocol.connected
        assert protocol.client.receive_exactly.called

    @pytest.mark.anyio
    async def test_private_safe_exec_handler(self, protocol):
        """
        `_safe_exec_handler` is a private (and almost static) method
        to apply a function to an event without letting any exception
        reach the outter scope.
        """
        bad_handler = mock.AsyncMock(side_effect=Exception())
        bad_handler.__name__ = 'named-handler'
        event = mock.AsyncMock()

        await protocol._safe_exec_handler(bad_handler, event)

        assert bad_handler.called
        bad_handler.assert_called_with(event)

    @pytest.mark.anyio
    async def test_process_events_with_custom_name(self, protocol):
        """
        `process_events` will accept an event with "Event-Name" header as "CUSTOM"
        in its headers by calling the handlers indexed by its "Event-Subclass".
        """
        handlers = [mock.AsyncMock(), mock.AsyncMock()]
        protocol.event_handlers['custom-subclass'] = handlers

        event = mock.AsyncMock()
        event.headers = {
            'Event-Name': 'CUSTOM',
            'Event-Subclass': 'custom-subclass',
        }
        protocol._esl_event_queue = create_queue(0)
        await protocol._esl_event_queue.put(event)

        async with create_task_group() as protocol.tg:
            await protocol.tg.spawn(protocol.process_events)
            await sleep(0.1)

            await protocol.tg.cancel_scope.cancel()

        assert handlers[0].called
        handlers[0].assert_called_with(event)
        assert handlers[1].called
        handlers[1].assert_called_with(event)

    @pytest.mark.anyio
    async def test_process_events_with_log_type(self, protocol):
        """
        `process_events` will accept an event with "log/data" type
        and pass it to its handlers.
        """
        handlers = [mock.AsyncMock(), mock.AsyncMock()]
        protocol.event_handlers['log'] = handlers

        event = mock.AsyncMock()
        event.headers = {
            'Content-Type': 'log/data',
        }
        protocol._esl_event_queue = create_queue(0)
        await protocol._esl_event_queue.put(event)

        async with create_task_group() as protocol.tg:
            await protocol.tg.spawn(protocol.process_events)
            await sleep(0.1)
            await protocol.tg.cancel_scope.cancel()

        assert handlers[0].called
        handlers[0].assert_called_with(event)
        assert handlers[1].called
        handlers[1].assert_called_with(event)
            
    @pytest.mark.anyio
    async def test_process_events_with_no_handlers_will_rely_on_generic(self, protocol):
        """
        `process_events` will rely only on handlers for "*" if
        a given event has no handlers.
        """
        fallback_handlers = [mock.AsyncMock(), mock.AsyncMock()]
        protocol.event_handlers['*'] = fallback_handlers
        other_handlers = [mock.AsyncMock(), mock.AsyncMock()]
        protocol.event_handlers['other-handlers'] = other_handlers

        event = mock.AsyncMock()
        event.headers = {
            'Event-Name': 'CUSTOM',
            'Event-Subclass': 'custom-subclass-without-handlers',
        }
        protocol._esl_event_queue = create_queue(0)
        await protocol._esl_event_queue.put(event)

        async with create_task_group() as protocol.tg:
            await protocol.tg.spawn(protocol.process_events)
            await sleep(0.1)

            await protocol.tg.cancel_scope.cancel()

        assert fallback_handlers[0].called
        fallback_handlers[0].assert_called_with(event)
        assert fallback_handlers[1].called
        fallback_handlers[1].assert_called_with(event)
        assert not other_handlers[0].called
        assert not other_handlers[1].called

    @pytest.mark.anyio
    async def test_process_events_with_pre_handler(self, protocol):
        """
        `process_events` will call for `before_handle` property
        if it was implemented on such protocol instance, but the
        event will also be passed to default handlers.
        """
        protocol.before_handle = mock.AsyncMock()
        some_handlers = [mock.AsyncMock(), mock.AsyncMock()]
        protocol.event_handlers['some-handlers'] = some_handlers

        event = mock.AsyncMock()
        event.headers = {
            'Event-Name': 'CUSTOM',
            'Event-Subclass': 'some-handlers',
        }
        protocol._esl_event_queue = create_queue(0)
        await protocol._esl_event_queue.put(event)

        async with create_task_group() as protocol.tg:
            await protocol.tg.spawn(protocol.process_events)
            await sleep(0.1)
            await protocol.tg.cancel_scope.cancel()

        assert protocol.before_handle.called
        protocol.before_handle.assert_called_with(event)
        assert some_handlers[0].called
        some_handlers[0].assert_called_with(event)
        assert some_handlers[1].called
        some_handlers[1].assert_called_with(event)

    @pytest.mark.anyio
    async def test_process_events_with_post_handler(self, protocol):
        """
        `process_events` will call for `after_handle` property
        if it was implemented on such protocol instance, but the
        event will also be passed to default handlers.
        """
        protocol.after_handle = mock.AsyncMock()
        some_handlers = [mock.AsyncMock(), mock.AsyncMock()]
        protocol.event_handlers['some-handlers'] = some_handlers

        event = mock.AsyncMock()
        event.headers = {
            'Event-Name': 'CUSTOM',
            'Event-Subclass': 'some-handlers',
        }

        protocol._esl_event_queue = create_queue(0)
        await protocol._esl_event_queue.put(event)

        async with create_task_group() as protocol.tg:
            await protocol.tg.spawn(protocol.process_events)
            await sleep(0.1)
            await protocol.tg.cancel_scope.cancel()

        assert protocol.after_handle.called
        protocol.after_handle.assert_called_with(event)
        assert some_handlers[0].called
        some_handlers[0].assert_called_with(event)
        assert some_handlers[1].called
        some_handlers[1].assert_called_with(event)

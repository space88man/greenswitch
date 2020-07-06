import pytest
from anyio import create_task_group, create_tcp_server, run
from trioswitch import InboundESL


@pytest.fixture
def server():

    server = FakeESLServer('0.0.0.0', 8022, 'ClueCon')
    return server
    # await server.stop()


class FakeESLServer(object):
    def __init__(self, address, port, password):
        self._address = address
        self._port = port
        self._password = password

        self._running = False
        self.commands = {}
        self.setup_commands()
        self.clients = []

    def setup_commands(self):
        self.commands['api khomp show links concise'] = ('B00L00:kes{SignalLost},sync\n' +
                                                         'B01L00:kesOk,sync\n' +
                                                         'B01L01:[ksigInactive]\n')

    async def start_server(self):

        async with create_task_group() as self.tg, await create_tcp_server(self._port) as self.server:
            self._running = True
            async for client in self.server.accept_connections():
                self.clients.append(client)
                await self.tg.spawn(self.protocol_read, client)

    async def stop(self):
        self._running = False
        for cl in self.clients:
            await cl.close()
        await self.server.close()
        await self.tg.cancel_scope.cancel()
        print("FakeESLServer is stopped")

    async def command_reply(self, client, data):
        await client.send_all('Content-Type: command/reply\n'.encode('utf-8'))
        await client.send_all(('Reply-Text: %s\n\n' % data).encode('utf-8'))

    async def protocol_send(self, client, lines):
        for line in lines:
            await client.send_all((line + '\n').encode('utf-8'))
        await client.send_all('\n'.encode('utf-8'))

    async def api_response(self, client, data):
        data_length = len(data)
        await client.send_all('Content-Type: api/response\n'.encode('utf-8'))
        await client.send_all(('Content-Length: %d\n\n' % data_length).encode('utf-8'))
        await client.send_all(data.encode('utf-8'))

    async def handle_request(self, client, request):
        if request.startswith('auth'):
            received_password = request.split()[-1].strip()
            if received_password == self._password:
                await self.command_reply(client, '+OK accepted')
            else:
                await self.command_reply(client, '-ERR invalid')
                await self.disconnect(client)
        elif request == 'exit':
            await self.command_reply(client, '+OK bye')
            await self.disconnect(client)
            await self.server.close()
        elif request in self.commands:
            data = self.commands.get(request)
            if request.startswith('api'):
                self.api_response(client, data)
            else:
                await self.command_reply(client, data)
        else:
            if request.startswith('api'):
                self.api_response(client, '-ERR %s Command not found\n' % request.replace('api', '').split()[0])
            else:
                await self.command_reply(client, '-ERR command not found')

    async def protocol_read(self, client):
        # self._client_socket, address = self.server.accept()
        await self.protocol_send(client, ['Content-Type: auth/request'])
        while self._running:
            try:
                buf = await client.receive_until(b"\n\n", 1024)
            except Exception:
                self._running = False
                await self.stop()
                break
            if not buf and not self._running:
                break
            await self.handle_request(client, buf.decode("utf-8").strip())

    async def fake_event_plain(self, client, data):
        await self.protocol_send(client, ['Content-Type: text/event-plain',
                            'Content-Length: %s' % len(data)])
        await client.send_all(data)

    async def fake_raw_event_plain(self, client, data):
        await client.send_all(data)

    async def disconnect(self, client):
        await self.protocol_send(client, ['Content-Type: text/disconnect-notice',
                            'Content-Length: 67'])
        await client.send_all('Disconnected, goodbye.\n'.encode('utf-8'))
        await client.send_all('See you at ClueCon! http://www.cluecon.com/\n'.encode('utf-8'))
        self._running = False
        await client.close()



#!/usr/bin/env python


import logging
import trio
from trioswitch import outbound
import json
import threading

logging.basicConfig(level=logging.DEBUG)


"""
Add a extension on your dialplan to bound the outbound socket on FS channel
as example below

<extension name="out socket">
    <condition>
        <action application="socket" data="<outbound socket server host>:<outbound socket server port> sync full"/>
    </condition>
</extension>

Or see the complete doc on https://freeswitch.org/confluence/display/FREESWITCH/mod_event_socket
"""


class MyApplication(object):
    def __init__(self, session):
        self.session = session

        self.session.register_handle('*', self.pr_event)

    async def pr_event(self, event):
        print(json.dumps(event.headers, indent=4))
        if hasattr(event, 'data'):
            print(event.data)
    

    async def run(self):
        """
        Main function that is called when a call comes in.
        """
        await self.session.connect()
        print("connect")

        # await self.session.myevents()
        print("myevents")

        await self.session.linger()
        print("linger")

        await self.session.answer()
        print("answer")


        # Now block until the end of the file. pass block=False to
        # return immediately.
        await self.session.playback('local_stream://moh', block=False)
        print("playback")

        await trio.sleep(10.0)

        await self.session.call_command('break')
        print("break")

        await trio.sleep(1.0)

        await self.session.call_command('hangup')
        print("hangup")

        await trio.sleep(2.0)
        await self.session.stop()
        print("stop")


server = outbound.OutboundESLServer(
    bind_address='127.0.0.1',
    bind_port=8084,
    application=MyApplication,
    max_connections=5)


thr = threading.Thread(target=trio.run, args=(server.listen,))
thr.setDaemon(True)
thr.start()

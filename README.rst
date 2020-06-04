TrioSWITCH: FreeSWITCH Event Socket Protocol
=============================================

In the aioswitch* branches we are doing a rewrite of GreenSWITCH using
trio as async/await framework.


References:

* GreenSWITCH: https://github.com/EvoluxBR/greenswitch

* Trio: https://github.com/python-trio/trio

* FreeSWITCH: https://github.com/signalwire/freeswitch


Status
------

* inbound working, can receive events in plain or json
* no tests
* outbound not working


Example
-------

Inbound example::

    import trio
    from greeswitch import esl
    import threading

    async def before_fn(self, event):
        print("This is a before handle")

    async def after_fn(self, event):
        print("This is a after handle")


    esl.InboundESL.before_handle = before_fn
    esl.InboundESL.after_handle = after_fn


    conn = esl.InboundESL(host="localhost", port=8021, password="ClueCon")


    async def handler(event):
        print("This is an event handler")
        print(event.headers)

    conn.register_handle("conference::maintenance", handler)

    # run main loop in a thread so we can control it at the
    # REPL
    def do_1():
        thr = threading.Thread(target=trio.run, args=(conn.connect,))
        thr.setDaemon(True)
        thr.start()


    def do_task(task, *args):
        return trio.from_thread.run(task, *args, trio_token=conn.token)

    # python -i thisfile.py
    >>> do_1() # connected to FreeSWITCH ESL socket
    >>> do_task(conn.send, "events json CUSTOM conference::maintenance")
    >>> # make a conference call to FreeSWITCH and observe events

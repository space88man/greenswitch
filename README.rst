TrioSWITCH: FreeSWITCH Event Socket Protocol
=============================================

In the aioswitch* branches we are doing a rewrite of GreenSWITCH using
trio as async/await framework.


References:

* GreenSWITCH: https://github.com/EvoluxBR/greenswitch

* Trio: https://github.com/python-trio/trio

* FreeSWITCH: https://github.com/signalwire/freeswitch


Notes
-----

* Inbound: The outermost async function, i.e. the "run forever" main function,
  is the ``run_inbound()`` method of an ``InboundESL`` instance

  We store the nursery, and the trio token, as attributes on the object
  so the whole caboodle can be cancelled, or methods invoked from alien
  threads with ``trio.from_thread.run()``.
* Outbound: The outermost async function, i.e. the "run forever" main function,
  is the ``listen()`` method of an ``OutboundServer`` instance

  For each incoming connection it starts a child task group with the ``run_outbound()`` method.

.. |ss| raw:: html
   <strike>

.. |se| raw:: html
   </strike> 


Status
------
* outbound is working
* inbound working, can receive events in plain or json, XML
  not yet supported
* no tests
* |ss| outbound not working |se|


Example
-------

Inbound example::
    # save to testin.py
    import trio
    from trioswitch import esl
    import threading

    async def before_fn(self, event):
        print("This is a before handler")

    async def after_fn(self, event):
        print("This is a after handler")


    esl.InboundESL.before_handle = before_fn
    esl.InboundESL.after_handle = after_fn


    conn = esl.InboundESL(host="localhost", port=8021, password="ClueCon")


    async def handler(event):
        print("This is an event handler")
        print(event.headers)

    conn.register_handle("conference::maintenance", handler)

    # run trio event loop in a thread so we can control it at the REPL
    # run_inbound() method starts the outermost task group/nursery

    def do_inbound():
        thr = threading.Thread(target=trio.run, args=(conn.run_inbound,))
        thr.setDaemon(True)
        thr.start()

    # invoke coroutine function from an alien thread
    def do_task(corofn, *args):
        return trio.from_thread.run(corofn, *args, trio_token=conn.token)

    # ---- end of testin.py ----
    
    # python -i testin.py
    >>> do_inbound() # connected to FreeSWITCH ESL socket
    >>> do_task(conn.send, "events json CUSTOM conference::maintenance")
    >>> # make a conference call to FreeSWITCH and observe events

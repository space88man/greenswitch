from typing import Optional

import trio.abc

# Unlike Twisted / asyncio, trio stream abstractions
# do not have a line reader mode and the ability to switch
# between line reader and N bytes reader.
# 
# This functionality is necessary for Content-Length: followed
# by N opaque bytes protocols such as HTTP/SIP/FreeSWITCH/RESP3
# The following implementation is suggested by GH user
# @alexchamberlain https://github.com/python-trio/trio/issues/796#issuecomment-638143456

class TerminatedFrameReceiver:
    def __init__(
        self,
        buffer: bytes = b"",
        stream: Optional[trio.abc.ReceiveStream] = None,
        terminator: bytes = b"\n",
        max_frame_length: int = 16384,
    ):
        assert isinstance(buffer, bytes)
        assert not stream or isinstance(stream, trio.abc.ReceiveStream)

        self.stream = stream
        self.terminator = terminator
        self.max_frame_length = max_frame_length

        self._buf = bytearray(buffer)
        self._next_find_idx = 0

    def __bool__(self):
        return bool(self._buf)

    async def receive(self):
        while True:
            terminator_idx = self._buf.find(self.terminator, self._next_find_idx)
            if terminator_idx < 0:
                self._next_find_idx = max(0, len(self._buf) - len(self.terminator) + 1)
                await self._receive()
            else:
                return self._frame(terminator_idx + len(self.terminator))

    async def receive_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            await self._receive()

        return self._frame(n)

    async def _receive(self):
        if len(self._buf) > self.max_frame_length:
            raise ValueError("frame too long")

        more_data = await self.stream.receive_some() if self.stream is not None else b""
        if more_data == b"":
            if self._buf:
                raise ValueError("incomplete frame")
            raise trio.EndOfChannel

        self._buf += more_data

    def _frame(self, idx: int) -> bytes:
        frame = self._buf[:idx]
        del self._buf[:idx]
        self._next_find_idx = 0
        return frame
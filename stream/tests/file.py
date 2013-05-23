import io
import os
import itertools
import unittest
from ..file import File
from ...monad import async
from ...event import Event
from ...common import BrokenPipeError
from ...core import schedule
from ...tests import async_test

__all__ = ('FileTest',)


class FileTest(unittest.TestCase):
    BLOCK = b'X' * 1024

    @async_test
    def test(self):
        received = io.BytesIO()
        start = Event()
        reader_fd, writer_fd = os.pipe()

        @async
        def reader_coro():
            with File(reader_fd) as reader:
                try:
                    while True:
                        received.write((yield reader.read(1024)))
                except BrokenPipeError:
                    pass
        reader_future = reader_coro().future()
        if reader_future.completed:
            yield reader_future

        with File(writer_fd) as writer:
            # fill socket buffer
            size = 0
            for blocks in itertools.count():
                write = writer.write(self.BLOCK).future()
                if not write.completed:
                    size += yield write
                    break
                else:
                    size += write.value
            start(None)

            # drain socket
            while received.tell() < size:
                yield schedule()
            self.assertEqual(received.getvalue(), b'X' * size)
            received.seek(0)
            received.truncate(0)

            yield writer.write(b'one')
            yield schedule()

            yield writer.write(b', two')
            yield schedule()

            yield writer.write(b', three')
            yield schedule()

        yield reader_future
        self.assertEqual(received.getvalue(), b'one, two, three')
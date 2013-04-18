import struct
from functools import wraps
from collections import deque
from .wrapped import WrappedStream
from ..common import BrokenPipeError
from ..monad import Cont, async, do_return
from ..event import Event

__all__ = ('BufferedStream',)


class BufferedStream(WrappedStream):
    """Buffered stream
    """
    default_buffer_size = 1 << 16
    size_struct = struct.Struct('>I')

    def __init__(self, base, buffer_size=None):
        WrappedStream.__init__(self, base)

        self.buffer_size = buffer_size or self.default_buffer_size
        self.read_buffer = Buffer()
        self.write_buffer = Buffer()

        @async
        def flush():
            """Flush write buffers
            """
            with self.writing:
                while self.write_buffer:
                    block = self.write_buffer.slice(self.buffer_size)
                    self.write_buffer.dequeue((yield self.base.write(block)), False)
                yield self.base.flush()
        self.flush = singleton(flush)

    @async
    def read(self, size):
        if not size:
            do_return(b'')
        with self.reading:
            if not self.read_buffer:
                self.read_buffer.enqueue((yield self.base.read(self.buffer_size)))
            do_return(self.read_buffer.dequeue(size))

    @async
    def read_until_size(self, size):
        """Read exactly size bytes
        """
        if not size:
            do_return(b'')
        with self.reading:
            while len(self.read_buffer) < size:
                self.read_buffer.enqueue((yield self.base.read(self.buffer_size)))
            do_return(self.read_buffer.dequeue(size))

    @async
    def read_until_eof(self):
        """Read until stream is closed
        """
        with self.reading:
            try:
                while True:
                    self.read_buffer.enqueue((yield self.base.read(self.buffer_size)))
            except BrokenPipeError:
                pass
            do_return(self.read_buffer.dequeue())

    @async
    def read_until_sub(self, sub=None):
        """Read until substring is found

        Returns data including substring. Default substring is "\\n".
        """
        sub = sub or b'\n'
        with self.reading:
            offset = 0
            while True:
                data = self.read_buffer.slice()
                find_offset = data[offset:].find(sub)
                if find_offset >= 0:
                    break
                offset = max(0, len(data) - len(sub))
                self.read_buffer.enqueue((yield self.base.read(self.buffer_size)))
            do_return(self.read_buffer.dequeue(offset + find_offset + len(sub)))

    @async
    def read_until_regex(self, regex):
        """Read until regular expression is matched

        Returns data (including match) and match object.
        """
        with self.reading:
            while True:
                data = self.read_buffer.slice()
                match = regex.search(data)
                if match:
                    break
                self.read_buffer.enqueue((yield self.base.read(self.buffer_size)))
            do_return((self.read_buffer.dequeue(match.end()), match))

    @async
    def write(self, data):
        """Write data

        Write data without blocking if write buffer's length less then doubled
        buffer size limit, if buffer's length is more then buffer size limit
        flush is started in the background.
        """
        with self.writing:  # state check
            self.write_buffer.enqueue(data)
        if len(self.write_buffer) > 2 * self.buffer_size:
            yield self.flush()
        elif len(self.write_buffer) > self.buffer_size:
            self.flush()()
        do_return(len(data))

    def write_schedule(self, data):
        """Enqueue data to write buffer

        Just enqueues data to write buffer (buffer's size limit would not be
        checked), flush need to be called manually.
        """
        self.write_buffer.enqueue(data)
        return len(data)

    @async
    def read_bytes(self):
        """Read bytes object
        """
        do_return((yield self.read_until_size(self.size_struct.unpack
                 ((yield self.read_until_size(self.size_struct.size)))[0])))

    def write_bytes(self, bytes):
        """Write bytes object to buffer
        """
        self.write_schedule(self.size_struct.pack(len(bytes)))
        self.write_schedule(bytes)

    @async
    def read_struct_list(self, struct, complex=None):
        """Read list of structures
        """
        struct_data = (yield self.read_until_size(self.size_struct.unpack((
                       yield self.read_until_size(self.size_struct.size)))[0]))
        if complex:
            do_return([struct.unpack(struct_data[offset:offset + struct.size])
                      for offset in range(0, len(struct_data), struct.size)])
        else:
            do_return([struct.unpack(struct_data[offset:offset + struct.size])[0]
                      for offset in range(0, len(struct_data), struct.size)])

    def write_struct_list(self, struct_list, struct, complex=None):
        """Write list of structures to buffer
        """
        self.write_schedule(self.size_struct.pack(len(struct_list) * struct.size))
        if complex:
            for struct_target in struct_list:
                self.write_schedule(struct.pack(*struct_target))
        else:
            for struct_target in struct_list:
                self.write_schedule(struct.pack(struct_target))

    @async
    def read_bytes_list(self):
        """Read array of bytes
        """
        bytes_list = []
        for size in (yield self.read_struct_list(self.size_struct, False)):
            bytes_list.append((yield self.read_until_size(size)))
        do_return(bytes_list)

    def write_bytes_list(self, bytes_list):
        """Write bytes array object to buffer
        """
        self.write_struct_list([len(bytes) for bytes in bytes_list],
                               self.size_struct, False)
        for bytes in bytes_list:
            self.write_schedule(bytes)


def singleton(action):
    """Singleton asynchronous action

    If there is current non finished action all new continuations will be hooked
    to this action result, otherwise new action will be started.
    """
    @wraps(action)
    def singleton_action():
        def run(ret):
            done.on_once(lambda val: ret(val))
            if len(done) == 1:
                action().__monad__()(lambda val: done(val))
        return Cont(run)
    done = Event()
    return singleton_action


class Buffer(object):
    """Bytes FIFO buffer
    """
    def __init__(self):
        self.offset = 0
        self.chunks = deque()
        self.chunks_size = 0

    def slice(self, size=None, offset=None):
        """Get bytes with ``offset`` and ``size``
        """
        offset = offset or 0
        size = size or len(self)

        data = []
        data_size = 0

        # dequeue chunks
        size += self.offset + offset
        while self.chunks:
            if data_size >= size:
                break
            chunk = self.chunks.popleft()
            data.append(chunk)
            data_size += len(chunk)

        # re-queue merged chunk
        data = b''.join(data)
        self.chunks.appendleft(data)

        return data[self.offset + offset:size]

    def enqueue(self, data):
        """Enqueue "data" to buffer
        """
        if data:
            self.chunks.append(data)
            self.chunks_size += len(data)

    def dequeue(self, size=None, returns=None):
        """Dequeue "size" bytes from buffer

        Returns dequeued data if returns if True (or not set) otherwise None.
        """
        size = size or len(self)
        if not self.chunks:
            return b''

        data = []
        data_size = 0

        # dequeue chunks
        size = min(size + self.offset, self.chunks_size)
        while self.chunks:
            if data_size >= size:
                break
            chunk = self.chunks.popleft()
            data.append(chunk)
            data_size += len(chunk)

        if data_size == size:
            # no chunk re-queue
            self.chunks_size -= data_size
            offset, self.offset = self.offset, 0
        else:
            # If offset is beyond the middle of the chunk it will be split
            offset = len(chunk) - (data_size - size)
            if offset << 1 > len(chunk):
                chunk = chunk[offset:]
                offset, self.offset = self.offset, 0
            else:
                offset, self.offset = self.offset, offset

            # re-queue chunk
            self.chunks.appendleft(chunk)
            self.chunks_size += len(chunk) - data_size

        if returns is None or returns:
            return b''.join(data)[offset:size]

    def __len__(self):
        return self.chunks_size - self.offset

    def __bool__(self):
        return bool(self.chunks)
    __nonzero__ = __bool__

    def __str__(self):
        return '<Buffer[len:{}] at {}>'.format(len(self), id(self))
    __repr__ = __str__
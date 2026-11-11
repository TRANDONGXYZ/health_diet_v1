"""Cooperative socket reads, including while waiting for HTTP headers on Windows."""
import io
import select
import time


class InterruptibleSocket:
    def __init__(self, sock, token, remaining, idle):
        self.sock, self.token, self.remaining = sock, token, remaining
        self.idle = idle
        self.files = 0
        self.closing = False

    def __getattr__(self, name):
        return getattr(self.sock,name)

    def makefile(self, mode, *args, **kwargs):
        if mode != 'rb':
            raise ValueError('Read-only HTTP transport expected.')
        self.files += 1
        return io.BufferedReader(_Reader(self))

    def close(self):
        # HTTPConnection can close its reference immediately after headers.
        # Its HTTPResponse must retain the socket until the file is closed.
        self.closing = True
        if not self.files:
            self.sock.close()


class _Reader(io.RawIOBase):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.last_data = time.monotonic()

    def readable(self):
        return True

    def readinto(self, buffer):
        owner = self.owner
        while True:
            owner.token.check()
            remaining = owner.remaining()
            idle = owner.idle - (time.monotonic() - self.last_data)
            if idle <= 0:
                raise TimeoutError('Ollama stream idle timeout')
            pending = getattr(owner.sock,'pending',lambda:0)()
            ready = pending or select.select([owner.sock],[],[],min(.1,idle,remaining))[0]
            owner.token.check()
            if not ready:
                continue
            timeout = owner.sock.gettimeout()
            try:
                owner.sock.settimeout(min(.1,remaining,idle))
                count = owner.sock.recv_into(buffer)
            except (TimeoutError, BlockingIOError):
                continue
            finally:
                owner.sock.settimeout(timeout)
            self.last_data = time.monotonic()
            return count

    def close(self):
        if not self.closed:
            self.owner.files -= 1
            if self.owner.closing and not self.owner.files:
                self.owner.sock.close()
        super().close()

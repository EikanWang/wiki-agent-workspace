"""Server-side PTY that runs `tmux attach` and relays raw bytes."""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import struct
import subprocess
import termios
from typing import Awaitable, Callable


class PtyProcess:
    def __init__(self, args: list[str], env: dict[str, str], cols: int, rows: int, cwd: str | None = None):
        self.args = args
        self.env = env
        self.cols = cols
        self.rows = rows
        self.cwd = cwd
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self._write_buf = bytearray()
        self._writer_registered = False
        self._closed = False
        self.on_output: Callable[[bytes], Awaitable[None]] | None = None
        self.on_exit: Callable[[int | None], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit_task: asyncio.Task | None = None

    def start(self) -> None:
        master, slave = os.openpty()
        self._set_winsize(slave, self.rows, self.cols)

        def preexec() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(
            self.args, stdin=slave, stdout=slave, stderr=slave, preexec_fn=preexec, env=self.env,
            cwd=self.cwd, close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        self.master_fd = master
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master, self._readable)
        self._exit_task = self._loop.create_task(self._wait_exit())

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None or self._closed:
            return
        self.cols, self.rows = cols, rows
        try:
            self._set_winsize(self.master_fd, rows, cols)
        except OSError:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    def _readable(self) -> None:
        assert self.master_fd is not None and self._loop is not None
        try:
            data = os.read(self.master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            # EOF: the attach client exited (detached/killed)
            try:
                self._loop.remove_reader(self.master_fd)
            except (ValueError, OSError):
                pass
            return
        if self.on_output:
            self._loop.create_task(self.on_output(data))

    def write(self, data: bytes) -> None:
        if self.master_fd is None or self._closed:
            return
        self._write_buf += data
        self._flush()

    def _flush(self) -> None:
        assert self.master_fd is not None and self._loop is not None
        while self._write_buf:
            try:
                n = os.write(self.master_fd, bytes(self._write_buf[:4096]))
            except BlockingIOError:
                if not self._writer_registered:
                    self._loop.add_writer(self.master_fd, self._writable)
                    self._writer_registered = True
                return
            except OSError:
                self._write_buf.clear()
                return
            del self._write_buf[:n]
        if self._writer_registered:
            try:
                self._loop.remove_writer(self.master_fd)
            except (ValueError, OSError):
                pass
            self._writer_registered = False

    def _writable(self) -> None:
        self._flush()

    async def _wait_exit(self) -> None:
        assert self.proc is not None
        # Poll instead of blocking a worker thread so interpreter shutdown is never held up.
        while self.proc.poll() is None:
            await asyncio.sleep(0.2)
        rc = self.proc.returncode
        # drain remaining output
        if self.master_fd is not None:
            for _ in range(50):
                try:
                    data = os.read(self.master_fd, 65536)
                except (BlockingIOError, OSError):
                    break
                if not data:
                    break
                if self.on_output:
                    await self.on_output(data)
        if self.on_exit:
            await self.on_exit(rc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop and self.master_fd is not None:
            try:
                self._loop.remove_reader(self.master_fd)
            except (ValueError, OSError):
                pass
            if self._writer_registered:
                try:
                    self._loop.remove_writer(self.master_fd)
                except (ValueError, OSError):
                    pass
        if self.proc and self.proc.poll() is None:
            # Ending the attach client only detaches this browser; the tmux session keeps running.
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

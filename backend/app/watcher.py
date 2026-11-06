"""Filesystem watcher for the workspace root (DESIGN §5.3): 500 ms debounce, publishes
`file_changed` / `tree_changed` events on the global bus and records new document versions.

Registering a large tree with the notify backend can take tens of seconds and holds the GIL,
so the watcher runs in a separate *process* and streams changes to the server over a pipe.
The server stays responsive; until registration completes the UI's manual refresh still works.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import pickle
import struct
import time
from pathlib import Path
from typing import Any

from .documents import MARKDOWN_SUFFIXES, DocumentService, EXCLUDED_DIRS
from .events import EventBus

log = logging.getLogger("waw.watcher")


def _child_main(root: str, conn, excluded: list[str]) -> None:  # pragma: no cover - separate process
    """Child process: run watchfiles.watch and send one message per batch over the pipe."""
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from watchfiles import Change, watch

    excluded_set = set(excluded)

    def flt(change: Change, path: str) -> bool:
        parts = Path(path).parts
        if any(part in excluded_set or part.startswith(".waw") for part in parts):
            return False
        return not Path(path).name.startswith(".waw-tmp-")

    try:
        conn.send({"type": "starting"})
        first = True
        for changes in watch(root, debounce=500, step=100, watch_filter=flt, recursive=True, ignore_permission_denied=True,
                             rust_timeout=1000, yield_on_timeout=True):
            if first:
                first = False
                conn.send({"type": "ready"})
            if not changes:
                continue
            conn.send({"type": "changes", "items": [[int(c), p] for c, p in changes]})
    except Exception as exc:
        msg = f"{exc.__class__.__name__}: {exc}"
        if "watch limit" in str(exc).lower() or "MaxFilesWatch" in str(exc):
            msg = ("the inotify watch limit of this host is exhausted (other programs such as agent servers may hold most of it); "
                   "raise it with `sudo sysctl -w fs.inotify.max_user_watches=1048576` (persist in /etc/sysctl.d/) and restart")
        try:
            conn.send({"type": "error", "message": msg})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


class _FrameReader:
    """Incremental decoder for the multiprocessing ``Connection.send`` wire format (``!i`` length, or ``-1`` plus
    ``!Q`` for >2 GiB, then a pickle). The server reads whatever bytes are available and decodes only complete
    messages, so a large batch that arrives in pieces is fine. ``Connection.recv()`` cannot be used on the event
    loop: uvloop (uvicorn's default loop) makes the descriptor non-blocking, and a message that has only partly
    arrived then raises BlockingIOError."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Any]:
        self.buf += data
        out = []
        while True:
            if len(self.buf) < 4:
                break
            (size,) = struct.unpack("!i", self.buf[:4])
            head = 4
            if size == -1:
                if len(self.buf) < 12:
                    break
                (size,) = struct.unpack("!Q", self.buf[4:12])
                head = 12
            if len(self.buf) < head + size:
                break
            body = bytes(self.buf[head:head + size])
            del self.buf[:head + size]
            out.append(pickle.loads(body))
        return out


class WorkspaceWatcher:
    MAX_RESTARTS = 5  # consecutive automatic restarts before giving up (counter resets once a child is ready)

    def __init__(self, root: Path, docs: DocumentService, bus: EventBus):
        self.root = root
        self.docs = docs
        self.bus = bus
        self.error: str | None = None
        self.ready = False
        self.started_at: float | None = None
        self.restarts = 0
        self._proc: mp.process.BaseProcess | None = None
        self._fd: int | None = None
        self._frames = _FrameReader()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._child_failed = False
        self._restart_handle: asyncio.TimerHandle | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._spawn()

    def _spawn(self) -> None:
        assert self._loop is not None
        ctx = mp.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        self._proc = ctx.Process(target=_child_main, args=(str(self.root), child_conn, sorted(EXCLUDED_DIRS)), daemon=True, name="waw-watcher")
        self._proc.start()
        child_conn.close()
        # take the raw descriptor: we frame messages ourselves (see _FrameReader)
        self._fd = os.dup(parent_conn.fileno())
        parent_conn.close()
        os.set_blocking(self._fd, False)
        self._frames = _FrameReader()
        self.started_at = time.time()
        self._loop.add_reader(self._fd, self._readable)

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        if self._loop:
            try:
                self._loop.remove_reader(self._fd)
            except (ValueError, OSError):
                pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def _kill_child(self) -> None:
        proc = self._proc
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join(2)

    async def stop(self) -> None:
        self._stopping = True
        if self._restart_handle:
            self._restart_handle.cancel()
            self._restart_handle = None
        self._close_fd()
        if self._proc and self._proc.is_alive():
            await asyncio.to_thread(self._kill_child)

    def status(self) -> dict[str, Any]:
        return {"ready": self.ready, "error": self.error, "restarts": self.restarts,
                "registering_seconds": (None if self.ready or not self.started_at else round(time.time() - self.started_at))}

    # ------------------------------------------------------------------ pipe reader
    def _readable(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            while True:
                try:
                    data = os.read(fd, 1 << 20)
                except (BlockingIOError, InterruptedError):
                    return  # drained; the rest of a partial message comes later
                if not data:
                    self._lost("the watcher process closed its pipe")
                    return
                for msg in self._frames.feed(data):
                    self._on_message(msg)
        except Exception as exc:  # undecodable data, closed descriptor, ...
            log.exception("filesystem watcher pipe for %s failed", self.root)
            self._lost(f"{exc.__class__.__name__}: {exc}")

    def _lost(self, why: str) -> None:
        """The child went away or the pipe broke: restart it (changes made meanwhile are announced as a tree
        change once the new child is ready), unless it keeps failing."""
        self._close_fd()
        proc = self._proc
        code = proc.exitcode if proc is not None and not proc.is_alive() else None
        detail = f"{why}" + (f" (exit code {code})" if code is not None else "")
        self.ready = False
        if self._stopping:
            return
        self._kill_child()
        if self._child_failed:
            return  # keep the child's own error message
        if self.restarts >= self.MAX_RESTARTS:
            self.error = f"filesystem watcher stopped after {self.restarts} restarts ({detail}); use manual refresh"
            log.error("%s: %s", self.root, self.error)
            self.bus.publish("global", {"type": "watcher_error", "workspace_id": self.docs.workspace_id, "message": self.error})
            return
        self.restarts += 1
        delay = min(30.0, 2.0 ** self.restarts)
        log.warning("filesystem watcher for %s lost: %s; restarting in %.0f s (attempt %d/%d)", self.root, detail, delay,
                    self.restarts, self.MAX_RESTARTS)
        assert self._loop is not None
        self._restart_handle = self._loop.call_later(delay, self._restart)

    def _restart(self) -> None:
        self._restart_handle = None
        if self._stopping:
            return
        try:
            self._spawn()
        except Exception as exc:
            log.exception("restarting the filesystem watcher for %s failed", self.root)
            self._lost(f"restart failed: {exc}")

    def _on_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "ready":
            recovered = self.restarts > 0 or self.error is not None
            self.ready = True
            self.error = None
            if recovered:
                log.info("filesystem watcher for %s is running again", self.root)
                # events during the gap were missed: make clients re-list the tree and re-check open files
                self._publish({"type": "tree_changed", "workspace_id": self.docs.workspace_id})
            else:
                log.info("filesystem watcher ready for %s", self.root)
            self.restarts = 0
            self.bus.publish("global", {"type": "watcher_ready", "workspace_id": self.docs.workspace_id})
        elif t == "error":
            # the child explains why it stopped (e.g. inotify watch limit): restarting would not help
            self._child_failed = True
            self.error = f"filesystem watcher stopped: {msg.get('message')}; use manual refresh"
            log.warning(self.error)
        elif t == "changes":
            if self._loop:
                self._loop.create_task(asyncio.to_thread(self._handle, msg.get("items", [])))

    # ------------------------------------------------------------------ handling (worker thread)
    def _handle(self, items: list[list[Any]]) -> None:
        kinds = {1: "added", 2: "modified", 3: "deleted"}
        tree_changed = False
        for code, path in items:
            p = Path(path)
            try:
                rel = p.resolve().relative_to(self.root.resolve()).as_posix() if p.exists() else p.relative_to(self.root).as_posix()
            except ValueError:
                try:
                    rel = p.relative_to(self.root).as_posix()
                except ValueError:
                    continue
            if p.suffix.lower() not in MARKDOWN_SUFFIXES:
                if code in (1, 3):
                    tree_changed = True
                continue
            tree_changed = True
            try:
                info = self.docs.observe_file_change(rel)
            except Exception as exc:  # a bad file must not kill the watcher
                log.warning("observe_file_change(%s) failed: %s", rel, exc)
                info = None
            ev = {"type": "file_changed", "path": rel, "change": kinds.get(code, "modified"), "workspace_id": self.docs.workspace_id}
            if info:
                ev.update({k: v for k, v in info.items() if k != "path"})
            self._publish(ev)
        if tree_changed:
            self._publish({"type": "tree_changed", "workspace_id": self.docs.workspace_id})

    def _publish(self, event: dict[str, Any]) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.bus.publish, "global", event)

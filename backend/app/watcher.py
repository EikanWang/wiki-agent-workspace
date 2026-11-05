"""Filesystem watcher for the workspace root (DESIGN §5.3): 500 ms debounce, publishes
`file_changed` / `tree_changed` events on the global bus and records new document versions.

Registering a large tree with the notify backend can take tens of seconds and holds the GIL,
so the watcher runs in a separate *process* and streams changes to the server over a pipe.
The server stays responsive; until registration completes the UI's manual refresh still works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing as mp
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


class WorkspaceWatcher:
    def __init__(self, root: Path, docs: DocumentService, bus: EventBus):
        self.root = root
        self.docs = docs
        self.bus = bus
        self.error: str | None = None
        self.ready = False
        self.started_at: float | None = None
        self._proc: mp.process.BaseProcess | None = None
        self._conn = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        ctx = mp.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        self._proc = ctx.Process(target=_child_main, args=(str(self.root), child_conn, sorted(EXCLUDED_DIRS)), daemon=True, name="waw-watcher")
        self._proc.start()
        child_conn.close()
        self._conn = parent_conn
        self.started_at = time.time()
        self._loop.add_reader(parent_conn.fileno(), self._readable)

    async def stop(self) -> None:
        if self._loop and self._conn is not None:
            try:
                self._loop.remove_reader(self._conn.fileno())
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._proc and self._proc.is_alive():
            self._proc.terminate()
            await asyncio.to_thread(self._proc.join, 5)
            if self._proc.is_alive():
                self._proc.kill()

    def status(self) -> dict[str, Any]:
        return {"ready": self.ready, "error": self.error,
                "registering_seconds": (None if self.ready or not self.started_at else round(time.time() - self.started_at))}

    # ------------------------------------------------------------------ pipe reader
    def _readable(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            while conn.poll():
                msg = conn.recv()
                self._on_message(msg)
        except (EOFError, OSError):
            if self._loop:
                try:
                    self._loop.remove_reader(conn.fileno())
                except (ValueError, OSError):
                    pass
            if not self.error:
                self.error = "filesystem watcher process exited; use manual refresh"
                log.warning(self.error)

    def _on_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "ready":
            self.ready = True
            log.info("filesystem watcher ready for %s", self.root)
            self.bus.publish("global", {"type": "watcher_ready"})
        elif t == "error":
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
            ev = {"type": "file_changed", "path": rel, "change": kinds.get(code, "modified")}
            if info:
                ev.update({k: v for k, v in info.items() if k != "path"})
            self._publish(ev)
        if tree_changed:
            self._publish({"type": "tree_changed"})

    def _publish(self, event: dict[str, Any]) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.bus.publish, "global", event)

"""Filesystem watcher: pipe framing under both event loops, automatic restart, permanent child errors."""

from __future__ import annotations

import asyncio
import os
import pickle
import signal
import struct
import threading
import time
from pathlib import Path

import pytest

from backend.app import watcher as W


class Docs:
    workspace_id = "ws-test"

    def __init__(self):
        self.seen: list[str] = []

    def observe_file_change(self, rel):
        self.seen.append(rel)
        return None


class Bus:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, channel, event):
        self.events.append(event)


def frame(obj) -> bytes:
    body = pickle.dumps(obj)
    return struct.pack("!i", len(body)) + body


def test_frame_reader_decodes_split_and_batched_messages():
    r = W._FrameReader()
    a, b = frame({"n": 1}), frame({"n": 2, "pad": "x" * 100_000})
    data = a + b
    got = []
    for i in range(0, len(data), 7_777):  # arbitrary chunking, including inside the length header
        got += r.feed(data[i:i + 7_777])
    assert got == [{"n": 1}, {"n": 2, "pad": "x" * 100_000}]
    assert r.feed(b"") == [] and not r.buf
    # the >2 GiB header form (-1 then !Q) is understood too
    body = pickle.dumps("big")
    assert W._FrameReader().feed(struct.pack("!i", -1) + struct.pack("!Q", len(body)) + body) == ["big"]


def _partial_message_scenario(loop_name: str) -> tuple[list[int], str | None]:
    """A message that arrives in two parts with a pause in between, as a large batch from the child can.
    Under uvloop (uvicorn's default) the descriptor is non-blocking; this used to be reported as
    'filesystem watcher process exited' and live updates stopped for good."""

    async def main():
        w = W.WorkspaceWatcher(Path("/nonexistent"), Docs(), Bus())
        w._loop = asyncio.get_running_loop()
        got: list[int] = []
        w._handle = lambda items: got.append(len(items))
        r, wfd = os.pipe()
        os.set_blocking(r, False)
        w._fd = r
        w._loop.add_reader(r, w._readable)
        msg = frame({"type": "changes", "items": [[2, "/x/a.md"]] * 5000})

        def writer():
            os.write(wfd, msg[: len(msg) // 2])
            time.sleep(0.3)
            os.write(wfd, msg[len(msg) // 2:])

        threading.Thread(target=writer, daemon=True).start()
        for _ in range(50):
            await asyncio.sleep(0.05)
            if got:
                break
        await asyncio.sleep(0.2)
        err = w.error
        w._stopping = True
        w._close_fd()
        os.close(wfd)
        return got, err

    if loop_name == "uvloop":
        uvloop = pytest.importorskip("uvloop")
        return uvloop.run(main())
    return asyncio.run(main())


@pytest.mark.parametrize("loop_name", ["asyncio", "uvloop"])
def test_partial_message_is_not_mistaken_for_an_exit(loop_name):
    got, err = _partial_message_scenario(loop_name)
    assert got == [5000]
    assert err is None


async def _wait(cond, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_watcher_restarts_after_the_child_dies(tmp_path: Path):
    root = tmp_path / "wiki"
    root.mkdir()
    docs, bus = Docs(), Bus()
    w = W.WorkspaceWatcher(root, docs, bus)
    w.start()
    try:
        assert await _wait(lambda: w.ready), "watcher never became ready"
        first = w._proc
        os.kill(first.pid, signal.SIGKILL)
        assert await _wait(lambda: not w.ready or w._proc is not first, 10)
        # restarted automatically (first retry after ~2 s) and ready again, with no lasting error
        assert await _wait(lambda: w.ready and w._proc is not first, 30), w.status()
        assert w.error is None and w.restarts == 0
        assert any(e.get("type") == "tree_changed" for e in bus.events)  # clients re-list what was missed
        # and it still sees changes
        (root / "after.md").write_text("# hi\n")
        assert await _wait(lambda: "after.md" in docs.seen, 15)
    finally:
        await w.stop()
    assert not w._proc.is_alive()


async def test_child_reported_error_is_kept_and_not_retried(tmp_path: Path):
    w = W.WorkspaceWatcher(tmp_path, Docs(), Bus())
    w._loop = asyncio.get_running_loop()
    w._on_message({"type": "error", "message": "the inotify watch limit of this host is exhausted"})
    w._lost("the watcher process closed its pipe")
    assert w._restart_handle is None
    assert "inotify watch limit" in (w.error or "")


async def test_tree_changed_names_the_folders_that_changed(tmp_path: Path):
    """Clients reload only these folders instead of every folder they show."""
    root = tmp_path / "wiki"
    (root / "a" / "b").mkdir(parents=True)
    bus = Bus()
    w = W.WorkspaceWatcher(root, Docs(), bus)
    w.start()
    try:
        assert await _wait(lambda: w.ready)
        (root / "a" / "b" / "new.md").write_text("# new\n")
        (root / "top.txt").write_text("x")
        assert await _wait(lambda: any(e.get("type") == "tree_changed" for e in bus.events), 15)
        await asyncio.sleep(1.0)  # let a second batch (if the two writes were split) arrive
        dirs = set()
        for e in bus.events:
            if e.get("type") == "tree_changed":
                assert "dirs" in e, e
                dirs |= set(e["dirs"])
        assert dirs == {"a/b", ""}, dirs
    finally:
        await w.stop()

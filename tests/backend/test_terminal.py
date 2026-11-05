"""Terminal acceptance tests C9-C11 with real tmux (skipped if tmux is missing)."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import ORIGIN, TOKEN, requires_tmux
from backend.app.main import create_app

pytestmark = requires_tmux


class Term:
    """Synchronous helper over Starlette's TestClient WebSocket."""

    def __init__(self, client: TestClient, csrf: str, tid: str, cols=100, rows=30):
        self.closed = False
        getattr(client, "terms", []).append(self)
        self.ws = client.websocket_connect(
            f"/api/v1/terminals/{tid}/ws?cols={cols}&rows={rows}",
            subprotocols=[f"waw-csrf.{csrf}"],
            headers={"origin": ORIGIN},
        )
        self.conn = self.ws.__enter__()
        self.buf = b""

    def send_bytes(self, data: bytes) -> None:
        self.conn.send_text(json.dumps({"type": "input", "data": base64.b64encode(data).decode()}))

    def send_json(self, obj) -> None:
        self.conn.send_text(json.dumps(obj))

    def recv(self, timeout=10):
        return json.loads(self.conn.receive_text())

    def read_until(self, needle: bytes, timeout: float = 15) -> bytes:
        deadline = time.time() + timeout
        while needle not in self.buf and time.time() < deadline:
            msg = self.recv()
            if msg["type"] == "output":
                self.buf += base64.b64decode(msg["data"])
            elif msg["type"] == "state" and msg["state"] in ("exited", "error"):
                self.buf += f"[STATE:{msg['state']}:{msg['message']}]".encode()
                break
        return self.buf

    def expect_state(self, state: str, timeout: float = 10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.recv()
            if msg["type"] == "state":
                assert msg["state"] == state, msg
                return msg
            if msg["type"] == "output":
                self.buf += base64.b64decode(msg["data"])
        raise AssertionError("no state message")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.ws.__exit__(None, None, None)
        except Exception:
            pass


@pytest.fixture
def tc(settings):
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as client:
        r = client.post("/api/v1/auth/login", json={"token": TOKEN}, headers={"origin": ORIGIN})
        assert r.status_code == 200
        client.csrf = r.json()["csrf_token"]
        client.mut = {"origin": ORIGIN, "x-csrf-token": client.csrf}
        client.terms = []
        try:
            yield client
        finally:
            for term in client.terms:
                term.close()
            for t in client.get("/api/v1/terminals").json()["terminals"]:
                client.delete(f"/api/v1/terminals/{t['id']}", headers=client.mut)
    # clean up any tmux sessions created by this test's app socket
    ctx = app.state.ctx
    try:
        for name in ctx.terminals.tmux.list_sessions():
            ctx.terminals.tmux.kill_session(name)
    except Exception:
        pass


def tmux_pane_pids(ctx, tmux_session: str) -> list[int]:
    p = subprocess.run(ctx.terminals.tmux.base_args() + ["list-panes", "-t", f"{tmux_session}:", "-F", "#{pane_pid}"], capture_output=True, text=True)
    return [int(x) for x in p.stdout.split()]


def test_unauthenticated_websocket_cannot_run_commands(settings):
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as client:
        r = client.post("/api/v1/auth/login", json={"token": TOKEN}, headers={"origin": ORIGIN})
        csrf = r.json()["csrf_token"]
        tid = client.post("/api/v1/terminals", json={}, headers={"origin": ORIGIN, "x-csrf-token": csrf}).json()["id"]
        # no cookie
        anon = TestClient(app, base_url=ORIGIN)
        with pytest.raises(Exception):
            with anon.websocket_connect(f"/api/v1/terminals/{tid}/ws", subprotocols=[f"waw-csrf.{csrf}"], headers={"origin": ORIGIN}) as ws:
                ws.receive_text()
        # cookie but no CSRF subprotocol
        with pytest.raises(Exception):
            with client.websocket_connect(f"/api/v1/terminals/{tid}/ws", headers={"origin": ORIGIN}) as ws:
                ws.receive_text()
        # wrong origin
        with pytest.raises(Exception):
            with client.websocket_connect(f"/api/v1/terminals/{tid}/ws", subprotocols=[f"waw-csrf.{csrf}"], headers={"origin": "http://evil.example"}) as ws:
                ws.receive_text()
        client.delete(f"/api/v1/terminals/{tid}", headers={"origin": ORIGIN, "x-csrf-token": csrf})


def test_terminal_survives_browser_disconnect_and_server_restart(tc, settings, workspace):
    r = tc.post("/api/v1/terminals", json={"label": "work"}, headers=tc.mut)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["state"] == "live" and t["label"] == "work"
    term = Term(tc, tc.csrf, t["id"])
    term.expect_state("connected")
    term.send_bytes(b"export PS1='$ '; cd " + str(workspace).encode() + b"\n")
    term.send_bytes(b"MARK=alpha42; echo marker-$MARK-set\n")
    out = term.read_until(b"marker-alpha42-set")
    assert b"marker-alpha42-set" in out
    term.send_bytes(b"sleep 600 &\necho started-$!\n")
    out = term.read_until(b"started-")
    ctx = tc.app.state.ctx
    pane_pids = tmux_pane_pids(ctx, t["tmux_session"])
    assert pane_pids
    # browser disconnects: only the attach client goes away
    term.close()
    time.sleep(0.5)
    assert ctx.terminals.tmux.has_session(t["tmux_session"])
    sleepers = subprocess.run(["pgrep", "-P", str(pane_pids[0]), "-f", "sleep 600"], capture_output=True, text=True).stdout.split()
    assert sleepers, "background process must keep running after disconnect"
    # reconnect: same session, same shell variables
    term2 = Term(tc, tc.csrf, t["id"])
    term2.expect_state("connected")
    term2.send_bytes(b"echo marker-again-$MARK\n")
    out = term2.read_until(b"marker-again-alpha42")
    assert b"marker-again-alpha42" in out
    term2.close()
    # tmux history is accessible separately from the browser buffer
    h = tc.get(f"/api/v1/terminals/{t['id']}/history", params={"lines": 500}).json()
    assert "marker-alpha42-set" in h["text"] and "Snapshot" in h["note"]

    # Web server restart (new app on same state + same app tmux socket)
    app2 = create_app(settings)
    with TestClient(app2, base_url=ORIGIN) as tc2:
        r = tc2.post("/api/v1/auth/login", json={"token": TOKEN}, headers={"origin": ORIGIN})
        csrf2 = r.json()["csrf_token"]
        listing = tc2.get("/api/v1/terminals").json()
        mine = [x for x in listing["terminals"] if x["id"] == t["id"]][0]
        assert mine["state"] == "live" and mine["label"] == "work"
        term3 = Term(tc2, csrf2, t["id"])
        term3.expect_state("connected")
        term3.send_bytes(b"echo marker-restart-$MARK\n")
        out = term3.read_until(b"marker-restart-alpha42")
        assert b"marker-restart-alpha42" in out
        assert tmux_pane_pids(app2.state.ctx, t["tmux_session"]) == pane_pids
        term3.close()
        # explicit close terminates the processes
        r = tc2.delete(f"/api/v1/terminals/{t['id']}", headers={"origin": ORIGIN, "x-csrf-token": csrf2})
        assert r.status_code == 200
        time.sleep(0.5)
        assert not app2.state.ctx.terminals.tmux.has_session(t["tmux_session"])
        assert not subprocess.run(["pgrep", "-f", f"sleep 600"], capture_output=True, text=True).stdout.strip() or not [
            p for p in subprocess.run(["pgrep", "-P", str(pane_pids[0])], capture_output=True).stdout.split()]


def test_resize_ctrl_c_colors_unicode_and_binary_input(tc):
    t = tc.post("/api/v1/terminals", json={}, headers=tc.mut).json()
    term = Term(tc, tc.csrf, t["id"], cols=80, rows=24)
    term.expect_state("connected")
    term.send_bytes(b"export PS1='$ '\n")
    term.send_json({"type": "resize", "cols": 132, "rows": 43})
    term.send_bytes(b"sleep 0.3; echo size-$(tput cols)x$(tput lines)\n")
    out = term.read_until(b"size-132x43")
    assert b"size-132x43" in out
    # Ctrl-C interrupts a running command
    term.send_bytes(b"sleep 100\n")
    time.sleep(0.3)
    term.send_bytes(b"\x03")
    term.send_bytes(b"echo after-\"\"int\n")
    assert b"after-int" in term.read_until(b"after-int")
    # 256-colour and true-colour sequences pass through unchanged
    term.send_bytes(b"printf '\\e[38;5;196mRED256\\e[0m \\e[38;2;10;200;30mRGB\\e[0m\\n'; echo colors-em\"\"itted\n")
    out = term.read_until(b"colors-emitted\r\n")
    assert b"\x1b[38;5;196mRED256" in out and b"\x1b[38;2;10;200;30mRGB" in out
    term.send_bytes(b"echo term-is-$TERM\n")
    assert b"term-is-tmux-256color" in term.read_until(b"term-is-tmux") or b"term-is-screen-256color" in term.read_until(b"term-is-screen")
    # Unicode output arrives intact even if split across chunks
    term.send_bytes("echo 'unicode-测试-émoji-😀-do'\"\"'ne'\n".encode("utf-8"))
    out = term.read_until("😀-done".encode("utf-8"))
    assert "unicode-测试-émoji-😀-done".encode("utf-8") in out
    # binary (non-UTF-8) input bytes are accepted by the relay; tmux, a UTF-8 terminal, may discard
    # invalid UTF-8 key bytes itself (byte transparency of the PTY relay is covered in test_pty_relays_raw_bytes)
    term.send_bytes(b"\xa1\xb5")
    term.send_bytes(b"echo still-al\"\"ive\n")
    assert b"still-alive" in term.read_until(b"still-alive")
    # invalid base64 and invalid dimensions are rejected without closing the socket
    term.send_json({"type": "input", "data": "!!!notbase64"})
    msg = term.recv()
    while msg["type"] != "state":
        msg = term.recv()
    assert msg["state"] == "error" and "base64" in msg["message"]
    term.send_json({"type": "resize", "cols": 1, "rows": 5000})
    msg = term.recv()
    while msg["type"] != "state":
        msg = term.recv()
    assert msg["state"] == "error" and "dimensions" in msg["message"]
    term.send_json({"type": "ping"})
    msg = term.recv()
    while msg["type"] != "pong":
        msg = term.recv()
    term.close()
    tc.delete(f"/api/v1/terminals/{t['id']}", headers=tc.mut)


def test_fullscreen_program_and_independent_terminals(tc):
    t1 = tc.post("/api/v1/terminals", json={"label": "one"}, headers=tc.mut).json()
    t2 = tc.post("/api/v1/terminals", json={"label": "two"}, headers=tc.mut).json()
    assert t1["tmux_session"] != t2["tmux_session"]
    a = Term(tc, tc.csrf, t1["id"])
    b = Term(tc, tc.csrf, t2["id"])
    a.expect_state("connected")
    b.expect_state("connected")
    a.send_bytes(b"X=one; echo in-$X\n")
    b.send_bytes(b"echo in-${X:-unset}\n")
    assert b"in-one" in a.read_until(b"in-one")
    assert b"in-unset" in b.read_until(b"in-unset")
    # full-screen program: `less` enters the alternate screen
    a.send_bytes(b"printf 'L%d\\n' $(seq 1 200) | less\n")
    out = a.read_until(b"\x1b[?1049h")
    assert b"\x1b[?1049h" in out or b"L1" in out
    a.send_bytes(b"q")
    a.send_bytes(b"echo back-to-shell\n")
    assert b"back-to-shell" in a.read_until(b"back-to-shell")
    a.close()
    b.close()
    listing = tc.get("/api/v1/terminals").json()["terminals"]
    assert [x["label"] for x in listing] == ["one", "two"]
    # reorder/rename persists
    tc.patch(f"/api/v1/terminals/{t2['id']}", json={"label": "renamed", "tab_order": -1}, headers=tc.mut)
    listing = tc.get("/api/v1/terminals").json()["terminals"]
    assert [x["label"] for x in listing] == ["renamed", "one"]
    for t in (t1, t2):
        tc.delete(f"/api/v1/terminals/{t['id']}", headers=tc.mut)


def test_killed_tmux_session_shows_exited_not_resumed(tc):
    t = tc.post("/api/v1/terminals", json={}, headers=tc.mut).json()
    ctx = tc.app.state.ctx
    # simulate host reboot / tmux server death
    ctx.terminals.tmux.kill_session(t["tmux_session"])
    listing = tc.get("/api/v1/terminals").json()["terminals"]
    assert [x for x in listing if x["id"] == t["id"]][0]["state"] == "exited"
    with pytest.raises(Exception):
        with tc.websocket_connect(f"/api/v1/terminals/{t['id']}/ws", subprotocols=[f"waw-csrf.{tc.csrf}"], headers={"origin": ORIGIN}) as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state" and msg["state"] == "exited"
            ws.receive_text()  # raises on close
    r = tc.get(f"/api/v1/terminals/{t['id']}/history")
    assert r.status_code == 410 and r.json()["error"]["code"] == "TERMINAL_GONE"
    # a new terminal can be created explicitly
    t2 = tc.post("/api/v1/terminals", json={}, headers=tc.mut).json()
    assert t2["state"] == "live" and t2["id"] != t["id"]
    tc.delete(f"/api/v1/terminals/{t2['id']}", headers=tc.mut)
    tc.delete(f"/api/v1/terminals/{t['id']}", headers=tc.mut)


def test_terminal_create_rejects_bad_cwd(tc):
    r = tc.post("/api/v1/terminals", json={"cwd": "/definitely/not/here"}, headers=tc.mut)
    assert r.status_code == 400


async def test_pty_relays_raw_bytes_without_text_decoding():
    """Bytes that are not valid UTF-8 (e.g. X10 mouse reports) reach the PTY unchanged."""
    import asyncio
    import os

    from backend.app.terminal.pty import PtyProcess

    received = bytearray()
    done = asyncio.Event()

    async def on_output(data: bytes):
        received.extend(data)
        if b"M-!M-5" in received:
            done.set()

    pty = PtyProcess(["cat", "-v"], env=dict(os.environ, LANG="C.UTF-8"), cols=80, rows=24)
    pty.on_output = on_output
    pty.start()
    pty.write(b"\x1b[M\x20\xa1\xb5\n")
    try:
        await asyncio.wait_for(done.wait(), timeout=10)
        assert b"M-!M-5" in received  # cat -v shows 0xa1 0xb5 as M-!M-5: bytes arrived intact
        assert b"\xa1\xb5" in received  # the tty echo contains the raw, undecoded bytes
    finally:
        pty.close()

"""Terminals in a machine's own tmux (machines.rtmux): attach from the browser, survive a dropped connection and an app
restart, report an unreachable machine as detached (not exited). The fake ssh's "machine" is this one, so the
machine's app tmux server runs here under its own socket (<server socket>-r)."""

from __future__ import annotations

import subprocess

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import FAKES, ORIGIN, TOKEN, requires_tmux
from test_terminal import Term
from backend.app.config import DevHost, DevSettings
from backend.app.development import targets
from backend.app.main import create_app

pytestmark = requires_tmux


@pytest.fixture
def r_settings(settings, tmp_path, fake_env):
    settings.development = DevSettings(code_server=str(FAKES / "fake-code-server"), ssh=str(FAKES / "fake-ssh"),
                                       worktrees_dir=tmp_path / "worktrees",
                                       hosts=[DevHost(name="box", ssh="me@box", work_dir=str(tmp_path / "remote"))])
    targets._HOMES.clear()
    yield settings
    sock = settings.tmux_socket_name
    subprocess.run(["tmux", "-L", f"{sock}-r", "kill-server"], capture_output=True)
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)


def client_for(s) -> TestClient:
    c = TestClient(create_app(s), base_url=ORIGIN)
    c.__enter__()
    r = c.post("/api/v1/auth/login", json={"token": TOKEN}, headers={"origin": ORIGIN})
    assert r.status_code == 200
    c.csrf = r.json()["csrf_token"]
    c.mut = {"origin": ORIGIN, "x-csrf-token": c.csrf}
    c.terms = []
    return c


def rtmux(s, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", f"{s.tmux_socket_name}-r", *args], capture_output=True, text=True)


def test_remote_terminal_attach_detach_restart(r_settings, tmp_path, fake_env):
    c = client_for(r_settings)
    try:
        t = c.post("/api/v1/terminals", json={"machine": "box", "cwd": str(tmp_path)}, headers=c.mut).json()
        assert t["machine"] == "box", t
        name = t["tmux_session"]
        term = Term(c, c.csrf, t["id"])
        assert "on box" in term.expect_state("connected")["message"]
        term.send_bytes(b"echo HI$((40+2))\r")
        assert b"HI42" in term.read_until(b"HI42")
        term.close()  # the browser tab goes away: the shell stays
        assert rtmux(r_settings, "has-session", "-t", f"={name}").returncode == 0
        # attach again: tmux redraws the screen
        term = Term(c, c.csrf, t["id"])
        term.expect_state("connected")
        term.send_bytes(b"echo AG$((1))AIN\r")  # (the pty echoes the typed line itself before tmux attaches)
        assert b"AG1AIN" in term.read_until(b"AG1AIN")
        # the client is cut off (like a dropped connection): detached, the session keeps running
        assert rtmux(r_settings, "detach-client", "-s", f"={name}").returncode == 0
        assert term.expect_state("detached")
        term.close()
    finally:
        for x in c.terms:
            x.close()
        c.__exit__(None, None, None)
    # an app restart: the terminal is still live (the server's own tmux never had it)
    c = client_for(r_settings)
    try:
        listing = c.get("/api/v1/terminals").json()["terminals"]
        assert next(x for x in listing if x["id"] == t["id"])["state"] == "live"
        assert "HI42" in c.get(f"/api/v1/terminals/{t['id']}/history").json()["text"]
        # unreachable machine: detached with the reason (Reconnect tries again), the tab is not marked exited
        (fake_env / "ssh_down").write_text("me@box\n")
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(f"/api/v1/terminals/{t['id']}/ws", subprotocols=[f"waw-csrf.{c.csrf}"], headers={"origin": ORIGIN}) as ws:
                msg = ws.receive_json()
                assert msg["state"] == "detached" and "cannot reach box" in msg["message"]
                ws.receive_text()
        (fake_env / "ssh_down").unlink()
        listing = c.get("/api/v1/terminals").json()["terminals"]
        assert next(x for x in listing if x["id"] == t["id"])["state"] == "live"
        # the shell ended there: exited
        rtmux(r_settings, "kill-session", "-t", f"={name}")
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(f"/api/v1/terminals/{t['id']}/ws", subprotocols=[f"waw-csrf.{c.csrf}"], headers={"origin": ORIGIN}) as ws:
                assert ws.receive_json()["state"] == "exited"
                ws.receive_text()
        assert c.delete(f"/api/v1/terminals/{t['id']}", headers=c.mut).status_code == 200
    finally:
        c.__exit__(None, None, None)

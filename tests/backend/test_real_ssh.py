"""The machine layer against a real OpenSSH server (Phase 6): an unprivileged sshd on 127.0.0.1 with its own host key
and an authorized key made for the test. Covers what the fake ssh cannot: host-key scanning and trust with the real
ssh-keyscan and StrictHostKeyChecking, shared connections (ControlMaster), the probe, a workspace on the machine,
commands in the machine's own tmux, recipes and env.sh, folder listing and a benchmark.

Skipped when sshd, ssh, ssh-keyscan, tmux or git is missing, or with WAW_REAL_SSH=0."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from conftest import FAKES, make_settings
from test_bench import SPEC, bench_source, wait_bench
from test_development import new_branch_ws, wait_run
from test_machine_setup import wait_job

from backend.app.config import DevSettings
from backend.app.development import targets

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
pytestmark = pytest.mark.skipif(
    os.environ.get("WAW_REAL_SSH") == "0" or not SSHD or not all(shutil.which(x) for x in ("ssh", "ssh-keyscan", "tmux", "git")),
    reason="needs sshd, ssh, ssh-keyscan, tmux and git (WAW_REAL_SSH=0 skips)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sshd(tmp_path_factory):
    d = tmp_path_factory.mktemp("sshd")
    for k in ("host_key", "client_key"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / k)], check=True)
    (d / "authorized_keys").write_text((d / "client_key.pub").read_text())
    port = _free_port()
    (d / "sshd_config").write_text(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {d}/host_key
AuthorizedKeysFile {d}/authorized_keys
PidFile {d}/sshd.pid
UsePAM no
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowTcpForwarding yes
AllowStreamLocalForwarding yes
StreamLocalBindUnlink yes
""")
    proc = subprocess.Popen([SSHD, "-D", "-f", str(d / "sshd_config"), "-E", str(d / "log")])
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("sshd did not start: " + (d / "log").read_text()[-500:] if (d / "log").exists() else "sshd did not start")
    yield {"port": port, "key": str(d / "client_key"), "dir": d}
    proc.terminate()
    proc.wait(10)


@pytest.fixture(autouse=True)
def clean():
    targets._HOMES.clear()
    yield
    targets._HOMES.clear()


async def test_a_real_machine_end_to_end(running_app, tmp_path, workspace, fake_env, sshd):
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(code_server=str(FAKES / "fake-code-server"), worktrees_dir=tmp_path / "worktrees")
    h = await running_app(s)
    api = h.api
    work = tmp_path / "remote"
    r = await api.post("/api/v1/machines", json={"name": "real", "ssh": "127.0.0.1", "port": sshd["port"],
                                                 "identity_file": sshd["key"], "work_dir": str(work)})
    assert r.status_code == 201, r.text
    from backend.app.machines import rtmux

    rsock = rtmux.remote_socket(h.ctx.terminals.tmux.socket)
    try:
        # a machine added in the app accepts only a host key confirmed here
        chk = (await api.post("/api/v1/machines/real/check")).json()
        assert chk["status"] == "hostkey_unknown", chk
        hops = (await api.get("/api/v1/machines/real/hostkeys")).json()["hops"]
        real_fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(sshd["dir"] / "host_key.pub")],
                                 capture_output=True, text=True, check=True).stdout.split()[1]
        fps = [k["fingerprint"] for k in hops[0]["keys"]]
        assert real_fp in fps, (real_fp, fps)
        r = await api.post("/api/v1/machines/real/hostkeys", json={"hop": "real", "fingerprints": [real_fp]})
        assert r.status_code == 200 and r.json()["status"] == "trusted", r.text
        chk = (await api.post("/api/v1/machines/real/check")).json()
        assert chk["status"] == "online", __import__("json").dumps(chk)
        assert not (Path.home() / ".ssh" / "known_hosts").exists() or "127.0.0.1]:" + str(sshd["port"]) not in \
            (Path.home() / ".ssh" / "known_hosts").read_text()  # the user's own file is left alone
        # the probe
        r = await api.post("/api/v1/machines/real/probe")
        assert r.status_code == 200, r.text
        caps = r.json()
        assert caps["tools"]["git"] and caps["tools"]["tmux"], caps
        # folders there
        (work / "a" / "b").mkdir(parents=True)
        r = await api.get("/api/v1/machines/real/fs/dirs", params={"path": str(work / "a")})
        assert r.status_code == 200 and [x["name"] for x in r.json()["entries"]] == ["b"], r.text
        # a recipe whose env reaches later commands
        r = await api.post("/api/v1/recipes", json={"name": "env", "run": "true", "env_out": "export REAL_SSH_OK=yes"})
        rid = r.json()["id"]
        await api.put("/api/v1/machines/real/recipes", json={"recipes": [rid]})
        log = await wait_job(api, (await api.post("/api/v1/machines/real/setup", json={"bootstrap": None})).json()["id"])
        assert log["job"]["status"] == "passed", log["text"]
        assert "export REAL_SSH_OK=yes" in (work / "env.sh").read_text()
        # a workspace there; a command in the machine's tmux sees the env
        src = bench_source(tmp_path)
        d = await new_branch_ws(api, src, h.ctx.workspace_id, host="real")
        run = (await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks",
                              json={"kind": "custom", "command": 'echo "ok=$REAL_SSH_OK"; tmux -L ' + rsock + ' ls >/dev/null && echo in-tmux'})).json()
        out = await wait_run(api, run["id"], 60)
        assert out["run"]["status"] == "passed" and "ok=yes" in out["text"] and "in-tmux" in out["text"], out["text"]
        # a benchmark there
        await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": d["id"], "spec": SPEC})
        b = await wait_bench(api, (await api.post(f"/api/v1/dev/workspaces/{d['id']}/bench/run", json={"name": "b1"})).json()["id"], 60)
        assert b["status"] == "passed" and b["summary"]["v"]["median"] == 10 and b["machine"] == "real", b
        # commands shared one connection
        cm = h.ctx.settings.development.control_dir
        assert cm is not None and any(cm.iterdir())
    finally:
        await asyncio.to_thread(subprocess.run, ["tmux", "-L", rsock, "kill-server"], capture_output=True)

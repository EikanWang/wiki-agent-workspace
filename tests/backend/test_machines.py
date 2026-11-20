"""Machines: the registry (config hosts + machines added in the app), ssh command lines (shared connections, bastion
chains), hop-by-hop checks, host keys, the capability probe, SSH keys and ssh-copy-id, terminals on a machine.
A fake ssh runs the commands on this machine (tests/fakes/fake-ssh)."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import FAKES, make_settings, requires_tmux

from backend.app.config import DevHost, DevSettings
from backend.app.development import targets
from backend.app.machines import conn, health, probe


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    targets._HOMES.clear()
    yield h
    targets._HOMES.clear()


def make_dev(tmp_path: Path, hosts: list[DevHost] | None = None) -> DevSettings:
    return DevSettings(code_server=str(FAKES / "fake-code-server"), ssh=str(FAKES / "fake-ssh"), worktrees_dir=tmp_path / "worktrees",
                       ssh_keyscan=str(FAKES / "fake-ssh-keyscan"), ssh_copy_id=str(FAKES / "fake-ssh-copy-id"),
                       hosts=hosts or [])


@pytest.fixture
def m_settings(tmp_path, workspace):
    s = make_settings(tmp_path, workspace)
    s.development = make_dev(tmp_path, [
        DevHost(name="cfgbox", ssh="me@cfgbox", work_dir=str(tmp_path / "remote")),
        DevHost(name="cfgjump", ssh="cfgjump.example", kind="bastion"),
    ])
    return s


def calls(state: Path, name: str = "ssh_calls.log") -> list[list[str]]:
    f = state / name
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


async def add(api, **body) -> dict:
    r = await api.post("/api/v1/machines", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------------------------- command lines


def test_ssh_argv_bastion_chain_and_shared_connections(tmp_path, fake_env):
    dev = make_dev(tmp_path)
    dev.control_dir = tmp_path / "cm"
    dev.known_hosts = tmp_path / "kh"
    b1 = DevHost(name="b1", ssh="b1.example", kind="bastion", user="jo", strict_host_keys=True)
    b2 = DevHost(name="b2", ssh="b2.example", kind="bastion", port=2222, strict_host_keys=True)
    m = DevHost(name="m", ssh="m.internal", via=["b1", "b2"], identity_file="~/.ssh/k", options={"Compression": "yes"},
                strict_host_keys=True)
    dev.ui_hosts = [b1, b2, m]
    argv = conn.ssh_argv(dev, m, ["-tt"])
    assert argv[0] == dev.ssh and argv[-2:] == ["--", "m.internal"]
    assert "ControlMaster=auto" in argv and f"ControlPersist={conn.CONTROL_PERSIST}" in argv
    assert "StrictHostKeyChecking=yes" in argv and f"UserKnownHostsFile=~/.ssh/known_hosts {tmp_path / 'kh'}" in argv
    assert argv[argv.index("-i") + 1] == "~/.ssh/k" and "Compression=yes" in argv
    pc = next(a for a in argv if a.startswith("ProxyCommand="))[len("ProxyCommand="):]
    outer = shlex.split(pc)
    assert outer[-4:] == ["-W", "%h:%p", "--", "b2.example"] and "2222" in outer
    inner = next(a for a in outer if a.startswith("ProxyCommand="))
    assert "-W %%h:%%p -- b1.example" in inner  # escaped once: expanded by the b2 hop, not by the machine's ssh
    # each hop has a shared connection of its own
    paths = {a for a in [*argv, *outer, *shlex.split(inner[len("ProxyCommand="):])] if a.startswith("ControlPath=")}
    assert len(paths) == 3
    # port forwards get a connection of their own
    fw = conn.ssh_argv(dev, m, ["-L", "/a:/b"])
    assert "ControlPath=none" in fw and "ControlMaster=auto" not in fw
    # the fake ssh runs the ProxyCommands like OpenSSH: each hop is asked to reach the next one
    r = subprocess.run([*conn.ssh_argv(dev, m), "echo hi"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "hi", r.stderr
    ws = [c[c.index("-W") + 1:] for c in calls(fake_env) if "-W" in c]
    assert ["m.internal:22", "--", "b2.example"] in ws and ["b2.example:2222", "--", "b1.example"] in ws
    # a broken first hop breaks the path
    (fake_env / "ssh_down").write_text("b1.example\n")
    r = subprocess.run([*conn.ssh_argv(dev, m), "echo hi"], capture_output=True, text=True)
    assert r.returncode == 255


def test_control_dir_short_paths(tmp_path):
    assert conn.control_dir_for(Path("/s")) == Path("/s/ssh/cm")
    deep = conn.control_dir_for(tmp_path / ("x" * 80))
    assert len(str(deep)) < 60 and str(os.getuid()) in deep.name


def test_classify_errors():
    assert health.classify(255, "Permission denied (publickey).")[0] == "auth_failed"
    assert health.classify(255, "Host key verification failed.")[0] == "hostkey_unknown"
    assert health.classify(255, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@")[0] == "hostkey_changed"
    assert health.classify(255, "ssh: Could not resolve hostname x: Name or service not known")[0] == "dns"
    assert health.classify(255, "ssh: connect to host x port 22: Connection timed out")[0] == "timeout"
    assert health.classify(255, "ssh: connect to host x port 22: Connection refused")[0] == "refused"
    assert health.classify(255, "kex_exchange_identification: Connection closed by remote host")[0] == "unreachable"
    assert health.classify(255, "something else")[0] == "failed"


# ---------------------------------------------------------------------------------------------- probe parsing

SAMPLE = """@@uname
Linux
6.8.0-45-generic
x86_64
gpu-node-3
@@os
Ubuntu 22.04.4 LTS
@@nproc
224
@@lscpu
Model name:                         Intel(R) Xeon(R) Platinum 8480+
Thread(s) per core:                 2
Core(s) per socket:                 56
Socket(s):                          2
NUMA node(s):                       2
@@cpuinfo
model name	: Intel(R) Xeon(R) Platinum 8480+
@@mem
MemTotal:       1056426456 kB
@@disk
/home/me
/dev/nvme0n1p2  1844244104 1000 900000000  52% /
@@home
/home/me
@@shell
/bin/bash
@@sudo
passwordless
@@pm
apt-get
@@gnufind
find (GNU findutils) 4.8.0
@@tool git
/usr/bin/git
git version 2.34.1
@@tool tar
/usr/bin/tar
tar (GNU tar) 1.34
@@tool tmux
/usr/bin/tmux
tmux 3.2a
@@tool cmake
@@nvidia
0, NVIDIA H100 80GB HBM3, GPU-1111, 00000000:1B:00.0, 550.54.15, 81559 MiB
1, NVIDIA H100 80GB HBM3, GPU-2222, 00000000:2B:00.0, 550.54.15, 81559 MiB
@@xpu
{"device_list": [{"device_id": 0, "device_name": "Intel(R) Data Center GPU Max 1550", "uuid": "u-0", "pci_bdf_address": "0000:3a:00.0", "memory_physical_size_byte": "137438953472"}]}
@@rocm
@@lspci
0000:1b:00.0 "3D controller" "NVIDIA Corporation" "GH100 [H100 SXM5 80GB]" -ra1 "NVIDIA Corporation" "Device 16c1"
0000:3a:00.0 "Display controller" "Intel Corporation" "Ponte Vecchio XT (2 Tile) [Data Center GPU Max 1550]" "Intel Corporation" "Device 0000"
0000:9a:00.0 "Display controller" "Intel Corporation" "Device e20b" "Intel Corporation" "Device 1100"
0000:03:00.0 "VGA compatible controller" "ASPEED Technology, Inc." "ASPEED Graphics Family" -r52 "ASPEED" "ASPEED"
@@drivers
xe
nvidia 550.54.15
@@end
"""


def test_probe_parse():
    caps = probe.parse(SAMPLE, DevHost(name="g", ssh="g"))
    assert caps["complete"] and caps["os"]["hostname"] == "gpu-node-3" and caps["os"]["pretty"].startswith("Ubuntu")
    assert caps["cpu"] == {"model": "Intel(R) Xeon(R) Platinum 8480+", "threads": 224, "sockets": 2, "cores_per_socket": 56,
                           "threads_per_core": 2, "numa_nodes": 2}
    assert caps["memory_kb"] == 1056426456 and caps["disk"]["free_kb"] == 900000000
    assert caps["sudo"] == "passwordless" and caps["package_manager"] == "apt-get" and caps["gnu_find"]
    assert caps["tools"]["git"]["version"] == "2.34.1" and caps["tools"]["tmux"]["version"] is not None
    assert not caps["tools"]["cmake"]["ok"] and caps["missing_required"] == []
    gpus = caps["gpus"]
    # nvidia-smi and xpu-smi devices once each (their lspci lines are skipped), an Intel card without xpu-smi from lspci,
    # the BMC's graphics left out
    assert [(g["vendor"], g["source"]) for g in gpus] == [("nvidia", "nvidia-smi"), ("nvidia", "nvidia-smi"), ("intel", "xpu-smi"),
                                                        ("intel", "lspci")]
    assert gpus[0]["pci"] == "0000:1b:00.0" and gpus[2]["name"] == "Intel(R) Data Center GPU Max 1550"


def test_probe_parse_incomplete():
    caps = probe.parse("@@uname\nLinux\n", DevHost(name="g", ssh="g"))
    assert not caps["complete"] and "git" in caps["missing_required"] and "GNU find" in caps["missing_required"]


# ---------------------------------------------------------------------------------------------- API


async def test_machines_crud_and_validation(running_app, m_settings, tmp_path, fake_env):
    h = await running_app(m_settings)
    api = h.api
    ms = (await api.get("/api/v1/machines")).json()
    assert [m["name"] for m in ms["machines"]] == ["local", "cfgbox", "cfgjump"]
    assert ms["machines"][1]["source"] == "config" and not ms["machines"][1]["editable"] and "ProxyCommand" not in ms["allowed_options"]
    await add(api, name="jumpui", kind="bastion", ssh="jumpui.example", user="ops", port=2200)
    m1 = await add(api, name="m1", ssh="m1.internal", via=["jumpui"], options={"serveraliveinterval": "15"}, work_dir="/data/me")
    assert m1["source"] == "ui" and m1["editable"] and m1["options"] == {"ServerAliveInterval": "15"} and m1["strict_host_keys"]
    assert "ProxyCommand=" in m1["ssh_command"] and "jumpui.example" in m1["ssh_command"]
    bad = [
        ({"name": "local", "ssh": "x"}, "INVALID_MACHINE"),
        ({"name": "x y", "ssh": "x"}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "-oProxyCommand=evil"}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "x", "options": {"ProxyCommand": "nc %h %p"}}, "SSH_OPTION_NOT_ALLOWED"),
        ({"name": "x", "ssh": "x", "options": {"LocalCommand": "id"}}, "SSH_OPTION_NOT_ALLOWED"),
        ({"name": "x", "ssh": "x", "options": {"HostName": "a b"}}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "x", "via": ["m1"]}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "x", "via": ["nope"]}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "x", "kind": "bastion", "via": ["jumpui"]}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "x", "identity_file": "/nonexistent/key"}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "x", "work_dir": "relative"}, "INVALID_MACHINE"),
        ({"name": "x", "ssh": "x", "port": 70000}, "INVALID_MACHINE"),
    ]
    for body, code in bad:
        r = await api.post("/api/v1/machines", json=body)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (body, r.text)
    r = await api.post("/api/v1/machines", json={"name": "cfgbox", "ssh": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MACHINE_EXISTS"
    # config hosts are read-only here
    r = await api.patch("/api/v1/machines/cfgbox", json={"ssh": "other"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MACHINE_READ_ONLY"
    assert (await api.delete("/api/v1/machines/cfgbox")).status_code == 409
    # the rest of the app sees the app's machines (not bastions) next to the config ones
    hosts = [x["name"] for x in (await api.get("/api/v1/dev/info")).json()["hosts"]]
    assert "m1" in hosts and "cfgbox" in hosts and "jumpui" not in hosts and "cfgjump" not in hosts
    assert h.ctx.settings.development.host("m1").via == ["jumpui"]
    # edit; no renames
    r = await api.patch("/api/v1/machines/m1", json={"port": 2022, "via": []})
    assert r.status_code == 200 and r.json()["port"] == 2022 and r.json()["via"] == []
    assert (await api.patch("/api/v1/machines/m1", json={"name": "m2"})).status_code == 400
    await api.patch("/api/v1/machines/m1", json={"via": ["jumpui"]})
    # a bastion in use cannot go
    r = await api.delete("/api/v1/machines/jumpui")
    assert r.status_code == 409 and r.json()["error"]["code"] == "MACHINE_IN_USE"
    assert (await api.delete("/api/v1/machines/m1")).status_code == 200
    assert (await api.delete("/api/v1/machines/jumpui")).status_code == 200
    assert (await api.get("/api/v1/machines/m1")).status_code == 404
    assert h.ctx.settings.development.host("m1") is None
    assert (await api.delete("/api/v1/machines/local")).status_code == 400


async def test_machines_survive_restart_and_config_mirror(running_app, m_settings, tmp_path, fake_env):
    h = await running_app(m_settings)
    await add(h.api, name="m1", ssh="m1.internal")
    await h.stop()
    m_settings.development.hosts = [x for x in m_settings.development.hosts if x.name != "cfgjump"]
    m_settings.development.ui_hosts = []
    h2 = await running_app(m_settings)
    names = [m["name"] for m in (await h2.api.get("/api/v1/machines")).json()["machines"]]
    assert names == ["local", "cfgbox", "m1"]
    assert h2.ctx.settings.development.host("m1") is not None


async def test_check_hop_by_hop(running_app, m_settings, tmp_path, fake_env):
    api = (await running_app(m_settings)).api
    await add(api, name="b", kind="bastion", ssh="b.example")
    await add(api, name="m1", ssh="m1.internal", via=["b"])
    r = (await api.post("/api/v1/machines/m1/check")).json()
    assert r["ok"] and r["status"] == "online" and [x["name"] for x in r["hops"]] == ["b", "m1"] and all(x["ok"] for x in r["hops"])
    assert (await api.get("/api/v1/machines/m1")).json()["status"] == "online"
    # the machine's own login is refused
    await add(api, name="m2", ssh="authfail.internal", via=["b"])
    r = (await api.post("/api/v1/machines/m2/check")).json()
    assert r["status"] == "auth_failed" and r["failed_at"] == "m2" and r["hops"][0]["ok"] and r["summary"].startswith("the machine")
    # the bastion is down: it is named, the machine behind it is skipped
    await add(api, name="b2", kind="bastion", ssh="unreachable-b.example")
    await add(api, name="m3", ssh="m3.internal", via=["b2"])
    r = (await api.post("/api/v1/machines/m3/check")).json()
    assert r["status"] == "refused" and r["failed_at"] == "b2" and r["hops"][1]["status"] == "skipped"
    assert "bastion b2" in r["summary"]
    # host key not trusted
    await add(api, name="m4", ssh="badkey.internal")
    assert (await api.post("/api/v1/machines/m4/check")).json()["status"] == "hostkey_unknown"
    assert (await api.post("/api/v1/machines/local/check")).json()["ok"]


async def test_hostkeys_scan_and_trust(running_app, m_settings, tmp_path, fake_env):
    h = await running_app(m_settings)
    api = h.api
    await add(api, name="b", kind="bastion", ssh="b.example")
    await add(api, name="m1", ssh="m1.internal", via=["b"], port=2201)
    hops = (await api.get("/api/v1/machines/m1/hostkeys")).json()["hops"]
    assert [x["name"] for x in hops] == ["b", "m1"] and all(x["status"] == "unknown" for x in hops)
    assert hops[0]["scanned"] == "from this server" and hops[1]["scanned"] == "from bastion b"
    assert hops[1]["known_name"] == "[m1.internal]:2201"
    # the machine behind the bastion is scanned from there (ssh-keyscan runs through the bastion's ssh)
    assert any("-W" not in c and "b.example" in c and "ssh-keyscan" in " ".join(c) for c in calls(fake_env))
    fp = hops[1]["keys"][0]["fingerprint"]
    assert fp.startswith("SHA256:")
    r = await api.post("/api/v1/machines/m1/hostkeys", json={"hop": "m1", "fingerprints": ["SHA256:wrong"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "HOSTKEY_MISMATCH"
    r = await api.post("/api/v1/machines/m1/hostkeys", json={"hop": "m1", "fingerprints": [fp]})
    assert r.status_code == 200 and r.json()["status"] == "trusted", r.text
    kh = h.ctx.settings.development.known_hosts
    assert kh.read_text().startswith("[m1.internal]:2201 ssh-ed25519 ") and stat.S_IMODE(kh.stat().st_mode) == 0o600
    assert not (Path.home() / ".ssh" / "known_hosts").exists()  # the user's own file is left alone
    # the key changes: shown as changed; trusting the new one replaces the old
    (fake_env / "hostkey_seed").write_text("v2")
    hops = (await api.get("/api/v1/machines/m1/hostkeys")).json()["hops"]
    assert hops[1]["status"] == "changed" and hops[1]["keys"][0]["state"] == "changed"
    r = await api.post("/api/v1/machines/m1/hostkeys", json={"hop": "m1", "fingerprints": [hops[1]["keys"][0]["fingerprint"]]})
    assert r.json()["status"] == "trusted" and len([x for x in kh.read_text().splitlines() if x.startswith("[m1")]) == 1
    # config hosts keep the user's own ssh settings
    r = await api.post("/api/v1/machines/cfgbox/hostkeys", json={"hop": "cfgbox", "fingerprints": [fp]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MACHINE_READ_ONLY"
    r = await api.post("/api/v1/machines/m1/hostkeys", json={"hop": "cfgbox", "fingerprints": [fp]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_HOP"
    res = (await api.get("/api/v1/machines/m1/resolve")).json()
    assert res["hostname"] == "m1.internal" and res["port"] == 2201


async def test_probe_machine_and_server(running_app, m_settings, tmp_path, fake_env):
    api = (await running_app(m_settings)).api
    await add(api, name="m1", ssh="m1.internal", work_dir=str(tmp_path / "remote-m1"))
    caps = (await api.post("/api/v1/machines/m1/probe")).json()
    assert caps["complete"], caps
    assert caps["os"]["kernel"] == "Linux" and caps["tools"]["git"]["ok"] and caps["cpu"]["threads"] >= 1
    assert caps["work_dir"] == str(tmp_path / "remote-m1") and caps["disk"]["free_kb"]
    assert (await api.get("/api/v1/machines/m1")).json()["capabilities"]["os"]["kernel"] == "Linux"
    local = (await api.post("/api/v1/machines/local/probe")).json()
    assert local["complete"] and (await api.get("/api/v1/machines/local")).json()["capabilities"]["os"]["kernel"] == "Linux"
    await add(api, name="b", kind="bastion", ssh="b.example")
    assert (await api.post("/api/v1/machines/b/probe")).json()["error"]["code"] == "MACHINE_IS_BASTION"
    await add(api, name="dead", ssh="unreachable.internal")
    r = await api.post("/api/v1/machines/dead/probe")
    assert r.status_code == 502 and r.json()["error"]["code"] == "DEV_HOST_UNREACHABLE"


async def test_ssh_keys(running_app, m_settings, home):
    api = (await running_app(m_settings)).api
    assert (await api.get("/api/v1/ssh-keys")).json()["keys"] == []
    r = await api.post("/api/v1/ssh-keys")
    assert r.status_code == 201, r.text
    k = r.json()
    assert k["type"] == "ssh-ed25519" and k["identity_file"] == str(home / ".ssh" / "id_ed25519") and k["fingerprint"].startswith("SHA256:")
    assert stat.S_IMODE((home / ".ssh" / "id_ed25519").stat().st_mode) == 0o600
    assert (await api.post("/api/v1/ssh-keys")).json()["error"]["code"] == "SSH_KEY_EXISTS"
    assert len((await api.get("/api/v1/ssh-keys")).json()["keys"]) == 1


async def _wait_file(p: Path, timeout: float = 10) -> None:
    for _ in range(int(timeout * 20)):
        if p.exists() and p.read_text().strip():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{p} not written")


@requires_tmux
async def test_install_key_in_terminal(running_app, m_settings, home, fake_env):
    api = (await running_app(m_settings)).api
    assert (await api.post("/api/v1/machines/cfgbox/install-key", json={})).json()["error"]["code"] == "NO_SSH_KEY"
    await api.post("/api/v1/ssh-keys")
    await add(api, name="b", kind="bastion", ssh="b.example")
    await add(api, name="m1", ssh="m1.internal", via=["b"], user="dev", port=2201)
    r = await api.post("/api/v1/machines/m1/install-key", json={})
    assert r.status_code == 201, r.text
    assert r.json()["terminal"]["label"] == "Install key on m1"
    await _wait_file(fake_env / "copy_id_calls.log")
    args = calls(fake_env, "copy_id_calls.log")[-1]
    assert args[:2] == ["-i", str(home / ".ssh" / "id_ed25519.pub")] and args[-1] == "dev@m1.internal"
    assert "-p" in args and "ControlPath=none" in args and any(a.startswith("ProxyCommand=") and "b.example" in a for a in args)
    assert "StrictHostKeyChecking=yes" in args
    # the bastion on the path
    r = await api.post("/api/v1/machines/m1/install-key", json={"hop": "b"})
    assert r.status_code == 201
    r = await api.post("/api/v1/machines/m1/install-key", json={"public_key_file": "/etc/passwd"})
    assert r.json()["error"]["code"] == "INVALID_KEY"


@requires_tmux
async def test_terminal_on_machine(running_app, m_settings, tmp_path, fake_env):
    h = await running_app(m_settings)
    api = h.api
    await add(api, name="m1", ssh="m1.internal")
    r = await api.post("/api/v1/terminals", json={"machine": "m1", "cwd": str(tmp_path)})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["label"] == "m1" and t["machine"] == "m1" and t["cwd"] == str(tmp_path)
    # the session lives in the machine's own app tmux (the fake ssh's machine is this one), not in the server's
    rsock = f"{h.ctx.terminals.tmux.socket}-r"
    assert subprocess.run(["tmux", "-L", rsock, "has-session", "-t", f"={t['tmux_session']}"]).returncode == 0
    assert t["tmux_session"] not in h.ctx.terminals.tmux.list_sessions()
    conf = Path(os.environ["HOME"]) / ".cache" / "wiki-agent-workspace" / "tmux" / f"{rsock}.conf"
    assert "history-limit" in conf.read_text() and stat.S_IMODE(conf.stat().st_mode) == 0o600
    # the server's tmux check at listing time leaves it alone; history comes from the machine
    listing = (await api.get("/api/v1/terminals")).json()["terminals"]
    assert next(x for x in listing if x["id"] == t["id"])["state"] == "live"
    hist = await api.get(f"/api/v1/terminals/{t['id']}/history")
    assert hist.status_code == 200 and "on m1" in hist.json()["note"]
    # unreachable: closing still works (with a warning), the session may still run there
    (fake_env / "ssh_down").write_text("m1.internal\n")
    r = await api.get(f"/api/v1/terminals/{t['id']}/history")
    assert r.status_code == 502 and r.json()["error"]["code"] == "DEV_HOST_UNREACHABLE"
    (fake_env / "ssh_down").unlink()
    r = await api.delete(f"/api/v1/terminals/{t['id']}")
    assert r.status_code == 200 and "warning" not in r.json()
    assert subprocess.run(["tmux", "-L", rsock, "has-session", "-t", f"={t['tmux_session']}"], capture_output=True).returncode != 0
    r = await api.post("/api/v1/terminals", json={"machine": "m1", "cwd": str(tmp_path / "missing")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CWD_NOT_FOUND"
    r = await api.post("/api/v1/terminals", json={"machine": "nope"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "DEV_HOST_UNKNOWN"
    assert (await api.post("/api/v1/terminals", json={"machine": "local"})).status_code == 201


@requires_tmux
async def test_terminal_on_machine_without_tmux(running_app, m_settings, tmp_path, fake_env, monkeypatch):
    """No tmux there: the ssh login runs in this server's tmux, as before (it ends with the connection)."""
    from backend.app.machines import rtmux

    monkeypatch.setattr(rtmux.RemoteTmux, "info", lambda self, refresh=False: rtmux.RemoteInfo(False, reason="no tmux"))
    h = await running_app(m_settings)
    api = h.api
    await add(api, name="m2", ssh="m2.internal")
    r = await api.post("/api/v1/terminals", json={"machine": "m2", "cwd": str(tmp_path)})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["machine"] is None and t["tmux_session"] in h.ctx.terminals.tmux.list_sessions()
    for _ in range(100):
        if any("-tt" in c and "m2.internal" in c for c in calls(fake_env)):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(calls(fake_env))


def test_remote_tmux_conf_follows_the_version():
    from backend.app.machines import rtmux

    old = rtmux.conf_text("s", (2, 1), "screen", 5000)
    new = rtmux.conf_text("s", (3, 4), "tmux-256color", 5000)
    assert "window-size" not in old and "terminal-features" not in old and "focus-events" not in old
    assert "window-size latest" in new and "terminal-features" in new and 'default-terminal "tmux-256color"' in new
    assert "allow-passthrough on" in new and "allow-passthrough" not in rtmux.conf_text("s", (3, 2), "screen", 5000)
    assert rtmux.parse_version("tmux 3.2a") == (3, 2) and rtmux.parse_version("tmux next-3.5") == (3, 5)
    assert rtmux.parse_version("") is None


# ---------------------------------------------------------------------------------------------- real OpenSSH


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sshd_knows(sshd_bin: str, option: str) -> bool:
    r = subprocess.run([sshd_bin, "-T", "-f", "/dev/null", "-o", f"{option}=no"], capture_output=True, text=True)
    return "Bad configuration option" not in r.stderr and "unsupported option" not in r.stderr.lower()


@pytest.fixture
def sshd(tmp_path):
    """A throwaway sshd for the current user on a free port (key login only); skipped when OpenSSH is missing."""
    import shutil
    import time

    sshd_bin = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
    if not sshd_bin or not shutil.which("ssh") or not shutil.which("ssh-keygen"):
        pytest.skip("OpenSSH (ssh, sshd, ssh-keygen) not installed")
    d = tmp_path / "sshd"
    d.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / "host_key")], check=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / "client_key")], check=True)
    (d / "authorized_keys").write_text((d / "client_key.pub").read_text())
    os.chmod(d / "authorized_keys", 0o600)
    port = _free_port()
    (d / "sshd_config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d / 'host_key'}\nPidFile {d / 'sshd.pid'}\n"
        f"AuthorizedKeysFile {d / 'authorized_keys'}\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "UsePAM no\nStrictModes no\nAllowTcpForwarding yes\nPermitUserEnvironment no\nMaxStartups 100:30:200\n"
        # many short connections from one address in a row (host-key refusals, keyscans): no penalty box
        + ("PerSourcePenalties no\n" if _sshd_knows(sshd_bin, "PerSourcePenalties") else ""))
    proc = subprocess.Popen([sshd_bin, "-D", "-e", "-f", str(d / "sshd_config")], stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if proc.poll() is not None:
            pytest.skip(f"sshd does not start here: {proc.stderr.read()[-300:]}")
        try:
            import socket

            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield {"port": port, "key": str(d / "client_key"), "host_pub": (d / "host_key.pub").read_text().split()}
    proc.terminate()
    proc.wait(timeout=10)


def test_real_openssh_chain_mux_and_strict_keys(tmp_path, sshd):
    """The command lines work with real OpenSSH: a strict host-key check against the app's known_hosts, a shared
    connection, and a machine reached through a bastion (here the same sshd under two names)."""
    import getpass
    import time

    dev = DevSettings()
    dev.control_dir = tmp_path / "cm"
    dev.control_dir.mkdir(mode=0o700)
    dev.known_hosts = tmp_path / "known_hosts"
    p, key = sshd["port"], sshd["key"]
    me = getpass.getuser()
    b = DevHost(name="b", ssh="127.0.0.1", kind="bastion", port=p, user=me, identity_file=key, strict_host_keys=True)
    m = DevHost(name="m", ssh="localhost", via=["b"], port=p, user=me, identity_file=key, strict_host_keys=True,
                options={"HostKeyAlias": "waw-test-machine"})
    dev.ui_hosts = [b, m]

    def run(h, cmd="echo ok"):
        return subprocess.run([*conn.ssh_argv(dev, h), cmd], capture_output=True, text=True, timeout=60)

    # unknown host key: refused (strict), nothing written anywhere
    r = run(b)
    assert r.returncode == 255 and health.classify(r.returncode, r.stderr)[0] == "hostkey_unknown", r.stderr
    typ, blob = sshd["host_pub"][:2]
    dev.known_hosts.write_text(f"[127.0.0.1]:{p} {typ} {blob}\n")
    r = run(b)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
    # the shared connection is up and reused
    assert os.path.exists(conn.control_path(dev, b, []))
    started = time.monotonic()
    assert run(b).returncode == 0
    assert time.monotonic() - started < 5
    # the machine through the bastion: its own key (by alias) must be trusted too
    r = run(m)
    assert r.returncode == 255 and "Host key verification failed" in r.stderr, r.stderr
    with dev.known_hosts.open("a") as fh:
        fh.write(f"waw-test-machine {typ} {blob}\n")  # a HostKeyAlias is looked up without the port
    r = run(m, "echo through")
    assert r.returncode == 0 and r.stdout.strip() == "through", r.stderr
    # a two-hop chain (bastion behind bastion) — the %h/%p escaping of nested ProxyCommands
    b2 = DevHost(name="b2", ssh="localhost", kind="bastion", port=p, user=me, identity_file=key, strict_host_keys=True,
                 options={"HostKeyAlias": "waw-test-machine"})
    m2 = DevHost(name="m2", ssh="127.0.0.1", via=["b", "b2"], port=p, user=me, identity_file=key, strict_host_keys=True)
    dev.ui_hosts = [b, b2, m, m2]
    r = run(m2, "echo twohops")
    assert r.returncode == 0 and r.stdout.strip() == "twohops", r.stderr
    # real ssh-keyscan, run on the bastion for the machine behind it, finds the keys trusted above
    from backend.app.machines import hostkeys

    scanned = asyncio.run(hostkeys.scan(dev, m, [b]))
    assert scanned["status"] == "trusted" and scanned["known_name"] == "waw-test-machine" and scanned["scanned"] == "from bastion b"
    dev.known_hosts.write_text("")
    scanned = asyncio.run(hostkeys.scan(dev, b, []))
    assert scanned["status"] == "unknown"
    res = asyncio.run(hostkeys.trust(dev, b, "b", [k["fingerprint"] for k in scanned["keys"]]))
    assert res["status"] == "trusted" and run(b).returncode == 0
    for h in (m2, m, b2, b):
        asyncio.run(conn.close_master(dev, h))
    assert not os.path.exists(conn.control_path(dev, b, []))

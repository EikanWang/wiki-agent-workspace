"""Machine setup (Phase 3): what a machine gets from this server — the env script's variables, the proxy, a CA bundle —
always through 0600 files, never on a command line; tasks, terminals, the editor and agent turns get it; forwarded
secrets are masked in logs; <work_dir>/bin first on PATH; agent turns on a machine without the CLI on this server;
setup installs the app's tools from a release server (or streams them when the machine is offline) and copies the
opencode settings without secrets. The fake ssh runs commands on this machine (tests/fakes/fake-ssh) with only an
sshd-like environment (ssh_clean_env), so whatever the command sees was forwarded."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import FAKES, make_settings, requires_tmux
from test_development import make_source, new_branch_ws, wait_editor, wait_run

from backend.app.agents.base import configure_env_script
from backend.app.config import DevHost, DevSettings
from backend.app.development import targets
from backend.app.machines import bootstrap, envfwd

TOKEN_VALUE = "tok-SECRET-0123456789"
PROXY_WITH_CREDS = "http://alice:pr0xy-pass@proxy.example:3128"
CA_TEXT = "-----BEGIN CERTIFICATE-----\nFAKE-EXTRA-CA\n-----END CERTIFICATE-----\n"
PROXY_NAMES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    for n in PROXY_NAMES:
        monkeypatch.delenv(n, raising=False)
    targets._HOMES.clear()
    envfwd._CA_SENT.clear()
    yield h
    targets._HOMES.clear()
    configure_env_script(None)


def _all_ssh_args(state: Path) -> str:
    f = state / "ssh_calls.log"
    return f.read_text() if f.exists() else ""


# ---------------------------------------------------------------------------------------------- units
def test_sh_path_and_path_prefix():
    assert targets.sh_path("~/.cache/w", "bin") == '"$HOME"/.cache/w/bin'
    assert targets.sh_path("~") == '"$HOME"'
    assert targets.sh_path("/opt/my work", "run") == "'/opt/my work/run'"
    out = subprocess.run(["sh", "-c", targets.path_prefix("/opt/my work") + 'printf %s "$PATH"'], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "HOME": "/h"}).stdout
    assert out == "/opt/my work/bin:/usr/bin:/bin"


def test_collect_allowlist_proxy_and_secrets(tmp_path, monkeypatch):
    script = tmp_path / "env.sh"
    script.write_text(f"export WAW_TEST_TOKEN={TOKEN_VALUE}\nexport SHORT=abc\nexport PATH=/somewhere:$PATH\n"
                      "export LD_LIBRARY_PATH=/x\n")
    configure_env_script(str(script))
    monkeypatch.setenv("https_proxy", PROXY_WITH_CREDS)
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("no_proxy", ".corp.example")
    monkeypatch.setenv("FROM_SERVER_ENV", "server-value-12345")
    h = DevHost(name="m", ssh="m", forward_env=True)
    f = envfwd.collect(h)
    assert f.env == {"WAW_TEST_TOKEN": TOKEN_VALUE, "SHORT": "abc"}  # PATH and LD_* are this server's own
    assert f.secrets == [TOKEN_VALUE]  # short values are not masked (too many false hits)
    f = envfwd.collect(DevHost(name="m", ssh="m", forward_env=True, env_vars=["PATH", "FROM_SERVER_ENV", "MISSING"]))
    assert f.env["FROM_SERVER_ENV"] == "server-value-12345" and f.env["PATH"].startswith("/somewhere:") and "MISSING" not in f.env
    f = envfwd.collect(DevHost(name="m", ssh="m", forward_proxy=True))
    assert f.env["https_proxy"] == PROXY_WITH_CREDS and f.env["http_proxy"] == "http://proxy.example:3128"
    assert f.env["no_proxy"] == ".corp.example,localhost,127.0.0.1,::1" and f.env["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert f.secrets == [PROXY_WITH_CREDS]
    assert not envfwd.collect(DevHost(name="m", ssh="m"))  # opt-in: nothing by default
    text = envfwd.file_text(DevHost(name="m", ssh="m", work_dir="~/w"), envfwd.Forwarded(env={"A": "it's"}, ca=script))
    assert "export A='it'\"'\"'s'" in text and 'export SSL_CERT_FILE="$HOME"/w/ca.pem' in text


def test_mask_holds_back_a_partial_secret():
    s = ["supersecretvalue"]
    data, used = envfwd.mask_bytes(b"line one\nkey=supersec", s, final=False)
    assert data == b"line one\nkey=" and used == len(b"line one\nkey=")
    data, used = envfwd.mask_bytes(b"supersecretvalue done\n", s, final=False)
    assert data == b"**** done\n" and used == len(b"supersecretvalue done\n")
    assert envfwd.mask_bytes(b"x supersec", s, final=True)[0] == b"x supersec"


def test_sanitized_opencode_config(tmp_path):
    f = tmp_path / "opencode.jsonc"
    f.write_text('''{
      // comments and trailing commas are fine
      "$schema": "https://opencode.ai/config.json",
      "model": "bedrock/claude", /* inline */
      "provider": {
        "bedrock": {"options": {"region": "us-east-1", "apiKey": "sk-live-123", "bearer": "{env:AWS_BEARER_TOKEN_BEDROCK}",
                                "headers": {"X-Api-Key": "hdr-secret", "X-Trace": "on"}, "url": "https://x//y"}},
        "other": {"options": {"token": "{file:~/.secrets/t}"}},
      },
      "mcp": {"local": {"command": ["x"], "environment": {"TOKEN": "mcp-secret"}}},
      "autoupdate": false,
    }''')
    cfg, refs = bootstrap.sanitized_opencode_config([f])
    assert cfg == {"$schema": "https://opencode.ai/config.json", "model": "bedrock/claude",
                   "provider": {"bedrock": {"options": {"region": "us-east-1", "bearer": "{env:AWS_BEARER_TOKEN_BEDROCK}",
                                                        "url": "https://x//y"}}}}
    assert refs == ["AWS_BEARER_TOKEN_BEDROCK"]
    assert "secret" not in json.dumps(cfg) and "sk-live" not in json.dumps(cfg)


def test_release_assets():
    p = bootstrap.Platform(arch="x64", musl=False, avx2=True, root=False)
    assert bootstrap.asset("opencode", "v1.19.6", p) == ("opencode-linux-x64.tar.gz", "opencode")
    assert bootstrap.asset("opencode", "v1.19.6", bootstrap.Platform("x64", True, False, False))[0] == "opencode-linux-x64-baseline-musl.tar.gz"
    assert bootstrap.asset("code-server", "v4.145.2", p) == ("code-server-4.145.2-linux-amd64.tar.gz",
                                                              "code-server-4.145.2-linux-amd64/bin/code-server")
    assert bootstrap.asset("gh", "v2.104.0", bootstrap.Platform("arm64", False, False, False)) == (
        "gh_2.104.0_linux_arm64.tar.gz", "gh_2.104.0_linux_arm64/bin/gh")
    assert bootstrap.asset("codex", "rust-v0.169.1", p) == ("codex-x86_64-unknown-linux-musl.tar.gz", "codex-x86_64-unknown-linux-musl")
    assert bootstrap.pm_command("apt-get", ["tmux", "git"], root=True).startswith("env DEBIAN_FRONTEND=noninteractive apt-get update")
    assert bootstrap.pm_command("dnf", ["tmux"], root=False) == "sudo -n dnf install -y tmux"


# ---------------------------------------------------------------------------------------------- forwarding end to end
@pytest.fixture
def fwd_settings(tmp_path, workspace, monkeypatch, fake_env):
    script = tmp_path / "tokens.sh"
    script.write_text(f"export WAW_TEST_TOKEN={TOKEN_VALUE}\n")
    ca = tmp_path / "corp-ca.pem"
    ca.write_text(CA_TEXT)
    monkeypatch.setenv("https_proxy", PROXY_WITH_CREDS)
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    (fake_env / "ssh_clean_env").touch()
    s = make_settings(tmp_path, workspace)
    s.agent_env_script = str(script)
    s.development = DevSettings(code_server=str(FAKES / "fake-code-server"), ssh=str(FAKES / "fake-ssh"),
                                worktrees_dir=tmp_path / "worktrees",
                                hosts=[DevHost(name="box", ssh="me@box", work_dir=str(tmp_path / "remote"),
                                               code_server=str(FAKES / "fake-code-server"), opencode=str(FAKES / "fake-opencode"),
                                               codex=str(FAKES / "fake-codex"), forward_env=True, forward_proxy=True,
                                               ca_bundle=str(ca))])
    return s


ENV_PROBE = ('echo "token=$WAW_TEST_TOKEN"; echo "https=$https_proxy"; echo "http=$http_proxy"; echo "no=$no_proxy"; '
             'echo "path=$PATH"; tail -n 2 "$SSL_CERT_FILE"; echo "node=$NODE_EXTRA_CA_CERTS"; '
             'echo end')


@requires_tmux
async def test_tasks_get_the_forwarded_environment_masked(running_app, fwd_settings, tmp_path, fake_env):
    h = await running_app(fwd_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id, host="box")
    remote = tmp_path / "remote"
    run = (await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks", json={"kind": "custom", "command": ENV_PROBE})).json()
    assert run["in_remote_tmux"] is True
    log = await wait_run(api, run["id"])
    text = log["text"]
    assert log["run"]["status"] == "passed", text
    assert "token=****" in text and TOKEN_VALUE not in text           # forwarded, shown masked
    assert "https=****" in text and "http=http://proxy.example:3128" in text
    assert "no=localhost,127.0.0.1,::1" in text
    assert f"path={remote}/bin:" in text
    assert "FAKE-EXTRA-CA" in text and f"node={remote}/ca-extra.pem" in text
    raw = Path(h.ctx.extra["dev_tasks"].get(run["id"]).log_path).read_text()
    assert TOKEN_VALUE in raw  # the local copy is as the command printed it; masking is at read time
    # the CA: the machine's own bundle plus the extra one
    assert (remote / "ca-extra.pem").read_text() == CA_TEXT and (remote / "ca.pem").read_text().endswith(CA_TEXT)
    # never on a command line, and no env file left behind
    assert TOKEN_VALUE not in _all_ssh_args(fake_env) and "pr0xy-pass" not in _all_ssh_args(fake_env)
    assert not list((remote / "run").glob("env-*.sh")) if (remote / "run").exists() else True
    # without tmux there (ssh -tt fallback): the same through an env file
    from backend.app.machines import rtmux

    orig = rtmux.RemoteTmux.info
    rtmux.RemoteTmux.info = lambda self, refresh=False: rtmux.RemoteInfo(False, reason="no tmux")
    try:
        run = (await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks", json={"kind": "custom", "command": ENV_PROBE})).json()
        assert run["in_remote_tmux"] is False and run["machine"] == "box"
        log = await wait_run(api, run["id"])
    finally:
        rtmux.RemoteTmux.info = orig
    assert log["run"]["status"] == "passed" and "token=****" in log["text"] and f"path={remote}/bin:" in log["text"], log
    assert TOKEN_VALUE not in _all_ssh_args(fake_env)
    assert not list((remote / "run").glob("env-*.sh"))


async def test_editor_token_and_agent_turn_env_off_argv(running_app, fwd_settings, tmp_path, fake_env):
    fwd_settings.providers["opencode"].bin = str(tmp_path / "no-opencode-here")  # this server has no opencode
    h = await running_app(fwd_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id, host="box")
    remote = tmp_path / "remote"
    # the editor: the extension's token and the forwarded variables reach code-server, not the ssh command line
    await api.post(f"/api/v1/dev/workspaces/{d['id']}/editor/start")
    ed = await wait_editor(api, d["id"])
    assert ed["state"] == "running", ed
    env = json.loads((fake_env / "code_server_env.json").read_text())
    assert env["WAW_DEV_TOKEN"] and env["WAW_TEST_TOKEN"] == TOKEN_VALUE and env["WAW_DEV_SOCKET"].startswith(str(remote / "run"))
    assert env["PATH"].startswith(f"{remote}/bin:")
    assert env["WAW_DEV_TOKEN"] not in _all_ssh_args(fake_env) and TOKEN_VALUE not in _all_ssh_args(fake_env)
    await api.post(f"/api/v1/dev/workspaces/{d['id']}/editor/stop")
    # an agent turn: runs the machine's opencode although there is none here; gets the variables and setup's settings
    (remote / "opencode").mkdir(parents=True, exist_ok=True)
    (remote / "opencode" / "opencode.json").write_text("{}")
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/ask", json={"text": "[[write:r.txt:::remote]]", "kind": "implement"})
    assert r.status_code in (200, 201, 202), r.text
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed", ev[-1]
    oenv = json.loads((fake_env / "opencode_last_env.json").read_text())
    assert oenv["WAW_TEST_TOKEN"] == TOKEN_VALUE and oenv["https_proxy"] == PROXY_WITH_CREDS
    assert oenv["OPENCODE_CONFIG"] == str(remote / "opencode" / "opencode.json")
    assert oenv["PATH"].startswith(f"{remote}/bin:") and oenv["SSL_CERT_FILE"] == str(remote / "ca.pem")
    assert TOKEN_VALUE not in _all_ssh_args(fake_env) and "pr0xy-pass" not in _all_ssh_args(fake_env)
    assert not list((remote / "run").glob("env-*.sh"))
    # the probe says opencode is missing there: a clear error instead of a failed ssh command
    reg = h.ctx.extra["machines"]
    reg.save_probe("box", {"tools": {"opencode": {"ok": False, "path": None}}, "complete": True})
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/ask", json={"text": "hello", "kind": "ask"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "failed" and "not installed on box" in str(ev[-1]) and "Set up" in str(ev[-1]), ev[-1]


@requires_tmux
async def test_terminal_on_a_machine_gets_the_environment(running_app, fwd_settings, tmp_path, fake_env):
    h = await running_app(fwd_settings)
    api = h.api
    r = await api.post("/api/v1/terminals", json={"machine": "box", "cwd": str(tmp_path)})
    assert r.status_code == 201, r.text
    t = r.json()
    out = tmp_path / "term-env.txt"
    rsock = f"{h.ctx.terminals.tmux.socket}-r"
    try:
        for _ in range(50):  # the login shell needs a moment
            subprocess.run(["tmux", "-L", rsock, "send-keys", "-t", f"={t['tmux_session']}:",
                            f'printf "%s|%s\\n" "$WAW_TEST_TOKEN" "$PATH" > {out}', "Enter"], check=False)
            await asyncio.sleep(0.2)
            if out.exists() and out.read_text().strip():
                break
        tok, path = out.read_text().strip().split("|", 1)
        assert tok == TOKEN_VALUE and path.startswith(f"{tmp_path / 'remote'}/bin:")
        assert TOKEN_VALUE not in _all_ssh_args(fake_env)
        assert not list((tmp_path / "remote" / "run").glob("env-*.sh"))
    finally:
        await api.delete(f"/api/v1/terminals/{t['id']}")


async def test_machine_env_settings_validation(running_app, tmp_path, workspace, fake_env):
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(ssh=str(FAKES / "fake-ssh"), hosts=[DevHost(name="cfg", ssh="cfg", forward_env=True, env_vars=["A"])])
    api = (await running_app(s)).api
    r = await api.post("/api/v1/machines", json={"name": "m1", "ssh": "m1.internal"})
    assert r.status_code == 201 and r.json()["env"] == {"forward_env": False, "env_vars": [], "forward_proxy": False, "ca_bundle": None}
    ca = tmp_path / "ca.pem"
    ca.write_text(CA_TEXT)
    r = await api.patch("/api/v1/machines/m1", json={"env": {"forward_env": True, "env_vars": ["AWS_BEARER_TOKEN_BEDROCK", "A"],
                                                             "forward_proxy": True, "ca_bundle": str(ca)}})
    assert r.status_code == 200, r.text
    assert r.json()["env"] == {"forward_env": True, "env_vars": ["AWS_BEARER_TOKEN_BEDROCK", "A"], "forward_proxy": True, "ca_bundle": str(ca)}
    assert h_env(await api.get("/api/v1/machines/cfg")) == {"forward_env": True, "env_vars": ["A"], "forward_proxy": False, "ca_bundle": None}
    for bad in ({"env_vars": ["1BAD"]}, {"env_vars": "A"}, {"ca_bundle": "relative.pem"}, {"ca_bundle": str(tmp_path / "missing.pem")}):
        r = await api.patch("/api/v1/machines/m1", json={"env": bad})
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_MACHINE", (bad, r.text)
    assert (await api.patch("/api/v1/machines/cfg", json={"env": {"forward_env": False}})).status_code in (400, 403, 409)


def h_env(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()["env"]


# ---------------------------------------------------------------------------------------------- setup (bootstrap)
def _tgz(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in files.items():
            data = body.encode()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), 0o755
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _tool(name: str, version: str) -> str:
    return f"#!/bin/sh\necho '{name} {version}'\n"


class Releases:
    """A release server with GitHub's layout: /<repo>/releases/latest redirects to …/tag/<tag>."""

    TAGS = {"anomalyco/opencode": "v9.1.0", "coder/code-server": "v4.200.1", "cli/cli": "v2.300.0", "openai/codex": "rust-v0.500.2"}

    def __init__(self):
        p = bootstrap.Platform(arch=_arch(), musl=False, avx2=True, root=False)
        self.files: dict[str, bytes] = {}
        self.hits: list[str] = []
        for name, repo in bootstrap.REPOS.items():
            tag = self.TAGS[repo]
            for musl, avx2 in ((False, True), (False, False), (True, True), (True, False)):
                archive, member = bootstrap.asset(name, tag, bootstrap.Platform(p.arch, musl, avx2, False))
                self.files[f"/{repo}/releases/download/{tag}/{archive}"] = _tgz({member: _tool(name, bootstrap.version_of(tag))})
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self.do_GET(body=False)

            def do_GET(self, body=True):
                outer.hits.append(self.path)
                m = re.match(r"^/([^/]+/[^/]+)/releases/latest$", self.path)
                if m and m.group(1) in outer.TAGS:
                    self.send_response(302)
                    self.send_header("Location", f"http://{self.headers['Host']}/{m.group(1)}/releases/tag/{outer.TAGS[m.group(1)]}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = outer.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if body:
                    self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _arch() -> str:
    return {"x86_64": "x64", "aarch64": "arm64"}.get(os.uname().machine, "x64")


@pytest.fixture
def releases():
    r = Releases()
    yield r
    r.close()


async def wait_job(api, job_id: str, timeout: float = 120) -> dict:
    for _ in range(int(timeout / 0.2)):
        log = (await api.get(f"/api/v1/machine-jobs/{job_id}/log")).json()
        if log["job"]["status"] != "running":
            return log
        await asyncio.sleep(0.2)
    raise AssertionError(log)


async def test_setup_installs_tools_and_opencode_settings(running_app, tmp_path, workspace, fake_env, releases, clean):
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(ssh=str(FAKES / "fake-ssh"), release_base=releases.base)
    cfg_dir = clean / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(json.dumps({"model": "bedrock/x", "provider": {"bedrock": {"options": {
        "apiKey": "sk-should-not-travel", "region": "us-west-2", "token": "{env:AWS_BEARER_TOKEN_BEDROCK}"}}}}))
    h = await running_app(s)
    api = h.api
    work = tmp_path / "remote-m1"
    assert (await api.post("/api/v1/machines", json={"name": "m1", "ssh": "m1.internal", "work_dir": str(work)})).status_code == 201
    plan = (await api.get("/api/v1/machines/m1/bootstrap")).json()
    assert plan["probed"] is False and plan["release_base"] == releases.base
    assert plan["agent_settings"]["source"] == str(cfg_dir / "opencode.json")
    r = await api.post("/api/v1/machines/m1/bootstrap", json={"tools": ["opencode", "code-server", "gh"]})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "running" and job["title"] == "Set up m1"
    r2 = await api.post("/api/v1/machines/m1/bootstrap", json={})
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "MACHINE_BUSY"
    log = await wait_job(api, job["id"])
    assert log["job"]["status"] == "passed", log["text"]
    for name, ver in (("opencode", "9.1.0"), ("code-server", "4.200.1"), ("gh", "2.300.0")):
        exe = work / "bin" / name
        assert exe.is_symlink() and subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip() == f"{name} {ver}"
        assert str(Path(os.readlink(exe))).startswith(str(work / "tools" / f"{name}-{ver}"))
    assert log["job"]["result"]["installed"] == {"opencode": "v9.1.0", "code-server": "v4.200.1", "gh": "v2.300.0"}
    # the machine downloaded them itself
    assert not (tmp_path / "state" / "cache" / "tools").exists()
    # opencode settings without the key; the {env:} reference is kept and reported
    copied = json.loads((work / "opencode" / "opencode.json").read_text())
    assert copied == {"model": "bedrock/x", "provider": {"bedrock": {"options": {"region": "us-west-2", "token": "{env:AWS_BEARER_TOKEN_BEDROCK}"}}}}
    assert stat.S_IMODE((work / "opencode" / "opencode.json").stat().st_mode) == 0o600
    assert "uses {env:AWS_BEARER_TOKEN_BEDROCK}: not forwarded" in log["text"]
    # probed again: the tools are there, under setup's bin, first on PATH
    m = (await api.get("/api/v1/machines/m1")).json()
    assert m["capabilities"]["tools"]["opencode"]["path"] == str(work / "bin" / "opencode")
    plan = (await api.get("/api/v1/machines/m1/bootstrap")).json()
    oc = next(t for t in plan["tools"] if t["name"] == "opencode")
    assert oc["installed"] and oc["managed"] and not oc["default"]
    # again: nothing to do for what setup installed; history lists both runs
    log = await wait_job(api, (await api.post("/api/v1/machines/m1/bootstrap", json={"tools": ["opencode"], "agent_settings": False})).json()["id"])
    assert log["job"]["status"] == "passed" and log["job"]["result"]["skipped"] == {"opencode": "9.1.0"}, log["text"]
    jobs = (await api.get("/api/v1/machines/m1/jobs")).json()["jobs"]
    assert len(jobs) == 2 and jobs[0]["id"] == log["job"]["id"]


async def test_setup_offline_machine_gets_tools_from_this_server(running_app, tmp_path, workspace, fake_env, releases):
    (fake_env / "ssh_no_net").touch()
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(ssh=str(FAKES / "fake-ssh"), release_base=releases.base)
    api = (await running_app(s)).api
    work = tmp_path / "remote-off"
    assert (await api.post("/api/v1/machines", json={"name": "off", "ssh": "off.internal", "work_dir": str(work)})).status_code == 201
    r = await api.post("/api/v1/machines/off/bootstrap", json={"tools": ["codex"], "system_packages": False, "agent_settings": False})
    log = await wait_job(api, r.json()["id"])
    assert log["job"]["status"] == "passed", log["text"]
    assert "could not download it; sending it from this server" in log["text"]
    out = subprocess.run([str(work / "bin" / "codex")], capture_output=True, text=True).stdout.strip()
    assert out == "codex 0.500.2"
    cached = list((tmp_path / "state" / "cache" / "tools").iterdir())
    assert len(cached) == 1 and cached[0].name.endswith(".tar.gz")


async def test_setup_failures_and_cancel(running_app, tmp_path, workspace, fake_env, releases):
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(ssh=str(FAKES / "fake-ssh"), release_base=releases.base + "/nothing-here")
    api = (await running_app(s)).api
    await api.post("/api/v1/machines", json={"name": "m1", "ssh": "m1.internal", "work_dir": str(tmp_path / "w1")})
    await api.post("/api/v1/machines", json={"name": "b", "kind": "bastion", "ssh": "b.example"})
    await api.post("/api/v1/machines", json={"name": "dead", "ssh": "unreachable.internal"})
    assert (await api.post("/api/v1/machines/b/bootstrap", json={})).json()["error"]["code"] == "MACHINE_IS_BASTION"
    assert (await api.post("/api/v1/machines/local/bootstrap", json={})).json()["error"]["code"] == "MACHINE_IS_LOCAL"
    assert (await api.post("/api/v1/machines/m1/bootstrap", json={"tools": ["vim"]})).json()["error"]["code"] == "INVALID_TOOLS"
    log = await wait_job(api, (await api.post("/api/v1/machines/m1/bootstrap", json={"tools": ["gh"], "agent_settings": False})).json()["id"])
    assert log["job"]["status"] == "failed" and "cannot find the latest release of cli/cli" in log["text"], log["text"]
    log = await wait_job(api, (await api.post("/api/v1/machines/dead/bootstrap", json={})).json()["id"])
    assert log["job"]["status"] == "failed" and "Connection refused" in log["job"]["error"], log
    # cancel while it runs
    job = (await api.post("/api/v1/machines/m1/bootstrap", json={"tools": ["gh"]})).json()
    r = await api.post(f"/api/v1/machine-jobs/{job['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled", r.text

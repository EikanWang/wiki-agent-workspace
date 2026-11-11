"""The VS Code-compatible editor of a development workspace: one code-server process per running workspace.

code-server listens on a private Unix socket (mode 0600, in a 0700 folder) with its own auth disabled; the browser
only reaches it through the app's authenticated proxy (`/dev-editor/<id>/…`, see proxy.py). On an SSH host it runs
there and `ssh -L <local socket>:<remote socket>` brings its socket to the server; `-R` gives the integration
extension a socket back to this app.

Editor state shared by all workspaces of a host (settings, keybindings, installed extensions) lives in
`<state>/dev/code-server/` (local) or `<work_dir>/code-server/` (SSH host). Each workspace opens a generated
`.code-workspace` file (outside the worktree, so nothing is added to the repository) with the worktree, the reference
wiki (read-only), language settings (clangd / Python interpreter), build/test tasks and debug configurations.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import signal
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..deps import AppContext
from ..errors import ApiError, conflict
from ..models import DevWorkspace
from .service import DevService, new_token, settings_of
from .targets import Target, TargetError

log = logging.getLogger("waw.development.editor")

EXT_SRC = Path(__file__).resolve().parents[3] / "vscode-extension" / "waw-dev"
START_TIMEOUT = 90.0
# an explicit config file, so code-server never creates ~/.config/code-server/config.yaml (with a password) for us
CONFIG_YAML = "# written by Wiki Agent Workspace: the editor is reached only through the app's login\nauth: none\ncert: false\n"


def ext_version() -> str:
    try:
        return json.loads((EXT_SRC / "package.json").read_text("utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "0.0.0"


def build_vsix(out_dir: Path) -> Path:
    """Package vscode-extension/waw-dev as a .vsix (a zip with a VSIX manifest; no build tools needed)."""
    pkg = json.loads((EXT_SRC / "package.json").read_text("utf-8"))
    ver, pub, name = pkg["version"], pkg["publisher"], pkg["name"]
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = out_dir / f"{pub}.{name}-{ver}.vsix"
    if out.exists():
        return out
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{ver}" Publisher="{pub}" />
    <DisplayName>{pkg.get('displayName', name)}</DisplayName>
    <Description xml:space="preserve">{pkg.get('description', '')}</Description>
    <Categories>Other</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{pkg['engines']['vscode']}" />
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
  </Assets>
</PackageManifest>
"""
    types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/><Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/><Default Extension=".md" ContentType="text/markdown"/>
</Types>
"""
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("extension.vsixmanifest", manifest)
        z.writestr("[Content_Types].xml", types)
        for f in sorted(EXT_SRC.rglob("*")):
            if f.is_file() and "node_modules" not in f.parts:
                z.write(f, "extension/" + f.relative_to(EXT_SRC).as_posix())
    os.replace(tmp, out)
    return out


@dataclass
class EditorProc:
    dev_id: str
    state: str = "starting"  # starting | running | failed | stopped
    error: str | None = None
    proc: asyncio.subprocess.Process | None = None
    sock: Path | None = None            # local socket the proxy connects to
    remote_sock: str | None = None
    token: str = field(default_factory=new_token)
    log_path: Path | None = None
    started_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    workspace_file: str | None = None   # opened with ?workspace= (the editor profile is shared by all workspaces)


class EditorManager:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.dev = ctx.settings.development
        self.base = ctx.settings.state_dir / "dev"
        self.editors: dict[str, EditorProc] = {}
        self.installs: dict[str, dict[str, Any]] = {}  # host -> extension install state

    # ------------------------------------------------------------------ info
    def local_bin(self) -> str | None:
        cand = self.dev.code_server or "code-server"
        if os.path.sep in cand:
            return cand if os.access(cand, os.X_OK) else None
        return shutil.which(cand)

    def info(self) -> dict[str, Any]:
        exe = self.local_bin()
        return {"code_server": exe, "available": bool(exe), "extension_version": ext_version(),
                "install_hint": None if exe else ("Install code-server on the server (https://coder.com/docs/code-server/install) "
                                                  "or set development.code_server in the config file; SSH hosts need it too.")}

    def status(self, dev_id: str) -> dict[str, Any]:
        e = self.editors.get(dev_id)
        if e is None:
            return {"state": "stopped", "url": None, "error": None}
        if e.state == "running" and e.proc is not None and e.proc.returncode is not None:
            e.state, e.error = "failed", f"the editor exited (code {e.proc.returncode}): {self._log_tail(e)}"
        from urllib.parse import quote

        url = f"/dev-editor/{dev_id}/?workspace={quote(e.workspace_file or '', safe='/')}" if e.state == "running" else None
        return {"state": e.state, "url": url, "error": e.error,
                "started_at": e.started_at}

    def socket_for(self, dev_id: str) -> Path | None:
        e = self.editors.get(dev_id)
        return e.sock if e and e.state == "running" else None

    def check_token(self, dev_id: str, token: str) -> bool:
        e = self.editors.get(dev_id)
        return bool(e and token and hmac.compare_digest(e.token, token))

    def _log_tail(self, e: EditorProc, n: int = 1500) -> str:
        try:
            return (e.log_path.read_text("utf-8", "replace")[-n:] if e.log_path else "").strip()
        except OSError:
            return ""

    def _publish(self, dev_id: str) -> None:
        if self.ctx.events:
            self.ctx.events.publish("global", {"type": "dev_changed", "dev_id": dev_id})

    # ------------------------------------------------------------------ paths per host
    async def _dirs(self, t: Target) -> dict[str, str]:
        if t.is_local:
            root = self.base / "code-server"
            run = self.base / "run"
            for d in (root / "data", root / "extensions", root / "workspaces", root / "shared", run):
                d.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(run, 0o700)
            conf = root / "config.yaml"
            if not conf.exists():
                conf.write_text(CONFIG_YAML, encoding="utf-8")
            return {"data": str(root / "data"), "ext": str(root / "extensions"), "ws": str(root / "workspaces"), "run": str(run),
                    "shared": str(root / "shared"), "config": str(conf)}
        host = self.dev.host(t.name)
        assert host is not None
        base = await t.expand(host.work_dir)
        dirs = {"data": f"{base}/code-server/data", "ext": f"{base}/code-server/extensions", "ws": f"{base}/code-server/workspaces",
                "run": f"{base}/run", "shared": f"{base}/code-server/shared"}
        for d in dirs.values():
            await t.mkdir(d)
        dirs["config"] = f"{base}/code-server/config.yaml"
        await t.write_text(dirs["config"], CONFIG_YAML)
        return dirs

    def _bin_for(self, t: Target) -> str:
        if t.is_local:
            exe = self.local_bin()
            if not exe:
                raise ApiError(409, "EDITOR_UNAVAILABLE", self.info()["install_hint"])
            return exe
        host = self.dev.host(t.name)
        assert host is not None
        return host.code_server

    # ------------------------------------------------------------------ workspace file
    def workspace_file(self, row: DevWorkspace, wiki: str | None) -> dict[str, Any]:
        cfg = settings_of(row)
        wt = row.worktree_path
        folders: list[dict[str, str]] = [{"path": wt, "name": f"{row.name} — worktree"}]
        settings: dict[str, Any] = {
            "security.workspace.trust.enabled": False,
            "git.autofetch": False,
            "git.openRepositoryInParentFolders": "never",
            "editor.inlineSuggest.enabled": True,
            "C_Cpp.intelliSenseEngine": "disabled",
            "extensions.ignoreRecommendations": True,
            "workbench.startupEditor": "none",
            # the app's agent is the agent here: keep VS Code's own (Copilot) chat surfaces out of the way
            "workbench.secondarySideBar.defaultVisibility": "hidden",
            "chat.commandCenter.enabled": False,
            "waw.devId": row.id,
        }
        if wiki:
            folders.append({"path": wiki, "name": "wiki (reference, read-only)"})
            settings["files.readonlyInclude"] = {f"{wiki}/**": True}
        ccd = cfg.get("compile_commands_dir")
        clangd_args = ["--background-index", "--header-insertion=never", "--clang-tidy"]
        if ccd:
            clangd_args.insert(0, f"--compile-commands-dir={ccd if ccd.startswith('/') else f'{wt}/{ccd}'}")
        settings["clangd.arguments"] = clangd_args
        if cfg.get("clangd"):
            settings["clangd.path"] = cfg["clangd"]
        if cfg.get("python"):
            settings["python.defaultInterpreterPath"] = cfg["python"]
        tasks = []
        if cfg.get("build_command"):
            tasks.append({"label": "waw: build", "type": "shell", "command": cfg["build_command"], "options": {"cwd": wt},
                          "group": {"kind": "build", "isDefault": True}, "problemMatcher": ["$gcc"]})
        if cfg.get("test_command"):
            tasks.append({"label": "waw: test", "type": "shell", "command": cfg["test_command"], "options": {"cwd": wt},
                          "group": {"kind": "test", "isDefault": True}, "problemMatcher": []})
        launch = [
            {"name": "Python: current file", "type": "debugpy", "request": "launch", "program": "${file}", "cwd": wt,
             "console": "integratedTerminal", "justMyCode": False},
            {"name": "Python: pytest current file", "type": "debugpy", "request": "launch", "module": "pytest",
             "args": ["-q", "${file}"], "cwd": wt, "console": "integratedTerminal", "justMyCode": False},
        ]
        if cfg.get("python"):
            for c in launch:
                c["python"] = cfg["python"]
        if cfg.get("debug_program"):
            prog = cfg["debug_program"]
            launch.append({"name": "C++: debug program", "type": "lldb", "request": "launch",
                           "program": prog if prog.startswith("/") else f"{wt}/{prog}", "args": cfg.get("debug_args") or [],
                           "cwd": wt, **({"preLaunchTask": "waw: build"} if cfg.get("build_command") else {})})
        return {"folders": folders, "settings": settings, "tasks": {"version": "2.0.0", "tasks": tasks},
                "launch": {"version": "0.2.0", "configurations": launch}}

    async def write_workspace_file(self, row: DevWorkspace) -> str:
        """(Re)write the .code-workspace file; a running editor picks up the change itself."""
        svc = DevService(self.ctx)
        t = svc.target(row)
        dirs = await self._dirs(t)
        wiki = None
        if row.wiki_workspace_id:
            if t.is_local:
                root = svc.wiki_root(row)
                wiki = str(root) if root else None
            else:
                from .agent import remote_wiki_dir

                wiki = await remote_wiki_dir(self.ctx, row)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", row.name).strip("-")[:60] or "workspace"
        path = f"{dirs['ws']}/{slug}-{row.id[:8]}.code-workspace"  # the editor shows the file name as the workspace name
        await t.write_text(path, json.dumps(self.workspace_file(row, wiki), indent=2))
        return path

    # ------------------------------------------------------------------ integration extension
    async def ensure_extension(self, t: Target, exe: str, dirs: dict[str, str]) -> None:
        ver = ext_version()
        marker = f"{dirs['ext']}/.waw-dev-{ver}"
        if await self._exists(t, marker):
            return
        vsix = build_vsix(self.base / "vsix")
        if t.is_local:
            path = str(vsix)
        else:
            path = f"{dirs['run']}/{vsix.name}"
            rc, _, err = await t.run(["sh", "-c", f"cat > '{path}'"], timeout=120, stdin=vsix.read_bytes())
            if rc != 0:
                raise ApiError(400, "EDITOR_EXTENSION_FAILED", f"could not copy the integration extension to {t.name}: {err[:300]}")
        rc, out, err = await t.run([exe, "--config", dirs["config"], "--extensions-dir", dirs["ext"], "--user-data-dir", dirs["data"],
                                    "--install-extension", path, "--force"],
                                   timeout=300)
        if rc != 0:
            raise ApiError(400, "EDITOR_EXTENSION_FAILED", f"installing the integration extension failed: {(err or out).strip()[-400:]}")
        await t.write_text(marker, ver + "\n")

    async def _exists(self, t: Target, path: str) -> bool:
        if t.is_local:
            return Path(path).exists()
        rc, _, _ = await t.run(["test", "-e", path], timeout=30)
        return rc == 0

    def install_state(self, host: str) -> dict[str, Any]:
        return self.installs.get(host) or {"state": "idle", "log": "", "extensions": list(self.dev.extensions)}

    async def install_language_support(self, host: str, ids: list[str] | None) -> dict[str, Any]:
        """Install the C++/Python extensions (Open VSX) into the host's shared editor profile, in the background."""
        svc = DevService(self.ctx)
        t = svc.target(host)
        exe = self._bin_for(t)
        ids = [i for i in (ids or list(self.dev.extensions)) if i and not i.startswith("-")]
        cur = self.installs.get(host)
        if cur and cur["state"] == "running":
            return cur
        st: dict[str, Any] = {"state": "running", "log": "", "extensions": ids, "failed": []}
        self.installs[host] = st

        async def run() -> None:
            try:
                dirs = await self._dirs(t)
                for ext in ids:
                    st["log"] += f"$ install {ext}\n"
                    rc, out, err = await t.run([exe, "--config", dirs["config"], "--extensions-dir", dirs["ext"],
                                                "--user-data-dir", dirs["data"], "--install-extension", ext], timeout=600)
                    st["log"] += (out + err)[-2000:] + "\n"
                    if rc != 0:
                        st["failed"].append(ext)
                st["state"] = "failed" if st["failed"] else "done"
            except (ApiError, TargetError) as exc:
                st["state"], st["log"] = "failed", st["log"] + f"\n{getattr(exc, 'message', exc)}"
            except Exception as exc:  # pragma: no cover
                st["state"], st["log"] = "failed", st["log"] + f"\n{exc.__class__.__name__}: {exc}"
        asyncio.get_running_loop().create_task(run())
        return st

    # ------------------------------------------------------------------ lifecycle
    async def start(self, dev_id: str) -> dict[str, Any]:
        svc = DevService(self.ctx)
        row, t = svc._ready(dev_id)
        cur = self.editors.get(dev_id)
        if cur and cur.state in ("starting", "running") and (cur.proc is None or cur.proc.returncode is None):
            return self.status(dev_id)
        self._bin_for(t)  # fail fast when there is no editor executable
        e = EditorProc(dev_id=dev_id)
        self.editors[dev_id] = e
        e.task = asyncio.get_running_loop().create_task(self._run(e, row, t))
        self._publish(dev_id)
        return self.status(dev_id)

    async def _run(self, e: EditorProc, row: DevWorkspace, t: Target) -> None:
        try:
            await self._launch(e, row, t)
            e.state = "running"
        except (ApiError, TargetError) as exc:
            e.state, e.error = "failed", getattr(exc, "message", str(exc))
            await self._kill(e)
        except Exception as exc:
            log.exception("editor start failed")
            e.state, e.error = "failed", f"{exc.__class__.__name__}: {exc}"
            await self._kill(e)
        self._publish(e.dev_id)

    async def _launch(self, e: EditorProc, row: DevWorkspace, t: Target) -> None:
        exe = self._bin_for(t)
        dirs = await self._dirs(t)
        await self.ensure_extension(t, exe, dirs)
        ws_file = e.workspace_file = await self.write_workspace_file(row)
        run_local = self.base / "run"
        run_local.mkdir(parents=True, exist_ok=True, mode=0o700)
        e.sock = run_local / f"{row.id[:12]}.sock"
        e.sock.unlink(missing_ok=True)
        e.log_path = self.base / "logs" / f"editor-{row.id}.log"
        e.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {"WAW_DEV_ID": row.id, "WAW_DEV_TOKEN": e.token}
        port = self.ctx.settings.port
        bind = self.ctx.settings.bind
        app_host = "127.0.0.1" if bind in ("0.0.0.0", "::", "", "localhost") else bind
        ssh_opts = None
        if t.is_local:
            sock = str(e.sock)
            env["WAW_DEV_URL"] = f"http://{app_host}:{port}"
        else:
            sock = e.remote_sock = f"{dirs['run']}/{row.id[:12]}.sock"
            app_sock = f"{dirs['run']}/{row.id[:12]}-app.sock"
            env["WAW_DEV_SOCKET"] = app_sock
            await t.run(["rm", "-f", sock, app_sock], timeout=30)
            ssh_opts = ["-tt", "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
                        "-L", f"{e.sock}:{sock}", "-R", f"{app_sock}:{app_host}:{port}"]
        args = [exe, "--config", dirs["config"], "--vscode-option", f"shared-data-dir={dirs['shared']}", "--socket", sock, "--socket-mode", "600", "--auth", "none", "--disable-telemetry", "--disable-update-check",
                "--disable-workspace-trust", "--disable-getting-started-override", "--user-data-dir", dirs["data"],
                "--extensions-dir", dirs["ext"], ws_file]
        argv, lcwd, lenv = t.spawn_argv(args, row.worktree_path, env, ssh_opts=ssh_opts)
        fh = open(e.log_path, "ab", buffering=0)  # noqa: SIM115 - handed to the child process
        try:
            e.proc = await asyncio.create_subprocess_exec(*argv, cwd=lcwd, env=lenv, stdin=asyncio.subprocess.DEVNULL,
                                                          stdout=fh, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise ApiError(400, "EDITOR_START_FAILED", f"could not start the editor: {exc}")
        finally:
            fh.close()
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if e.proc.returncode is not None:
                raise ApiError(400, "EDITOR_START_FAILED", f"the editor exited during start (code {e.proc.returncode}): {self._log_tail(e)}")
            if e.sock.exists():
                try:
                    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(e.sock)), timeout=3) as c:
                        r = await c.get("http://localhost/healthz")
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(0.25)
        raise ApiError(504, "EDITOR_START_TIMEOUT", f"the editor did not become ready in {int(START_TIMEOUT)} s: {self._log_tail(e)}")

    async def _kill(self, e: EditorProc) -> None:
        if e.proc is not None and e.proc.returncode is None:
            try:
                os.killpg(e.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(e.proc.wait(), timeout=8)
            except TimeoutError:
                try:
                    os.killpg(e.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if e.remote_sock:
            try:
                row = DevService(self.ctx).get(e.dev_id)
                t = DevService(self.ctx).target(row)
                await t.run(["sh", "-c", f"pkill -f -- '--socket {e.remote_sock}' ; rm -f '{e.remote_sock}'"], timeout=30)
            except Exception:  # best effort
                pass
        if e.sock is not None:
            e.sock.unlink(missing_ok=True)

    async def stop(self, dev_id: str) -> dict[str, Any]:
        e = self.editors.pop(dev_id, None)
        if e is not None:
            if e.task is not None and not e.task.done():
                e.task.cancel()
            await self._kill(e)
            self._publish(dev_id)
        return {"state": "stopped", "url": None, "error": None}

    async def restart(self, dev_id: str) -> dict[str, Any]:
        await self.stop(dev_id)
        return await self.start(dev_id)

    async def shutdown(self) -> None:
        for dev_id in list(self.editors):
            try:
                await self.stop(dev_id)
            except Exception:  # pragma: no cover
                log.exception("stopping editor %s", dev_id)

    def require_running(self, dev_id: str) -> Path:
        sock = self.socket_for(dev_id)
        if sock is None:
            raise conflict("EDITOR_NOT_RUNNING", "the editor of this workspace is not running")
        return sock

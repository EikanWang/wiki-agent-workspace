"""Language servers for the web editor, bridged over a WebSocket.

`/api/v1/dev/workspaces/<id>/lsp/<lang>` starts the language server on the workspace's host (in the worktree, over
ssh for SSH hosts) and relays LSP JSON-RPC: one WebSocket text message = one LSP message (the bridge adds and strips
the `Content-Length` framing). The browser is the LSP client. The server ends with the connection.

C/C++: clangd (setting `clangd`, else clangd / clangd-NN on the host's PATH) with the workspace's
compile_commands.json folder. Python: setting `python_lsp`, else basedpyright-langserver, pyright-langserver or pylsp
(also looked up next to the configured interpreter).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..errors import ApiError
from .service import DevService, settings_of
from .targets import Target

log = logging.getLogger("waw.development.lsp")
router = APIRouter()

CLANGD = ["clangd"] + [f"clangd-{v}" for v in range(21, 13, -1)]
PYTHON = [("basedpyright-langserver", ["--stdio"]), ("pyright-langserver", ["--stdio"]), ("pylsp", [])]
HINTS = {
    "cpp": "Install clangd on the host (e.g. `sudo apt install clangd`, or an LLVM release) or set its path in the workspace settings.",
    "python": "Install a Python language server on the host: `pip install basedpyright` (or pyright, or python-lsp-server) "
              "into the workspace's Python environment, or set its path in the workspace settings.",
}
MAX_MESSAGE = 64 * 1024 * 1024

# language servers tried in order for the other languages (key = the web editor's server key, see lsp.ts LSP_LANGS)
KNOWN: dict[str, list[list[str]]] = {
    "yaml": [["yaml-language-server", "--stdio"]],
    "starlark": [["starpls", "server"], ["bazel-lsp"]],
    "shell": [["bash-language-server", "start"]],
    "cmake": [["neocmakelsp", "--stdio"], ["cmake-language-server"]],
    "json": [["vscode-json-language-server", "--stdio"], ["vscode-json-languageserver", "--stdio"]],
    "toml": [["taplo", "lsp", "stdio"]],
    "dockerfile": [["docker-langserver", "--stdio"]],
    "markdown": [["marksman", "server"]],
    "go": [["gopls"]],
    "rust": [["rust-analyzer"]],
    "typescript": [["typescript-language-server", "--stdio"]],
    "protobuf": [["buf", "beta", "lsp"], ["protols"]],
}
_servers: dict[str, list[str]] = {}


def ctx_servers() -> dict[str, list[str]]:
    """[development.language_servers] from the config: {key: argv}."""
    return _servers


def configure(servers: dict[str, list[str]]) -> None:
    _servers.clear()
    _servers.update({k: v for k, v in servers.items() if isinstance(v, list) and v and all(isinstance(x, str) for x in v)})


async def _which(t: Target, names: list[str], extra_dirs: list[str]) -> str | None:
    """First of `names` found in `extra_dirs` or on the host's PATH."""
    cands = [f"{d.rstrip('/')}/{n}" for d in extra_dirs for n in names] + names
    script = "; ".join(f"if [ -x {shlex.quote(c)} ]; then echo {shlex.quote(c)}; exit 0; fi" if "/" in c else
                       f"command -v {shlex.quote(c)} && exit 0" for c in cands) + "; exit 1"
    rc, out, _ = await t.run(["sh", "-c", script], timeout=30)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else None


async def server_command(t: Target, row: Any, lang: str) -> tuple[list[str] | None, str | None]:
    """(argv, None) or (None, why not)."""
    cfg = settings_of(row)
    custom = _servers.get(lang)
    if custom:  # [development.language_servers] wins
        exe = await _which(t, [custom[0]], [])
        return ([exe, *custom[1:]], None) if exe else (None, f"{custom[0]} (from [development.language_servers]) was not found on {t.label()}")
    if lang == "cpp":
        exe = cfg.get("clangd") or await _which(t, CLANGD, [])
        if not exe:
            return None, HINTS["cpp"]
        args = [exe, "--background-index", "--header-insertion=never", "--clang-tidy", "--pch-storage=memory", "--log=error"]
        ccd = cfg.get("compile_commands_dir")
        if ccd:
            args.append(f"--compile-commands-dir={ccd if ccd.startswith('/') else row.worktree_path.rstrip('/') + '/' + ccd}")
        return args, None
    if lang == "python":
        if cfg.get("python_lsp"):
            exe = cfg["python_lsp"]
            return [exe, *(["--stdio"] if "pyright" in exe.rsplit("/", 1)[-1] else [])], None
        py = cfg.get("python") or ""
        dirs = [py.rsplit("/", 1)[0]] if "/" in py else []
        for name, extra in PYTHON:
            exe = await _which(t, [name], dirs)
            if exe:
                return [exe, *extra], None
        return None, HINTS["python"]
    known = KNOWN.get(lang)
    if not known:
        return None, f"no language server for {lang}"
    for argv in known:
        exe = await _which(t, [argv[0]], [])
        if exe:
            return [exe, *argv[1:]], None
    return None, (f"No {lang} language server on {t.label()} (basic navigation is used: go to definition and references by "
                  f"search, outline, Bazel labels and paths). For full support install one of: "
                  + ", ".join(a[0] for a in known) + ", or set [development.language_servers] in the config file.")


async def lsp_info(ctx, dev_id: str) -> dict[str, Any]:
    svc = DevService(ctx)
    row, t = svc._ready(dev_id)
    out: dict[str, Any] = {}
    for lang in ("cpp", "python", *KNOWN.keys(), *[k for k in _servers if k not in KNOWN]):
        argv, why = await server_command(t, row, lang)
        out[lang] = {"available": argv is not None, "command": argv[0] if argv else None, "hint": why}
    return out


def _frame(body: bytes) -> bytes:
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


async def _read_messages(stream: asyncio.StreamReader):
    while True:
        length = None
        while True:
            line = await stream.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                break
            k, _, v = line.decode("ascii", "replace").partition(":")
            if k.strip().lower() == "content-length":
                length = int(v.strip())
        if length is None or length > MAX_MESSAGE:
            return
        yield await stream.readexactly(length)


@router.websocket("/dev/workspaces/{dev_id}/lsp/{lang}")
async def lsp_ws(ws: WebSocket, dev_id: str, lang: str):
    ctx = ws.app.state.ctx
    try:
        subprotocol = ctx.auth.authenticate_websocket(ws)
        svc = DevService(ctx)
        row, t = svc._ready(dev_id)
    except ApiError as exc:
        await ws.close(code=4401 if exc.status == 401 else 4404, reason=exc.code)
        return
    argv, why = await server_command(t, row, lang)
    await ws.accept(subprotocol=subprotocol)
    if argv is None:
        await ws.send_text(json.dumps({"waw": "unavailable", "message": why}))
        await ws.close(code=4412)
        return
    largv, lcwd, lenv = t.spawn_argv(argv, row.worktree_path, None)
    try:
        proc = await asyncio.create_subprocess_exec(*largv, cwd=lcwd, env=lenv, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    start_new_session=True, limit=MAX_MESSAGE)
    except OSError as exc:
        await ws.send_text(json.dumps({"waw": "unavailable", "message": f"could not start {argv[0]}: {exc}"}))
        await ws.close(code=4412)
        return
    await ws.send_text(json.dumps({"waw": "started", "command": argv[0], "host": t.label()}))
    stderr_tail: list[str] = []

    async def pump_out() -> None:
        assert proc.stdout
        try:
            async for body in _read_messages(proc.stdout):
                if ws.client_state != WebSocketState.CONNECTED:
                    return
                await ws.send_text(body.decode("utf-8", "replace"))
        except (asyncio.IncompleteReadError, RuntimeError, WebSocketDisconnect):
            pass

    async def pump_err() -> None:
        assert proc.stderr
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            stderr_tail.append(line.decode("utf-8", "replace").rstrip()[:500])
            del stderr_tail[:-30]

    async def pump_in() -> None:
        assert proc.stdin
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    return
                data = m.get("text")
                body = data.encode("utf-8") if data is not None else m.get("bytes")
                if body:
                    proc.stdin.write(_frame(body))
                    await proc.stdin.drain()
        except (WebSocketDisconnect, ConnectionResetError, BrokenPipeError, RuntimeError):
            pass

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in()), asyncio.create_task(pump_err())]
    await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if proc.returncode is None:
        with contextlib.suppress(Exception):
            proc.stdin.close()  # type: ignore[union-attr]
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
    if ws.client_state == WebSocketState.CONNECTED:
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"waw": "exited", "code": proc.returncode, "stderr": "\n".join(stderr_tail[-10:])}))
            await ws.close(code=4413)

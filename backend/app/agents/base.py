"""Agent adapter contract (DESIGN §8).

An adapter exposes capabilities(), list_sessions(), create_session(), resume_turn() and
cancel(). A turn yields typed streaming events: started, delta, tool_activity,
permission_request, completed, failed, cancelled.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator


class ProviderError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class Capabilities:
    provider: str
    available: bool
    version: str | None = None
    executable: str | None = None
    mode: str = "cli"
    can_list_sessions: bool = False
    can_resume: bool = False
    can_stream_deltas: bool = False
    can_cancel: bool = False
    can_relay_permissions: bool = False
    can_select_model: bool = False
    uses_local_service: bool = False
    eager_session_ids: bool = False      # native id known at create_session()
    preview_isolation: str = "copy"       # copy | unavailable
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "version": self.version,
            "executable": self.executable,
            "mode": self.mode,
            "can_list_sessions": self.can_list_sessions,
            "can_resume": self.can_resume,
            "can_stream_deltas": self.can_stream_deltas,
            "can_cancel": self.can_cancel,
            "can_relay_permissions": self.can_relay_permissions,
            "can_select_model": self.can_select_model,
            "uses_local_service": self.uses_local_service,
            "eager_session_ids": self.eager_session_ids,
            "preview_isolation": self.preview_isolation,
            "error": self.error,
            "notes": list(self.notes),
        }


@dataclass
class NativeSession:
    native_id: str
    title: str | None
    directory: str | None
    updated_at: str | None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"native_id": self.native_id, "title": self.title, "directory": self.directory,
                "updated_at": self.updated_at, **self.extra}


@dataclass
class AgentEvent:
    type: str  # started|delta|tool_activity|permission_request|completed|failed|cancelled|notice
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnContext:
    """Everything the adapter needs for one app-driven turn."""

    prompt: str
    workspace: Path
    native_session_id: str | None
    title: str | None = None
    cwd: Path | None = None  # overrides workspace cwd (preview copies)
    sandbox: str | None = None
    model: str | None = None     # provider model id; None = provider default
    variant: str | None = None   # reasoning effort / variant; None = default
    attachments: list[Path] = field(default_factory=list)  # images attached to the prompt (e.g. the image discussed)
    # run the provider's CLI on an SSH host (PR Development on a remote host): development.targets.RemoteExec
    remote: Any = None
    # skill folders of the connected folders / projects (OpenCode loads them via its config for the turn)
    skill_roots: list[Path] = field(default_factory=list)


class TurnHandle(ABC):
    """A running turn. events() yields AgentEvent until a terminal event is produced."""

    @abstractmethod
    def events(self) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    async def respond_permission(self, request_id: str, decision: str) -> None:
        raise ProviderError("PERMISSION_RELAY_UNSUPPORTED", "this provider mode cannot relay permission decisions")


class AgentAdapter(ABC):
    name: str

    @abstractmethod
    async def capabilities(self) -> Capabilities: ...

    @abstractmethod
    async def list_sessions(self, workspace: Path) -> list[NativeSession]: ...

    @abstractmethod
    async def create_session(self, workspace: Path, title: str | None = None) -> str | None:
        """Return the native session id, or None when the provider allocates it on first turn."""

    @abstractmethod
    async def resume_turn(self, ctx: TurnContext) -> TurnHandle: ...

    async def list_models(self) -> list[dict[str, Any]]:
        """Models selectable for app-driven turns: [{id, label, default?}]. Empty when the provider cannot list."""
        return []

    def variants(self) -> list[str]:
        """Reasoning-effort / variant names the provider accepts (empty when unsupported)."""
        return []

    async def default_model(self, workspace: Path | None = None) -> dict[str, str | None]:
        """The model (and variant) the provider uses when the app passes none — read from the provider's own
        configuration. `{"model": id | None, "variant": name | None}`; None values when it cannot be determined."""
        return {"model": None, "variant": None}

    async def session_exists(self, workspace: Path, native_id: str) -> bool | None:
        """True/False when determinable, None when the provider cannot tell."""
        try:
            sessions = await self.list_sessions(workspace)
        except ProviderError:
            return None
        return any(s.native_id == native_id for s in sessions)


# ---------------------------------------------------------------------- subprocess helper


def resolve_executable(configured: str | None, default_name: str) -> str | None:
    cand = configured or default_name
    if os.path.sep in cand:
        return cand if os.access(cand, os.X_OK) else None
    return shutil.which(cand)


# ---------------------------------------------------------------------- agent environment script

_env_script: str | None = None
_env_cache: tuple[tuple[float, int] | None, dict[str, str]] = (None, {})


def configure_env_script(path: str | None) -> None:
    """[agents] env_script: a shell script sourced for every agent process (e.g. one that exports API tokens)."""
    global _env_script, _env_cache
    _env_script = str(Path(path).expanduser()) if path else None
    _env_cache = (None, {})


def _script_vars() -> dict[str, str]:
    """Variables the script sets or changes, re-read whenever the script file changes (tokens can be rotated
    without restarting the server)."""
    global _env_cache
    if not _env_script:
        return {}
    try:
        st = os.stat(_env_script)
    except OSError:
        return {}
    stamp = (st.st_mtime, st.st_size)
    if _env_cache[0] == stamp:
        return _env_cache[1]
    try:
        p = subprocess.run(["bash", "-c", 'set -a; . "$1" >/dev/null 2>&1; env -0', "_", _env_script],
                           capture_output=True, timeout=30, check=False, env=dict(os.environ))
    except (OSError, subprocess.TimeoutExpired):
        return _env_cache[1]
    out: dict[str, str] = {}
    for item in p.stdout.split(b"\0"):
        k, sep, v = item.decode("utf-8", "replace").partition("=")
        if sep and k and k not in ("_", "SHLVL", "PWD", "OLDPWD") and os.environ.get(k) != v:
            out[k] = v
    _env_cache = (stamp, out)
    return out


def agent_env(base: dict[str, str] | None = None) -> dict[str, str] | None:
    """The environment for an agent process: `base` (default: the server's) plus the env script's variables.
    None when there is nothing to add (inherit)."""
    extra = _script_vars()
    if not extra:
        return base
    return {**(base if base is not None else os.environ), **extra}


async def run_capture(args: list[str], cwd: Path | None = None, timeout: float = 30, stdin: bytes | None = None,
                      env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a process with an argv array; never a shell string."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=agent_env(env),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProviderError("PROVIDER_UNAVAILABLE", f"{args[0]} timed out after {timeout}s")
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class SubprocessTurn(TurnHandle):
    """Common machinery for JSONL-emitting CLI turns."""

    def __init__(self, args: list[str], cwd: Path, stdin_data: bytes | None = None, env: dict[str, str] | None = None,
                 remote: Any = None, provider: str | None = None):
        if remote is not None:  # the same command line, run on the SSH host in remote.cwd (stdin/stdout go through ssh)
            args = remote.wrap(args, provider)
            cwd = Path.home()
        self.args = args
        self.cwd = cwd
        self.stdin_data = stdin_data
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self.stderr_tail: list[str] = []
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.args,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE if self.stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=agent_env(self.env),
            start_new_session=True,
        )
        if self.stdin_data is not None:
            assert self.proc.stdin is not None
            try:
                self.proc.stdin.write(self.stdin_data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.proc.stdin.close()
        self._stderr_task = asyncio.get_running_loop().create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text[:2000])
                if len(self.stderr_tail) > 50:
                    self.stderr_tail.pop(0)

    async def lines(self) -> AsyncIterator[str]:
        assert self.proc and self.proc.stdout
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # extremely long line; skip
                continue
            if not raw:
                break
            yield raw.decode("utf-8", "replace").rstrip("\n")

    async def wait(self) -> int:
        """Wait for exit, finish draining stderr and release the transport (no GC-time cleanup)."""
        assert self.proc
        rc = await self.proc.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
        self._close_transport()
        return rc

    def _close_transport(self) -> None:
        transport = getattr(self.proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # pragma: no cover - best effort
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def cancel(self) -> None:
        self._cancelled = True
        if not self.proc or self.proc.returncode is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stderr_text(self) -> str:
        return "\n".join(self.stderr_tail[-12:])


def classify_error(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("401", "unauthorized", "not logged in", "login", "api key", "invalid_api_key",
                            "authentication", "credentials", "please run `codex login`", "please sign in")):
        return "PROVIDER_AUTH_REQUIRED"
    if any(k in t for k in ("busy", "locked", "already running", "in use")):
        return "SESSION_BUSY"
    if any(k in t for k in ("session not found", "no such session", "could not find session", "unknown session",
                            "thread not found", "no rollout", "not found: ses")):
        return "SESSION_NOT_FOUND"
    return "PROVIDER_FAILED"

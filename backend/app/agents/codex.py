"""Codex adapter: `codex exec --json` JSONL events and `codex exec resume <id>`.

* New turn:    codex exec --json --skip-git-repo-check -C WS -s <sandbox> -   (prompt on stdin)
* Resume turn: codex exec resume --json --skip-git-repo-check -c sandbox_mode="<sandbox>" <id> -
  (`exec resume` has no -s/-C flags in 0.155; the sandbox is passed as a config override and
  the process runs with cwd=WS.)
* Session list: Codex has no non-interactive list command; the adapter reads
  `$CODEX_HOME/session_index.jsonl` and the `sessions/**/rollout-*.jsonl` session_meta lines.

Events (documented): thread.started, turn.started, item.started/updated/completed,
turn.completed, turn.failed, error. Agent messages arrive as whole items, not token deltas.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

from ..config import ProviderConfig
from .base import (
    AgentAdapter,
    AgentEvent,
    Capabilities,
    NativeSession,
    ProviderError,
    SubprocessTurn,
    TurnContext,
    TurnHandle,
    classify_error,
    resolve_executable,
    run_capture,
)

VALID_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


class CodexTurn(SubprocessTurn):
    async def events(self) -> AsyncIterator[AgentEvent]:
        await self.start()
        thread_id: str | None = None
        texts: dict[str, str] = {}
        order: list[str] = []
        failed: AgentEvent | None = None
        completed = False
        last_error: str | None = None
        async for line in self.lines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")
            if etype == "thread.started":
                thread_id = ev.get("thread_id")
                if thread_id:
                    yield AgentEvent("started", {"native_session_id": thread_id})
            elif etype.startswith("item."):
                item = ev.get("item") or {}
                itype = item.get("type")
                iid = item.get("id") or f"i{len(order)}"
                if itype == "agent_message":
                    if iid not in texts:
                        order.append(iid)
                    texts[iid] = item.get("text", "")
                    yield AgentEvent("delta", {"part_id": iid, "text": texts[iid], "replace": True})
                elif itype in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list", "reasoning"):
                    yield AgentEvent("tool_activity", {
                        "tool": itype, "status": item.get("status") or etype.split(".")[-1],
                        "title": item.get("command") or (item.get("changes") and json.dumps(item.get("changes"))[:300]) or item.get("server"),
                    })
                elif itype == "error":
                    last_error = item.get("message") or json.dumps(item)[:500]
            elif etype == "turn.completed":
                completed = True
            elif etype == "turn.failed":
                err = ev.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                failed = AgentEvent("failed", {"code": classify_error(str(msg)), "message": str(msg)[:4000]})
            elif etype == "error":
                last_error = ev.get("message") or json.dumps(ev)[:500]
        rc = await self.wait()
        if self.cancelled:
            yield AgentEvent("cancelled", {})
            return
        if failed is not None:
            yield failed
            return
        full = "\n\n".join(texts[p] for p in order if texts[p])
        if completed and (full or rc == 0):
            yield AgentEvent("completed", {"text": full, "exit_code": rc})
            return
        err = last_error or self.stderr_text() or f"codex exited with status {rc}"
        code = classify_error(err)
        if thread_id is None and "resume" in self.args and code == "PROVIDER_FAILED":
            code = "SESSION_NOT_FOUND"
        yield AgentEvent("failed", {"code": code, "message": err[:4000], "exit_code": rc})


class CodexAdapter(AgentAdapter):
    name = "codex"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.exe = resolve_executable(cfg.bin, "codex")
        self._caps: Capabilities | None = None

    async def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        caps = Capabilities(provider="codex", available=False, mode="cli", executable=self.exe,
                            can_list_sessions=True, can_resume=True, can_stream_deltas=False, can_cancel=True, can_select_model=True)
        if not self.exe:
            caps.error = "codex executable not found on PATH; install Codex CLI or set agents.codex.bin"
            self._caps = caps
            return caps
        try:
            rc, out, err = await run_capture([self.exe, "--version"], timeout=20)
            caps.version = (out or err).strip().splitlines()[0] if (out or err).strip() else None
            caps.available = rc == 0
            if rc != 0:
                caps.error = f"`codex --version` failed: {(err or out).strip()[:300]}"
        except (ProviderError, OSError) as exc:
            caps.error = str(exc)
        if self.cfg.sandbox not in VALID_SANDBOXES:
            caps.notes.append(f"configured codex sandbox '{self.cfg.sandbox}' is not one of {VALID_SANDBOXES}")
        caps.notes.append(f"App-driven turns use sandbox '{self.cfg.sandbox}' scoped to the workspace (agents.codex.sandbox).")
        caps.notes.append("Codex emits whole agent messages, not token deltas. Session list is read from $CODEX_HOME session files.")
        caps.notes.append("Codex exec cannot relay interactive approvals; approval-requiring actions fail visibly and the terminal remains available.")
        self._caps = caps
        return caps

    async def list_sessions(self, workspace: Path) -> list[NativeSession]:
        home = codex_home()
        out: dict[str, NativeSession] = {}
        index = home / "session_index.jsonl"
        if index.exists():
            try:
                for line in index.read_text("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = d.get("id")
                    if sid:
                        out[sid] = NativeSession(native_id=sid, title=d.get("thread_name"), directory=None, updated_at=d.get("updated_at"))
            except OSError:
                pass
        sessions_dir = home / "sessions"
        if sessions_dir.exists():
            files = sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:400]
            for f in files:
                try:
                    with f.open("r", encoding="utf-8", errors="replace") as fh:
                        first = fh.readline()
                except OSError:
                    continue
                try:
                    d = json.loads(first)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "session_meta":
                    continue
                p = d.get("payload") or {}
                sid = p.get("id") or p.get("session_id")
                if not sid:
                    continue
                existing = out.get(sid)
                ns = NativeSession(
                    native_id=sid,
                    title=existing.title if existing else None,
                    directory=p.get("cwd"),
                    updated_at=(existing.updated_at if existing and existing.updated_at else d.get("timestamp")),
                    extra={"originator": p.get("originator")},
                )
                out[sid] = ns
        if not home.exists():
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Codex home {home} does not exist; run `codex login` in the terminal first")
        ws = str(workspace.resolve())
        result = [s for s in out.values()]
        result.sort(key=lambda s: ((s.directory or "") != ws, s.updated_at or ""), reverse=False)
        # most recent first within each group
        result.sort(key=lambda s: s.updated_at or "", reverse=True)
        result.sort(key=lambda s: (s.directory or "") != ws)
        return result

    async def create_session(self, workspace: Path, title: str | None = None) -> str | None:
        return None  # thread id is allocated by `codex exec` on the first turn

    async def list_models(self) -> list[dict[str, Any]]:
        """Codex has no model-listing command; offer the configured default plus well-known ids and remember the
        user's recent choices (any free-text id is accepted by the UI)."""
        out: list[dict[str, Any]] = []
        try:
            import tomllib

            cfg = tomllib.loads((codex_home() / "config.toml").read_text("utf-8"))
            default = cfg.get("model")
            if default:
                out.append({"id": default, "label": f"{default} (configured default)", "default": True})
            for name, prof in (cfg.get("profiles") or {}).items():
                if isinstance(prof, dict) and prof.get("model") and prof["model"] != default:
                    out.append({"id": prof["model"], "label": f"{prof['model']} (profile {name})"})
        except (OSError, ValueError):
            pass
        for mid in ("gpt-5", "gpt-5-codex", "o3", "o4-mini", "gpt-4.1"):
            if not any(m["id"] == mid for m in out):
                out.append({"id": mid, "label": mid})
        return out

    def variants(self) -> list[str]:
        return ["minimal", "low", "medium", "high"]

    def _sandbox(self, ctx: TurnContext) -> str:
        sb = ctx.sandbox or self.cfg.sandbox or "workspace-write"
        if sb not in VALID_SANDBOXES:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"invalid codex sandbox '{sb}'; use one of {', '.join(VALID_SANDBOXES)}")
        return sb

    async def resume_turn(self, ctx: TurnContext) -> TurnHandle:
        if not self.exe:
            raise ProviderError("PROVIDER_UNAVAILABLE", "codex executable not found")
        cwd = ctx.cwd or ctx.workspace
        sandbox = self._sandbox(ctx)
        common = ["--json", "--skip-git-repo-check"]
        if self.cfg.profile:
            common += ["--profile", self.cfg.profile]
        if ctx.model:
            common += ["-m", ctx.model]
        if ctx.variant:
            common += ["-c", f'model_reasoning_effort="{ctx.variant}"']
        common += list(self.cfg.extra_args)
        if ctx.native_session_id:
            args = [self.exe, "exec", "resume", *common, "-c", f'sandbox_mode="{sandbox}"', ctx.native_session_id, "-"]
        else:
            args = [self.exe, "exec", *common, "-C", str(cwd), "-s", sandbox, "-"]
        return CodexTurn(args, cwd=cwd, stdin_data=ctx.prompt.encode("utf-8"))

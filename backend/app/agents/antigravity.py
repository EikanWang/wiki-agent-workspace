"""Google Antigravity adapter: the `agy` CLI in print mode with NDJSON events.

* New turn:    agy --input-format stream-json --output-format stream-json -p= --dangerously-skip-permissions
               (cwd = WS; the prompt on stdin as one NDJSON line {"event": "user", "message": {"content": …}})
* Resume turn: the same with --conversation <id>
* Read-only:   --mode plan instead of auto-approving tools (locked documents, PR priority)
* Sessions:    $ANTIGRAVITY_HOME (default ~/.gemini/antigravity-cli)/conversation_summaries.db (SQLite)

Events: init (conversation_id), step_update (user_input / agent_response with text_delta / tool with tool_info),
result (status, response). Text arrives as deltas per agent_response step.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

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

EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def antigravity_home() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_HOME") or (Path.home() / ".gemini" / "antigravity-cli"))


def _uri_path(uri: str) -> str:
    u = urlparse(uri)
    return unquote(u.path) if u.scheme == "file" else uri


class AntigravityTurn(SubprocessTurn):
    async def events(self) -> AsyncIterator[AgentEvent]:
        await self.start()
        conv: str | None = None
        texts: dict[str, str] = {}
        order: list[str] = []
        result: dict[str, Any] | None = None
        last_error: str | None = None
        async for line in self.lines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            if kind == "init":
                conv = ev.get("conversation_id") or (ev.get("init") or {}).get("conversation_id")
                if conv:
                    yield AgentEvent("started", {"native_session_id": conv})
            elif kind == "step_update":
                st = ev.get("step_update") or {}
                stype, idx = st.get("step_type"), str(st.get("step_index", len(order)))
                if stype == "agent_response" and st.get("text_delta"):
                    if idx not in texts:
                        order.append(idx)
                        texts[idx] = ""
                    texts[idx] += st["text_delta"]
                    yield AgentEvent("delta", {"part_id": idx, "text": texts[idx], "replace": True})
                elif stype == "tool":
                    info = st.get("tool_info") or {}
                    params = info.get("parameters") or {}
                    title = (params.get("CommandLine") or params.get("Path") or params.get("Query")
                             or (json.dumps(params)[:300] if params else None))
                    yield AgentEvent("tool_activity", {"tool": st.get("tool_name") or info.get("name") or "tool",
                                                       "status": "completed" if st.get("state") == "DONE" else "running",
                                                       "title": title})
                if st.get("error") or st.get("error_message"):
                    last_error = str(st.get("error") or st.get("error_message"))[:2000]
            elif kind == "result":
                result = ev.get("result") or {}
            elif kind == "error":
                last_error = str(ev.get("error") or ev.get("message") or json.dumps(ev))[:2000]
        rc = await self.wait()
        if self.cancelled:
            yield AgentEvent("cancelled", {})
            return
        full = "\n\n".join(texts[p].strip() for p in order if texts[p].strip())
        if result is not None and str(result.get("status", "")).upper() == "SUCCESS":
            yield AgentEvent("completed", {"text": full or str(result.get("response") or "").strip(), "exit_code": rc})
            return
        err = (last_error or (result and (result.get("error") or result.get("status")) and json.dumps(result)[:2000])
               or self.stderr_text() or f"agy exited with status {rc}")
        code = classify_error(str(err))
        if conv is None and "--conversation" in self.args and code == "PROVIDER_FAILED":
            code = "SESSION_NOT_FOUND"
        yield AgentEvent("failed", {"code": code, "message": str(err)[:4000], "exit_code": rc})


class AntigravityAdapter(AgentAdapter):
    name = "antigravity"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.exe = resolve_executable(cfg.bin, "agy")
        self._caps: Capabilities | None = None
        self._models: list[dict[str, Any]] | None = None

    async def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        caps = Capabilities(provider="antigravity", available=False, mode="cli", executable=self.exe,
                            can_list_sessions=True, can_resume=True, can_stream_deltas=True, can_cancel=True,
                            can_select_model=True)
        if not self.exe:
            caps.error = "agy (Google Antigravity CLI) not found on PATH; install it or set agents.antigravity.bin"
            self._caps = caps
            return caps
        try:
            rc, out, err = await run_capture([self.exe, "--version"], timeout=20)
            text = (out or err).strip()
            caps.version = text.splitlines()[0] if text else None
            caps.available = rc == 0
            if rc != 0:
                caps.error = f"`agy --version` failed: {text[:300]}"
        except (ProviderError, OSError) as exc:
            caps.error = str(exc)
        caps.notes.append("App-driven turns auto-approve tool use in the workspace (--dangerously-skip-permissions); "
                          "read-only turns use --mode plan.")
        caps.notes.append("Sessions are read from the Antigravity CLI's conversation_summaries.db.")
        self._caps = caps
        return caps

    async def list_sessions(self, workspace: Path) -> list[NativeSession]:
        db = antigravity_home() / "conversation_summaries.db"
        if not db.exists():
            raise ProviderError("PROVIDER_UNAVAILABLE", f"{db} does not exist; run `agy` once in the terminal to log in")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute("SELECT conversation_id, title, preview, workspace_uris, last_modified_time FROM conversation_summaries "
                                   "WHERE parent_conversation_id = '' ORDER BY last_modified_time DESC LIMIT 400").fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise ProviderError("PROVIDER_FAILED", f"could not read Antigravity conversations: {exc}") from exc
        out: list[NativeSession] = []
        for cid, title, preview, uris, modified in rows:
            try:
                dirs = [_uri_path(u) for u in json.loads(uris or "[]")]
            except (ValueError, TypeError):
                dirs = []
            out.append(NativeSession(native_id=cid, title=(title or preview or None), directory=dirs[0] if dirs else None,
                                     updated_at=str(modified)[:19].replace(" ", "T") if modified else None))
        ws = str(workspace.resolve())
        out.sort(key=lambda s: (s.directory or "") != ws)  # stable: newest first within each group
        return out

    async def create_session(self, workspace: Path, title: str | None = None) -> str | None:
        return None  # the conversation id is allocated by the first turn (init event)

    async def list_models(self) -> list[dict[str, Any]]:
        if self._models is None and self.exe:
            try:
                rc, out, _ = await run_capture([self.exe, "models"], timeout=60)
            except (ProviderError, OSError):
                rc, out = 1, ""
            models = []
            for line in out.splitlines():
                mid, _, label = line.partition("\t")
                if rc == 0 and mid.strip() and label.strip():
                    models.append({"id": mid.strip(), "label": label.strip()})
            self._models = models or None
        return list(self._models or [])

    def variants(self) -> list[str]:
        return list(EFFORTS)

    @staticmethod
    def _model_effort(model: str | None, variant: str | None, models: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        """agy's model ids carry the reasoning level (gemini-4-carbon-high), and `--effort` must be one the model has
        (Carbon: low / medium / high; `--model X-high --effort max` is refused). A chosen level picks the model id
        with that level when it exists; a level the model lacks is dropped (the model id's own level is used)."""
        ids = {m["id"] for m in models}
        if not model:
            # the default model is unknown here: only levels every model has
            return None, variant if variant in ("low", "medium", "high") else None
        if not variant:
            return model, None
        base = model
        for e in sorted(EFFORTS, key=len, reverse=True):
            if model.endswith("-" + e):
                base = model[: -len(e) - 1]
                break
        if f"{base}-{variant}" in ids:
            return f"{base}-{variant}", None
        if not ids and base == model:  # no model list: let agy decide
            return model, variant
        return model, None

    async def resume_turn(self, ctx: TurnContext) -> TurnHandle:
        if not self.exe and ctx.remote is None:
            raise ProviderError("PROVIDER_UNAVAILABLE", "agy (Google Antigravity CLI) executable not found")
        exe = self.exe or "agy"
        cwd = ctx.cwd or ctx.workspace
        args = [exe, "--input-format", "stream-json", "--output-format", "stream-json", "-p="]
        if ctx.sandbox == "read-only":
            args += ["--mode", "plan"]
        else:
            args += ["--dangerously-skip-permissions"]
        if self.cfg.profile:
            args += ["--agent", self.cfg.profile]
        model, effort = self._model_effort(ctx.model, ctx.variant, await self.list_models())
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        if ctx.native_session_id:
            args += ["--conversation", ctx.native_session_id]
        args += list(self.cfg.extra_args)
        prompt = ctx.prompt
        if ctx.attachments:  # no image flag: name the files so the agent opens them with its file tools
            prompt += "\n\nAttached images (open them with your file tools): " + ", ".join(str(p) for p in ctx.attachments)
        line = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        return AntigravityTurn(args, cwd=cwd, stdin_data=line.encode("utf-8"), remote=ctx.remote, provider="antigravity")

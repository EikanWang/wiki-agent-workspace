"""OpenCode adapters.

* CLI mode: `opencode run --format json [--session ID] --dir WS <prompt>` and
  `opencode session list --format json`. Each JSON line carries `type`, `sessionID` and a
  `part` (text/tool/step events). Observed with opencode 1.18.x.
* Server mode: a known loopback `opencode serve` instance (config `server_url`). Uses the
  documented HTTP API (`/api/session`, `/api/session/{id}/prompt`, `/api/event` SSE,
  `/api/session/{id}/permission/{rid}/reply`, `/api/session/{id}/interrupt`).
  Starting a separate server does not attach to a TUI's server; configure one instance
  and point both the terminal (`opencode --attach`/`--port`) and this app at it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

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


def _ms_to_iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


class OpenCodeCliTurn(SubprocessTurn):
    async def events(self) -> AsyncIterator[AgentEvent]:
        await self.start()
        native_id: str | None = None
        failed: AgentEvent | None = None
        texts: dict[str, str] = {}
        order: list[str] = []
        async for line in self.lines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = ev.get("sessionID") or (ev.get("part") or {}).get("sessionID")
            if sid and native_id is None:
                native_id = sid
                yield AgentEvent("started", {"native_session_id": sid})
            etype = ev.get("type")
            part = ev.get("part") or {}
            if etype == "text" or part.get("type") == "text":
                pid = part.get("id") or f"p{len(order)}"
                if pid not in texts:
                    order.append(pid)
                texts[pid] = part.get("text", "")
                yield AgentEvent("delta", {"part_id": pid, "text": texts[pid], "replace": True})
            elif etype == "tool" or part.get("type") == "tool":
                state = part.get("state") or {}
                yield AgentEvent("tool_activity", {
                    "tool": part.get("tool"), "status": state.get("status"),
                    "title": state.get("title"), "call_id": part.get("callID"),
                })
            elif etype == "permission" or part.get("type") == "permission":
                yield AgentEvent("permission_request", {
                    "request_id": ev.get("id") or part.get("id"),
                    "relay_supported": False,
                    "description": part.get("title") or ev.get("title") or "OpenCode requested a permission",
                    "options": [],
                    "raw": ev,
                })
            elif etype == "step_finish" or part.get("type") == "step-finish":
                if part.get("reason") in (None, "stop", "end_turn", "end-turn", "length", "tool-calls"):
                    pass
            elif etype == "error":
                msg = ev.get("error") or ev.get("message") or line
                if isinstance(msg, dict):
                    msg = msg.get("data", {}).get("message") or msg.get("message") or json.dumps(msg)
                failed = AgentEvent("failed", {"code": classify_error(str(msg)), "message": str(msg)[:4000]})
        rc = await self.wait()
        if self.cancelled:
            yield AgentEvent("cancelled", {})
            return
        if failed is not None:
            yield failed
            return
        full = "".join(texts[p] for p in order)
        if rc != 0 and not full:
            err = self.stderr_text() or f"opencode exited with status {rc}"
            yield AgentEvent("failed", {"code": classify_error(err), "message": err[:4000], "exit_code": rc})
            return
        if native_id is None and not full:
            err = self.stderr_text() or "opencode produced no output"
            yield AgentEvent("failed", {"code": classify_error(err), "message": err[:4000], "exit_code": rc})
            return
        yield AgentEvent("completed", {"text": full, "exit_code": rc})


class OpenCodeCliAdapter(AgentAdapter):
    name = "opencode"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.exe = resolve_executable(cfg.bin, "opencode")
        self._caps: Capabilities | None = None

    async def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        caps = Capabilities(provider="opencode", available=False, mode="cli", executable=self.exe,
                            can_stream_deltas=False, can_cancel=True, can_resume=True, can_list_sessions=True)
        if not self.exe:
            caps.error = "opencode executable not found on PATH; install OpenCode or set agents.opencode.bin"
            self._caps = caps
            return caps
        try:
            rc, out, err = await run_capture([self.exe, "--version"], timeout=20)
            caps.version = (out or err).strip().splitlines()[0] if (out or err).strip() else None
            caps.available = rc == 0
            if rc != 0:
                caps.error = f"`opencode --version` failed: {(err or out).strip()[:300]}"
        except (ProviderError, OSError) as exc:
            caps.error = str(exc)
        caps.notes.append("CLI JSON mode reports whole text parts, not token deltas.")
        caps.notes.append("Permission prompts cannot be relayed in CLI mode; OpenCode applies its configured permission rules.")
        self._caps = caps
        return caps

    async def list_sessions(self, workspace: Path) -> list[NativeSession]:
        if not self.exe:
            raise ProviderError("PROVIDER_UNAVAILABLE", "opencode executable not found")
        rc, out, err = await run_capture([self.exe, "session", "list", "--format", "json", "-n", "200"], cwd=workspace, timeout=60)
        if rc != 0:
            raise ProviderError(classify_error(err), f"opencode session list failed: {err.strip()[:500] or rc}")
        start = out.find("[")
        if start < 0:
            return []
        try:
            data = json.loads(out[start:])
        except json.JSONDecodeError as exc:
            raise ProviderError("PROVIDER_FAILED", f"could not parse opencode session list: {exc}")
        out_sessions: list[NativeSession] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            out_sessions.append(NativeSession(
                native_id=str(item["id"]), title=item.get("title"), directory=item.get("directory"),
                updated_at=_ms_to_iso(item.get("updated")),
            ))
        return out_sessions

    async def create_session(self, workspace: Path, title: str | None = None) -> str | None:
        # The CLI allocates the session id when the first turn starts.
        return None

    async def resume_turn(self, ctx: TurnContext) -> TurnHandle:
        if not self.exe:
            raise ProviderError("PROVIDER_UNAVAILABLE", "opencode executable not found")
        args = [self.exe, "run", "--format", "json", "--dir", str(ctx.cwd or ctx.workspace)]
        if ctx.native_session_id:
            args += ["--session", ctx.native_session_id]
        elif ctx.title:
            args += ["--title", ctx.title[:120]]
        if self.cfg.profile:
            args += ["--agent", self.cfg.profile]
        args += list(self.cfg.extra_args)
        args.append(ctx.prompt)
        return OpenCodeCliTurn(args, cwd=ctx.cwd or ctx.workspace)


# ---------------------------------------------------------------------- server mode


class OpenCodeServerTurn(TurnHandle):
    def __init__(self, adapter: "OpenCodeServerAdapter", ctx: TurnContext):
        self.adapter = adapter
        self.ctx = ctx
        self.native_id = ctx.native_session_id
        self._cancelled = False
        self._done = asyncio.Event()

    async def events(self) -> AsyncIterator[AgentEvent]:
        a = self.adapter
        client = a.client()
        try:
            if self.native_id is None:
                self.native_id = await a.create_session(self.ctx.workspace, self.ctx.title)
            assert self.native_id
            active = await a._active_sessions()
            if active is not None and self.native_id in active:
                yield AgentEvent("failed", {"code": "SESSION_BUSY", "message": "OpenCode reports this session is already running a turn"})
                return
            yield AgentEvent("started", {"native_session_id": self.native_id})
            texts: dict[str, str] = {}
            order: list[str] = []
            async with client.stream("GET", a.api("/event"), timeout=httpx.Timeout(None, connect=10)) as resp:
                if resp.status_code != 200:
                    yield AgentEvent("failed", {"code": "PROVIDER_UNAVAILABLE", "message": f"event stream returned HTTP {resp.status_code}"})
                    return
                # Submit the prompt once the stream is open so no event is missed.
                r = await client.post(a.api(f"/session/{self.native_id}/prompt"),
                                      json={"prompt": {"text": self.ctx.prompt}}, timeout=60)
                if r.status_code == 404:
                    yield AgentEvent("failed", {"code": "SESSION_NOT_FOUND", "message": f"OpenCode server has no session {self.native_id}"})
                    return
                if r.status_code == 409:
                    yield AgentEvent("failed", {"code": "SESSION_BUSY", "message": r.text[:500]})
                    return
                if r.status_code >= 400:
                    yield AgentEvent("failed", {"code": classify_error(r.text), "message": f"HTTP {r.status_code}: {r.text[:500]}"})
                    return
                # 200/204 only means the prompt was accepted; wait for events.
                async for line in resp.aiter_lines():
                    if self._cancelled:
                        yield AgentEvent("cancelled", {})
                        return
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("type", "")
                    props = ev.get("properties") or ev.get("data") or {}
                    sid = props.get("sessionID") or (props.get("info") or {}).get("sessionID") or (props.get("part") or {}).get("sessionID")
                    if sid != self.native_id:
                        continue
                    if etype == "session.next.text.delta":
                        pid = props.get("textID") or "t"
                        if pid not in texts:
                            order.append(pid)
                            texts[pid] = ""
                        texts[pid] += props.get("delta", "")
                        yield AgentEvent("delta", {"part_id": pid, "text": props.get("delta", ""), "replace": False})
                    elif etype == "message.part.delta" and props.get("field") == "text":
                        pid = props.get("partID") or "t"
                        if pid not in texts:
                            order.append(pid)
                            texts[pid] = ""
                        texts[pid] += props.get("delta", "")
                        yield AgentEvent("delta", {"part_id": pid, "text": props.get("delta", ""), "replace": False})
                    elif etype == "message.part.updated":
                        part = props.get("part") or {}
                        if part.get("type") == "text":
                            pid = part.get("id") or "t"
                            if pid not in texts:
                                order.append(pid)
                            texts[pid] = part.get("text", "")
                            yield AgentEvent("delta", {"part_id": pid, "text": texts[pid], "replace": True})
                        elif part.get("type") == "tool":
                            st = part.get("state") or {}
                            yield AgentEvent("tool_activity", {"tool": part.get("tool"), "status": st.get("status"), "title": st.get("title")})
                    elif etype == "session.next.tool.called":
                        yield AgentEvent("tool_activity", {"tool": props.get("tool"), "status": "running", "call_id": props.get("callID")})
                    elif etype in ("session.next.tool.success", "session.next.tool.failed"):
                        yield AgentEvent("tool_activity", {"tool": props.get("tool"), "status": etype.rsplit(".", 1)[-1], "call_id": props.get("callID")})
                    elif etype in ("permission.v2.asked", "permission.asked"):
                        yield AgentEvent("permission_request", {
                            "request_id": props.get("id"),
                            "relay_supported": True,
                            "description": f"{props.get('action') or props.get('permission') or 'permission'}: "
                                           + ", ".join(props.get("resources") or props.get("patterns") or []),
                            "options": ["once", "always", "reject"],
                            "raw": props,
                        })
                    elif etype in ("permission.v2.replied", "permission.replied"):
                        yield AgentEvent("notice", {"message": f"permission {props.get('id')} replied: {props.get('reply') or props.get('response')}"})
                    elif etype in ("session.next.step.failed", "session.error"):
                        err = props.get("error") or {}
                        msg = err.get("message") or (err.get("data") or {}).get("message") or json.dumps(err)[:500]
                        code = "PROVIDER_AUTH_REQUIRED" if err.get("name") == "ProviderAuthError" else classify_error(str(msg))
                        yield AgentEvent("failed", {"code": code, "message": str(msg)[:4000]})
                        return
                    elif etype == "session.idle" or (etype == "session.status" and (props.get("status") or {}).get("type") == "idle"):
                        full = "".join(texts[p] for p in order)
                        if not full:
                            # Look up the final assistant message text.
                            full = await a._last_assistant_text(self.native_id)
                        yield AgentEvent("completed", {"text": full})
                        return
        except httpx.HTTPError as exc:
            yield AgentEvent("failed", {"code": "PROVIDER_UNAVAILABLE", "message": f"OpenCode server unreachable: {exc}"})
        except ProviderError as exc:
            yield AgentEvent("failed", {"code": exc.code, "message": exc.message})
        finally:
            self._done.set()

    async def cancel(self) -> None:
        self._cancelled = True
        if self.native_id:
            try:
                await self.adapter.client().post(self.adapter.api(f"/session/{self.native_id}/interrupt"), timeout=10)
            except httpx.HTTPError:
                pass

    async def respond_permission(self, request_id: str, decision: str) -> None:
        if decision not in ("once", "always", "reject"):
            raise ProviderError("INVALID_DECISION", "decision must be one of once, always, reject")
        r = await self.adapter.client().post(
            self.adapter.api(f"/session/{self.native_id}/permission/{request_id}/reply"), json={"reply": decision}, timeout=20
        )
        if r.status_code == 404:
            raise ProviderError("PERMISSION_STALE", "OpenCode no longer has that permission request")
        if r.status_code >= 400:
            raise ProviderError("PROVIDER_FAILED", f"permission reply rejected: HTTP {r.status_code} {r.text[:300]}")


class OpenCodeServerAdapter(AgentAdapter):
    name = "opencode"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.base = (cfg.server_url or "").rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._prefix = "/api"
        self._caps: Capabilities | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base, timeout=30)
        return self._client

    def api(self, path: str) -> str:
        return f"{self._prefix}{path}"

    async def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        caps = Capabilities(provider="opencode", available=False, mode="server", executable=self.base,
                            can_list_sessions=True, can_resume=True, can_stream_deltas=True, can_cancel=True,
                            can_relay_permissions=True, uses_local_service=True, eager_session_ids=True)
        if not self.base:
            caps.error = "agents.opencode.server_url is not configured"
            self._caps = caps
            return caps
        try:
            r = await self.client().get("/api/health", timeout=5)
            if r.status_code == 404:
                r = await self.client().get("/health", timeout=5)
                if r.status_code == 200:
                    self._prefix = ""
            if r.status_code == 200:
                caps.available = True
                try:
                    caps.version = (r.json().get("data") or r.json()).get("version")
                except Exception:
                    caps.version = None
            else:
                caps.error = f"OpenCode server at {self.base} returned HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            caps.error = f"OpenCode server at {self.base} is unreachable: {exc.__class__.__name__}"
        caps.notes.append("Shared live-session concurrency with a TUI attached to the same server is not verified; the app checks /session/active before each turn.")
        self._caps = caps
        return caps

    def _unwrap(self, r: httpx.Response) -> Any:
        data = r.json()
        if isinstance(data, dict) and "data" in data and len(data) <= 2:
            return data["data"]
        return data

    async def list_sessions(self, workspace: Path) -> list[NativeSession]:
        try:
            r = await self.client().get(self.api("/session"), params={"directory": str(workspace)})
        except httpx.HTTPError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"OpenCode server unreachable: {exc.__class__.__name__}")
        if r.status_code >= 400:
            raise ProviderError("PROVIDER_FAILED", f"session list failed: HTTP {r.status_code}")
        data = self._unwrap(r)
        out: list[NativeSession] = []
        for item in data or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            directory = item.get("directory") or (item.get("location") or {}).get("directory")
            out.append(NativeSession(native_id=item["id"], title=item.get("title"), directory=directory,
                                     updated_at=_ms_to_iso((item.get("time") or {}).get("updated"))))
        return out

    async def _active_sessions(self) -> set[str] | None:
        try:
            r = await self.client().get(self.api("/session/active"), timeout=10)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        data = self._unwrap(r)
        if isinstance(data, dict):
            return set(data.keys())
        if isinstance(data, list):
            return {x.get("id") for x in data if isinstance(x, dict)}
        return None

    async def _last_assistant_text(self, native_id: str) -> str:
        try:
            r = await self.client().get(self.api(f"/session/{native_id}/message"), timeout=20)
        except httpx.HTTPError:
            return ""
        if r.status_code != 200:
            return ""
        data = self._unwrap(r)
        if not isinstance(data, list):
            return ""
        for msg in data:
            info = msg.get("info") or msg
            if info.get("type") == "assistant" or (info.get("role") == "assistant"):
                parts = msg.get("parts") or msg.get("content") or []
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
                if text:
                    return text
        return ""

    async def create_session(self, workspace: Path, title: str | None = None) -> str | None:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title[:120]
        try:
            r = await self.client().post(self.api("/session"), params={"directory": str(workspace)}, json=body)
        except httpx.HTTPError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"OpenCode server unreachable: {exc.__class__.__name__}")
        if r.status_code >= 400:
            raise ProviderError(classify_error(r.text), f"session create failed: HTTP {r.status_code} {r.text[:300]}")
        data = self._unwrap(r)
        sid = data.get("id") if isinstance(data, dict) else None
        if not sid:
            raise ProviderError("PROVIDER_FAILED", "OpenCode server did not return a session id")
        return sid

    async def session_exists(self, workspace: Path, native_id: str) -> bool | None:
        try:
            r = await self.client().get(self.api(f"/session/{native_id}"), timeout=10)
        except httpx.HTTPError:
            return None
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        return None

    async def resume_turn(self, ctx: TurnContext) -> TurnHandle:
        return OpenCodeServerTurn(self, ctx)

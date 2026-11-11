"""Agent monitor: watch OpenCode, Codex and Claude Code sessions — also those started outside the app (a terminal,
another tool) — see their status at a glance, get alarmed when one needs you, read and continue them.

Sessions are read where the agents keep them: OpenCode via `opencode session list` / `opencode export <id>`, Codex
from `$CODEX_HOME/sessions/**/rollout-*-<id>.jsonl`, Claude Code from `$CLAUDE_CONFIG_DIR/projects/*/<id>.jsonl`
(default ~/.claude). Status is derived from the transcript:

* working  — a turn is open and the session showed activity recently
* needs_input — the agent asks you: a question tool / an approval request is pending, a tool call has been waiting
  without progress (likely a permission prompt in a terminal), or the finished turn ends with a question
* error    — the last turn failed
* stalled  — a turn is open but nothing happened for a while
* idle     — the last turn finished

Sending a message runs the provider's CLI non-interactively in the session's folder (`opencode run --session`,
`codex exec resume`, `claude -p --resume`); "Open in terminal" attaches the provider's own TUI to the session.
Settings (kv): `monitor_folders` (extra folders to discover sessions in), `monitor_sessions` (the watched sessions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .agents.base import ProviderError, agent_env, resolve_executable, run_capture
from .agents.codex import codex_home
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found
from .kv import kv_get, kv_put

log = logging.getLogger("waw.monitor")
router = APIRouter()
PROVIDERS = ("opencode", "codex", "claude")
WORKING_FRESH = 15 * 60       # an open turn with activity within this many seconds is "working"
TOOL_WAIT = 90                # a tool call without progress for this long: likely waiting for a permission prompt
MAX_MESSAGES = 400


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _ms(v: Any) -> float | None:
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return None


def _iso_ts(s: Any) -> float | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z") if t else None


def _bin(ctx: AppContext, provider: str) -> str | None:
    return resolve_executable(ctx.settings.provider(provider).bin, provider)


# ---------------------------------------------------------------------- readers


class Snapshot:
    """A session read from disk: where it is, what was said, and what state it is in."""

    def __init__(self, provider: str, sid: str):
        self.provider = provider
        self.id = sid
        self.found = False
        self.title: str | None = None
        self.directory: str | None = None
        self.model: str | None = None
        self.messages: list[dict[str, Any]] = []   # {role, text, tool, status, time}
        self.updated: float | None = None
        self.turn_open = False
        self.turn_started: float | None = None
        self.pending: str | None = None            # why it waits for the user (question / approval / tool wait)
        self.error: str | None = None              # the last turn's error

    def status(self, now: float, sending: bool) -> dict[str, Any]:
        last_text = next((m["text"] for m in reversed(self.messages) if m["role"] == "assistant" and m.get("text")), "")
        age = (now - self.updated) if self.updated else None
        if not self.found:
            state, level, why = "missing", "error", f"no {self.provider} session with this id was found on this machine"
        elif sending:
            state, level, why = "working", "busy", "sending your message"
        elif self.pending:
            state, level, why = "needs_input", "alarm", self.pending
        elif self.turn_open and age is not None and age < WORKING_FRESH:
            state, level, why = "working", "busy", "the agent is working"
        elif self.turn_open:
            state, level, why = "stalled", "warning", f"a turn is open but nothing happened for {int((age or 0) // 60)} min"
        elif self.error:
            state, level, why = "error", "error", self.error
        elif last_text.rstrip().rstrip("*_`)\"'").endswith("?"):
            state, level, why = "needs_input", "alarm", "the agent asked you a question"
        else:
            state, level, why = "idle", "ok", "the last turn finished"
        return {"state": state, "level": level, "reason": why, "last_text": last_text[-600:], "updated_at": _iso(self.updated),
                "messages": len(self.messages)}


def _tail_text(parts: list[str]) -> str:
    return "\n".join(p for p in parts if p).strip()


async def _export(exe: str, sid: str) -> tuple[int, str]:
    """`opencode export` into a file: through a pipe it exits before writing out large sessions (~200 KB)."""
    import tempfile

    with tempfile.TemporaryFile() as fh:
        try:
            proc = await asyncio.create_subprocess_exec(exe, "export", sid, stdout=fh, stderr=asyncio.subprocess.DEVNULL,
                                                        stdin=asyncio.subprocess.DEVNULL, env=agent_env(None), cwd=str(Path.home()))
        except OSError:
            return 1, ""
        try:
            rc = await asyncio.wait_for(proc.wait(), 180)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return 1, ""
        fh.seek(0)
        return rc, fh.read().decode("utf-8", "replace")


async def read_opencode(ctx: AppContext, sid: str, listing: dict[str, dict[str, Any]] | None) -> Snapshot:
    s = Snapshot("opencode", sid)
    exe = _bin(ctx, "opencode")
    if not exe:
        s.error = "opencode is not installed"
        return s
    meta = (listing or {}).get(sid)
    rc, out = await _export(exe, sid)
    start = out.find("{")
    if rc != 0 or start < 0:
        if meta:
            s.found, s.title, s.directory, s.updated = True, meta.get("title"), meta.get("directory"), _ms(meta.get("updated"))
        return s
    try:
        d = json.loads(out[start:])
    except json.JSONDecodeError:
        return s
    info = d.get("info") or {}
    s.found = True
    s.title = info.get("title") or (meta or {}).get("title")
    s.directory = info.get("directory") or (meta or {}).get("directory")
    m = info.get("model") or {}
    s.model = f"{m.get('providerID')}/{m.get('id')}" if m.get("id") else None
    s.updated = _ms((info.get("time") or {}).get("updated")) or _ms((meta or {}).get("updated"))
    now = time.time()
    for msg in d.get("messages") or []:
        mi = msg.get("info") or {}
        role = mi.get("role")
        t = mi.get("time") or {}
        created = _ms(t.get("created"))
        texts: list[str] = []
        for p in msg.get("parts") or []:
            pt = p.get("type")
            if pt == "text" and not p.get("synthetic"):
                texts.append(p.get("text") or "")
            elif pt == "tool":
                st = p.get("state") or {}
                status = st.get("status")
                title = st.get("title") or json.dumps(st.get("input") or {})[:160]
                s.messages.append({"role": "tool", "tool": p.get("tool"), "status": status, "text": str(title)[:400], "time": _iso(created)})
                if status in ("pending", "running") and role == "assistant" and not t.get("completed"):
                    if p.get("tool") == "question":
                        s.pending = "the agent asks you a question"
                    elif now - (_ms((st.get("time") or {}).get("start")) or created or now) > TOOL_WAIT and (s.updated is None or now - s.updated > TOOL_WAIT):
                        s.pending = f"the tool `{p.get('tool')}` is waiting (likely a permission prompt in the terminal)"
        if texts or role == "user":
            s.messages.append({"role": role or "assistant", "text": _tail_text(texts), "time": _iso(created)})
        if role == "assistant":
            s.turn_open = not t.get("completed") and not mi.get("error")
            s.turn_started = created
            err = mi.get("error")
            s.error = ((err.get("data") or {}).get("message") or err.get("name") or "error") if isinstance(err, dict) else None
        elif role == "user":
            s.turn_open = True  # until an assistant answer follows
            s.turn_started = created
            s.error = None
    if s.messages and s.messages[-1]["role"] == "user" and s.updated and now - s.updated > WORKING_FRESH:
        s.turn_open = False  # a user message nobody answered long ago: not running
    return s


def _codex_file(sid: str) -> Path | None:
    root = codex_home() / "sessions"
    if not root.exists():
        return None
    hits = sorted(root.rglob(f"rollout-*{sid}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def read_codex(sid: str) -> Snapshot:
    s = Snapshot("codex", sid)
    f = _codex_file(sid)
    if f is None:
        return s
    s.found = True
    s.updated = f.stat().st_mtime
    approvals: dict[str, str] = {}
    try:
        lines = f.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return s
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ, p, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp")
        if typ == "session_meta":
            s.directory = p.get("cwd")
        elif typ == "turn_context":
            s.model = p.get("model") or s.model
        elif typ == "event_msg":
            et = p.get("type") or ""
            if et == "task_started":
                s.turn_open, s.turn_started, s.error = True, _iso_ts(ts), None
            elif et == "task_complete":
                s.turn_open = False
                err = p.get("error")
                s.error = (err.get("message") if isinstance(err, dict) else str(err))[:500] if err else None
                approvals.clear()
            elif et in ("turn_aborted",):
                s.turn_open = False
            elif "approval_request" in et or et in ("request_user_input", "elicitation_request"):
                approvals[p.get("call_id") or p.get("id") or et] = et.replace("_", " ")
            elif et.endswith("approval") or et.endswith("_decision") or "approval_response" in et:
                approvals.pop(p.get("call_id") or p.get("id") or "", None)
            elif et == "item_completed":
                it = p.get("item") or {}
                if it.get("type") == "UserMessage":
                    text = it.get("text") or " ".join(c.get("text", "") for c in it.get("content") or [] if isinstance(c, dict))
                    s.messages.append({"role": "user", "text": text.strip(), "time": ts})
        elif typ == "response_item":
            pt = p.get("type")
            if pt == "message" and p.get("role") == "assistant":
                text = _tail_text([c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)])
                if text:
                    s.messages.append({"role": "assistant", "text": text, "time": ts})
            elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
                arg = p.get("arguments") or p.get("input") or ""
                try:
                    a = json.loads(arg) if isinstance(arg, str) and arg.startswith("{") else arg
                    arg = a.get("cmd") or a.get("command") or arg if isinstance(a, dict) else arg
                except (ValueError, AttributeError):
                    pass
                s.messages.append({"role": "tool", "tool": p.get("name") or pt, "status": p.get("status") or "called",
                                   "text": (arg if isinstance(arg, str) else json.dumps(arg))[:400], "time": ts})
    if not any(m["role"] == "user" for m in s.messages):  # older rollouts: the user's messages as response items
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = d.get("payload") or {}
            if d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                t = _tail_text([c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)])
                if t and not t.startswith(("<", "# AGENTS.md")):
                    s.messages.append({"role": "user", "text": t, "time": d.get("timestamp")})
    if approvals and s.turn_open:
        s.pending = f"Codex waits for your approval ({next(iter(approvals.values()))})"
    s.title = next((m["text"][:80] for m in s.messages if m["role"] == "user" and m["text"]), None)
    return s


def _claude_file(sid: str) -> Path | None:
    root = claude_home() / "projects"
    if not root.exists():
        return None
    hits = list(root.glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def read_claude(sid: str) -> Snapshot:
    s = Snapshot("claude", sid)
    f = _claude_file(sid)
    if f is None:
        return s
    s.found = True
    s.updated = f.stat().st_mtime
    open_tools: dict[str, tuple[str, float | None]] = {}
    last_kind = None
    stop = None
    try:
        lines = f.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return s
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = d.get("type")
        ts = d.get("timestamp")
        if d.get("cwd"):
            s.directory = d["cwd"]
        if typ in ("summary", "ai-title") and (d.get("summary") or d.get("title")):
            s.title = d.get("summary") or d.get("title")
        if d.get("isSidechain") or typ not in ("user", "assistant"):
            continue
        m = d.get("message") or {}
        content = m.get("content")
        items = content if isinstance(content, list) else [{"type": "text", "text": content}] if isinstance(content, str) else []
        if typ == "assistant":
            s.model = m.get("model") or s.model
            stop = m.get("stop_reason")
            texts = []
            for it in items:
                if it.get("type") == "text":
                    texts.append(it.get("text") or "")
                elif it.get("type") == "tool_use":
                    open_tools[it.get("id") or ""] = (it.get("name") or "tool", _iso_ts(ts))
                    inp = it.get("input") or {}
                    desc = inp.get("command") or inp.get("file_path") or inp.get("description") or json.dumps(inp)[:200]
                    s.messages.append({"role": "tool", "tool": it.get("name"), "status": "called", "text": str(desc)[:400], "time": ts})
            if texts:
                s.messages.append({"role": "assistant", "text": _tail_text(texts), "time": ts})
            s.error = (_tail_text(texts)[:500] or "API error") if d.get("isApiErrorMessage") else None
            last_kind = "assistant"
        else:
            results = [it for it in items if it.get("type") == "tool_result"]
            for it in results:
                open_tools.pop(it.get("tool_use_id") or "", None)
            texts = [it.get("text") or "" for it in items if it.get("type") == "text"]
            text = _tail_text(texts)
            if text and not d.get("isMeta") and not text.startswith(("<local-command", "<command-", "Caveat:")):
                s.messages.append({"role": "user", "text": text, "time": ts})
                s.turn_started = _iso_ts(ts)
            last_kind = "user"
    s.turn_open = last_kind == "user" or (last_kind == "assistant" and stop in (None, "tool_use"))
    now = time.time()
    if open_tools and s.turn_open:
        name, _since = next(iter(open_tools.values()))
        if name == "AskUserQuestion":
            s.pending = "Claude asks you a question"
        elif now - (s.updated or now) > 20:
            s.pending = f"the tool `{name}` is waiting (likely a permission prompt in the terminal)"
    if not s.title:
        s.title = next((mm["text"][:80] for mm in s.messages if mm["role"] == "user"), None)
    return s


# ---------------------------------------------------------------------- discovery


async def _opencode_list_in(exe: str, cwd: Path) -> dict[str, dict[str, Any]] | None:
    """The sessions OpenCode lists in `cwd`; None when it failed (timed out, crashed, unreadable output)."""
    try:
        rc, out, _ = await run_capture([exe, "session", "list", "--format", "json", "-n", "1000"], cwd=cwd, timeout=120)
    except (ProviderError, OSError) as exc:
        log.warning("opencode session list in %s failed: %s", cwd, exc)
        return None
    start = out.find("[")
    if rc != 0 or start < 0:
        return None
    try:
        return {x["id"]: x for x in json.loads(out[start:]) if isinstance(x, dict) and x.get("id")}
    except json.JSONDecodeError:
        return None


# Listing is slow with many sessions (one `opencode session list` per folder): results are kept per folder, every
# caller shares the run in progress, and a result older than OC_FRESH is refreshed in the background while the
# previous one is returned (OC_MAX_AGE: too old to return, wait for the new one).
OC_FRESH = 4.0
OC_MAX_AGE = 600.0
_oc_dir: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_oc_running: dict[str, asyncio.Task] = {}
_oc_cache = _oc_dir  # cleared when sessions are deleted


async def _oc_folder(exe: str, d: str) -> dict[str, dict[str, Any]]:
    hit = _oc_dir.get(d)
    age = time.time() - hit[0] if hit else None
    task = _oc_running.get(d)
    if task is None and (age is None or age >= OC_FRESH):
        async def refresh() -> dict[str, dict[str, Any]]:
            try:
                got = await _opencode_list_in(exe, Path(d))
                if got is not None:
                    _oc_dir[d] = (time.time(), got)
                elif d in _oc_dir:  # failed: keep the last good listing, try again later
                    _oc_dir[d] = (time.time(), _oc_dir[d][1])
                return _oc_dir[d][1] if d in _oc_dir else {}
            finally:
                _oc_running.pop(d, None)

        task = _oc_running[d] = asyncio.ensure_future(refresh())
    if hit and age is not None and age < OC_MAX_AGE:
        return hit[1]  # (being refreshed in the background if stale)
    return await asyncio.shield(task) if task else {}


async def opencode_listing(ctx: AppContext, folders: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """OpenCode lists only the sessions of the project (Git repository) it runs in: list from the home folder
    (the "global" sessions), from every given folder, every open / monitor folder and the watched sessions' folders."""
    exe = _bin(ctx, "opencode")
    if not exe:
        return {}
    dirs = {str(Path.home()), *(folders or []), *_folders(ctx)}
    if getattr(ctx, "workspaces", None) is not None:
        dirs |= {w["root"] for w in ctx.workspaces.list()}
    key = sorted(d for d in dirs if Path(d).is_dir())
    parts = await asyncio.gather(*(_oc_folder(exe, d) for d in key))
    out: dict[str, dict[str, Any]] = {}
    for p in parts:
        out.update(p)
    return out


def codex_listing() -> list[dict[str, Any]]:
    root = codex_home() / "sessions"
    if not root.exists():
        return []
    titles: dict[str, str] = {}
    idx = codex_home() / "session_index.jsonl"
    if idx.exists():
        for line in idx.read_text("utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
                titles[d["id"]] = d.get("thread_name") or ""
            except (ValueError, KeyError):
                continue
    out = []
    for f in sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:1000]:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                d = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        p = d.get("payload") or {}
        sid = p.get("id") or p.get("session_id")
        if sid:
            out.append({"id": sid, "title": titles.get(sid) or None, "directory": p.get("cwd"), "updated": f.stat().st_mtime})
    return out


def claude_listing() -> list[dict[str, Any]]:
    root = claude_home() / "projects"
    if not root.exists():
        return []
    out = []
    for f in sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:1000]:
        cwd = title = first_user = None
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 60:
                        break
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    cwd = cwd or d.get("cwd")
                    if d.get("type") in ("summary", "ai-title"):
                        title = title or d.get("summary") or d.get("title")
                    if d.get("type") == "user" and not first_user and not d.get("isMeta"):
                        c = (d.get("message") or {}).get("content")
                        if isinstance(c, str) and not c.startswith("<"):
                            first_user = c[:80]
        except OSError:
            continue
        out.append({"id": f.stem, "title": title or first_user, "directory": cwd, "updated": f.stat().st_mtime})
    return out


def _under(directory: str | None, folders: list[str]) -> str | None:
    if not directory:
        return None
    d = os.path.normpath(directory)
    best = None
    for f in folders:
        f2 = os.path.normpath(f)
        if (d == f2 or d.startswith(f2.rstrip("/") + "/")) and (best is None or len(f2) > len(best)):
            best = f2
    return best


# ---------------------------------------------------------------------- state


def _watched(ctx: AppContext) -> list[dict[str, Any]]:
    v = kv_get(ctx, "monitor_sessions", [])
    return [x for x in v if isinstance(x, dict) and x.get("provider") in PROVIDERS and x.get("id")] if isinstance(v, list) else []


def _folders(ctx: AppContext) -> list[str]:
    v = kv_get(ctx, "monitor_folders", [])
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


_sending: dict[str, dict[str, Any]] = {}   # "provider:id" -> {state, started, rc, output}
_tasks: dict[str, asyncio.Task] = {}
_cache: dict[str, tuple[float | None, float, Snapshot]] = {}  # key -> (updated marker, read at, snapshot)


async def snapshot(ctx: AppContext, provider: str, sid: str, listing: dict[str, dict[str, Any]] | None = None) -> Snapshot:
    key = f"{provider}:{sid}"
    if provider == "opencode":
        marker = _ms((listing or {}).get(sid, {}).get("updated")) if listing is not None else None
        hit = _cache.get(key)
        if hit and marker is not None and hit[0] == marker and time.time() - hit[1] < 300 and key not in _sending:
            return hit[2]
        snap = await read_opencode(ctx, sid, listing)
        _cache[key] = (marker, time.time(), snap)
        return snap
    f = _codex_file(sid) if provider == "codex" else _claude_file(sid)
    marker = f.stat().st_mtime if f else None
    hit = _cache.get(key)
    if hit and marker is not None and hit[0] == marker and time.time() - hit[1] < 60:
        return hit[2]
    snap = await asyncio.to_thread(read_codex if provider == "codex" else read_claude, sid)
    _cache[key] = (marker, time.time(), snap)
    return snap


def _app_job_state(ctx: AppContext, provider: str, sid: str) -> dict[str, Any] | None:
    """A turn the app itself runs on this session (permission requests are relayed there)."""
    from .models import AgentSession, Job

    with ctx.db.read() as s:
        rows = s.query(AgentSession).filter_by(provider=provider, native_id=sid).all()
        for r in rows:
            j = s.query(Job).filter(Job.session_id == r.id, Job.status.in_(["queued", "running", "waiting_provider_permission"])).first()
            if j is not None:
                return {"job_id": j.id, "status": j.status, "thread_id": j.thread_id}
    return None


async def _item(ctx: AppContext, w: dict[str, Any], listing: dict[str, dict[str, Any]] | None, folders: dict[str, str]) -> dict[str, Any]:
    snap = await snapshot(ctx, w["provider"], w["id"], listing)
    key = f"{w['provider']}:{w['id']}"
    send = _sending.get(key)
    st = snap.status(time.time(), bool(send and send["state"] == "running"))
    app = _app_job_state(ctx, w["provider"], w["id"])
    if app and app["status"] == "waiting_provider_permission":
        st.update(state="needs_input", level="alarm", reason="a permission request of an app turn waits for your decision")
    elif app and st["state"] in ("idle", "error"):
        st.update(state="working", level="busy", reason="an app turn is running")
    folder = _under(snap.directory, list(folders))
    return {"provider": w["provider"], "id": w["id"], "label": w.get("label") or _labels(ctx).get(key) or None, "title": snap.title, "directory": snap.directory,
            "folder": folder, "folder_name": folders.get(folder or ""), "model": snap.model, "found": snap.found,
            "status": st, "app_job": app, "send": send and {k: v for k, v in send.items() if k != "output"}}


def _folder_names(ctx: AppContext) -> dict[str, str]:
    names: dict[str, str] = {}
    if ctx.workspaces is not None:
        for w in ctx.workspaces.list():
            names[os.path.normpath(w["root"])] = w["name"]
    for f in _folders(ctx):
        names.setdefault(os.path.normpath(f), Path(f).name or f)
    return names


# ---------------------------------------------------------------------- routes


@router.get("/monitor/folders")
def get_folders(ctx: AppContext = Depends(require_auth)):
    return {"folders": [{"path": f, "name": Path(f).name or f, "exists": Path(f).is_dir()} for f in _folders(ctx)]}


class FoldersIn(BaseModel):
    folders: list[str] = Field(default_factory=list, max_length=200)


@router.put("/monitor/folders")
def put_folders(body: FoldersIn, ctx: AppContext = Depends(require_auth_mutating)):
    out: list[str] = []
    for f in body.folders:
        p = Path(f).expanduser()
        if not p.is_absolute():
            raise bad_request("give absolute folder paths", "INVALID_PATH")
        if not p.is_dir():
            raise not_found(f"{p} is not a folder", "FOLDER_NOT_FOUND")
        if str(p) not in out:
            out.append(os.path.normpath(str(p)))
    kv_put(ctx, "monitor_folders", out)
    return get_folders(ctx)


async def _discover(ctx: AppContext, wanted: list[str]) -> list[dict[str, Any]]:
    names = _folder_names(ctx)
    oc, cx, cl = await asyncio.gather(opencode_listing(ctx, wanted), asyncio.to_thread(codex_listing), asyncio.to_thread(claude_listing))
    out: list[dict[str, Any]] = []
    for provider, rows in (("opencode", [{"id": k, "title": v.get("title"), "directory": v.get("directory"), "updated": _ms(v.get("updated"))}
                                         for k, v in oc.items()]), ("codex", cx), ("claude", cl)):
        for r in rows:
            f = _under(r.get("directory"), wanted)
            if f is None:
                continue
            out.append({"provider": provider, "id": r["id"], "title": r.get("title"), "directory": r.get("directory"), "folder": f,
                        "folder_name": names.get(f, Path(f).name), "updated_at": _iso(r.get("updated")), "updated": r.get("updated")})
    out.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    return out


# ---------------------------------------------------------------------- automatic monitoring of folders

AUTO_KEY = "monitor_auto"
_auto_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _auto_cfg(ctx: AppContext) -> dict[str, Any]:
    """{folders: [...] | None (= every wiki folder and added folder), days: N, exclude: ["provider:id", ...]}"""
    v = kv_get(ctx, AUTO_KEY, {})
    v = v if isinstance(v, dict) else {}
    folders = v.get("folders")
    return {"folders": [x for x in folders if isinstance(x, str)] if isinstance(folders, list) else None,
            "days": int(v.get("days") or 7), "exclude": [x for x in v.get("exclude") or [] if isinstance(x, str)]}


def _auto_folders(ctx: AppContext, cfg: dict[str, Any]) -> list[str]:
    if cfg["folders"] is not None:
        return [os.path.normpath(f) for f in cfg["folders"]]
    return sorted(_folder_names(ctx))


async def auto_sessions(ctx: AppContext, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The sessions of the checked folders active within `days` (minus the excluded ones)."""
    cfg = cfg or _auto_cfg(ctx)
    folders = _auto_folders(ctx, cfg)
    if not folders or cfg["days"] <= 0:
        return []
    key = "\n".join(folders)
    hit = _auto_cache.get(key)
    if hit and time.time() - hit[0] < 5:  # new sessions show up within one refresh
        found = hit[1]
    else:
        found = await _discover(ctx, folders)
        _auto_cache.clear()
        _auto_cache[key] = (time.time(), found)
    since = time.time() - cfg["days"] * 86400
    excl = set(cfg["exclude"])
    return [x for x in found if (x.get("updated") or 0) >= since and f"{x['provider']}:{x['id']}" not in excl]


class AutoIn(BaseModel):
    folders: list[str] | None = Field(None, max_length=200)
    days: int = Field(7, ge=0, le=365)
    exclude: list[str] = Field(default_factory=list, max_length=2000)


@router.get("/monitor/auto")
def get_auto(ctx: AppContext = Depends(require_auth)):
    return _auto_cfg(ctx)


@router.put("/monitor/auto")
def put_auto(body: AutoIn, ctx: AppContext = Depends(require_auth_mutating)):
    kv_put(ctx, AUTO_KEY, {"folders": [os.path.normpath(f) for f in body.folders] if body.folders is not None else None,
                           "days": body.days, "exclude": list(dict.fromkeys(body.exclude))})
    return _auto_cfg(ctx)


@router.get("/monitor/discover")
async def discover(folders: str = Query("", max_length=40_000), ctx: AppContext = Depends(require_auth)):
    """Native sessions of the given folders (and their sub-folders), every provider, newest first; `auto`: monitored
    because it is recent in a checked folder."""
    wanted = [os.path.normpath(f) for f in folders.split("\n") if f.strip()]
    watched = {(w["provider"], w["id"]) for w in _watched(ctx)}
    cfg = _auto_cfg(ctx)
    excl = set(cfg["exclude"])
    auto_folders = set(_auto_folders(ctx, cfg))
    since = time.time() - cfg["days"] * 86400 if cfg["days"] > 0 else float("inf")
    rows = await _discover(ctx, wanted)
    labels = _labels(ctx)
    out = []
    for x in rows:
        k = f"{x['provider']}:{x['id']}"
        in_auto = (x["folder"] in auto_folders or _under(x.get("directory"), list(auto_folders)) is not None) and (x.get("updated") or 0) >= since
        out.append({**{kk: v for kk, v in x.items() if kk != "updated"}, "watched": (x["provider"], x["id"]) in watched,
                    "auto": in_auto and k not in excl, "excluded": in_auto and k in excl, "label": labels.get(k)})
    return {"sessions": out[:1500], "providers": {p: bool(_bin(ctx, p)) for p in PROVIDERS}}


class WatchIn(BaseModel):
    provider: str = Field(pattern="^(opencode|codex|claude)$")
    id: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    label: str | None = Field(None, max_length=200)


class WatchListIn(BaseModel):
    sessions: list[WatchIn] = Field(default_factory=list, max_length=500)


@router.get("/monitor/watched")
def get_watched(ctx: AppContext = Depends(require_auth)):
    return {"sessions": _watched(ctx)}


@router.put("/monitor/watched")
def put_watched(body: WatchListIn, ctx: AppContext = Depends(require_auth_mutating)):
    seen: set[tuple[str, str]] = set()
    out = []
    for w in body.sessions:
        if (w.provider, w.id) in seen:
            continue
        seen.add((w.provider, w.id))
        out.append({"provider": w.provider, "id": w.id, **({"label": w.label} if w.label else {})})
    kv_put(ctx, "monitor_sessions", out)
    return {"sessions": out}


@router.post("/monitor/watched", status_code=201)
async def add_watched(body: WatchIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Watch a session given by its id (e.g. copied from a terminal). It is checked: is it on this machine?"""
    snap = await snapshot(ctx, body.provider, body.id, await opencode_listing(ctx) if body.provider == "opencode" else None)
    cur = [w for w in _watched(ctx) if not (w["provider"] == body.provider and w["id"] == body.id)]
    cur.insert(0, {"provider": body.provider, "id": body.id, **({"label": body.label} if body.label else {})})
    kv_put(ctx, "monitor_sessions", cur)
    return {"found": snap.found, "title": snap.title, "directory": snap.directory}


@router.get("/monitor/status")
async def status(ctx: AppContext = Depends(require_auth)):
    """The watched sessions with their state; `alarms` counts those that need you."""
    watched = _watched(ctx)
    have = {(w["provider"], w["id"]) for w in watched}
    auto = [{"provider": x["provider"], "id": x["id"], "auto": True} for x in await auto_sessions(ctx) if (x["provider"], x["id"]) not in have]
    watched = watched + auto
    listing = await opencode_listing(ctx) if any(w["provider"] == "opencode" for w in watched) else None
    names = _folder_names(ctx)
    items = await asyncio.gather(*(_item(ctx, w, listing, names) for w in watched))
    for it, w in zip(items, watched, strict=True):
        it["auto"] = bool(w.get("auto"))
    return {"items": items, "alarms": sum(1 for i in items if i["status"]["level"] == "alarm"), "checked_at": _iso(time.time()),
            "providers": {p: bool(_bin(ctx, p)) for p in PROVIDERS}}


@router.get("/monitor/session/{provider}/{sid}")
async def session_detail(provider: str, sid: str, limit: int = Query(200, ge=1, le=MAX_MESSAGES), ctx: AppContext = Depends(require_auth)):
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    listing = await opencode_listing(ctx) if provider == "opencode" else None
    item = await _item(ctx, {"provider": provider, "id": sid, "label": next((w.get("label") for w in _watched(ctx) if w["provider"] == provider and w["id"] == sid), None)},
                       listing, _folder_names(ctx))
    snap = (await snapshot(ctx, provider, sid, listing))
    send = _sending.get(f"{provider}:{sid}")
    return {**item, "messages": snap.messages[-limit:], "total_messages": len(snap.messages),
            "send_output": (send or {}).get("output", "")[-6000:] if send else None}


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


def _send_argv(ctx: AppContext, provider: str, sid: str, text: str, cwd: str) -> tuple[list[str], bytes | None]:
    exe = _bin(ctx, provider)
    if not exe:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", f"{provider} is not installed on the server")
    if provider == "opencode":
        return [exe, "run", "--session", sid, "--dir", cwd, "--format", "json", "--", text], None
    if provider == "codex":
        return [exe, "exec", "resume", "--json", "--skip-git-repo-check", sid, "-"], text.encode("utf-8")
    return [exe, "-p", "--resume", sid, "--output-format", "text", text], None


@router.post("/monitor/session/{provider}/{sid}/send", status_code=202)
async def send(provider: str, sid: str, body: SendIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Continue the session with a message, run non-interactively in the session's folder."""
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    key = f"{provider}:{sid}"
    cur = _sending.get(key)
    if cur and cur["state"] == "running":
        raise conflict("SESSION_BUSY", "a message to this session is still being answered")
    snap = await snapshot(ctx, provider, sid, await opencode_listing(ctx) if provider == "opencode" else None)
    if not snap.found:
        raise not_found(f"no {provider} session {sid} on this machine", "SESSION_NOT_FOUND")
    cwd = snap.directory if snap.directory and Path(snap.directory).is_dir() else str(Path.home())
    argv, stdin = _send_argv(ctx, provider, sid, body.text, cwd)
    st: dict[str, Any] = {"state": "running", "started_at": _iso(time.time()), "rc": None, "output": "", "busy_warning": snap.turn_open}
    _sending[key] = st

    async def run() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=agent_env(None), stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            if stdin and proc.stdin:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            assert proc.stdout
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                st["output"] = (st["output"] + chunk.decode("utf-8", "replace"))[-20000:]
            st["rc"] = await proc.wait()
            st["state"] = "done" if st["rc"] == 0 else "failed"
        except Exception as exc:  # never leave an unretrieved task exception
            st["state"], st["output"] = "failed", st["output"] + f"\n{exc.__class__.__name__}: {exc}"
        finally:
            st["finished_at"] = _iso(time.time())
            _cache.pop(key, None)

    _tasks[key] = asyncio.get_running_loop().create_task(run())
    return {"state": "running", "busy_warning": snap.turn_open}


@router.post("/monitor/session/{provider}/{sid}/terminal", status_code=201)
async def open_terminal(provider: str, sid: str, ctx: AppContext = Depends(require_auth_mutating)):
    """A terminal tab with the provider's own interactive UI attached to the session (answer prompts there)."""
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    exe = _bin(ctx, provider)
    if not exe:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", f"{provider} is not installed on the server")
    snap = await snapshot(ctx, provider, sid, await opencode_listing(ctx) if provider == "opencode" else None)
    cwd = snap.directory if snap.directory and Path(snap.directory).is_dir() else str(Path.home())
    argv = {"opencode": [exe, "--session", sid], "codex": [exe, "resume", sid], "claude": [exe, "--resume", sid]}[provider]
    from .agents import base as agents_base

    if agents_base._env_script:  # the env script (tokens) for the TUI too — sourced, so no secret is on a command line
        argv = ["bash", "-c", '. "$1" >/dev/null 2>&1; shift; exec "$@"', "_", agents_base._env_script, *argv]
    assert ctx.terminals is not None
    label = f"{provider} {(snap.title or sid)[:30]}"
    return ctx.terminals.create(cwd, label, command=argv)


# ---------------------------------------------------------------------- rename / delete

LABELS_KEY = "monitor_labels"


def _labels(ctx: AppContext) -> dict[str, str]:
    v = kv_get(ctx, LABELS_KEY, {})
    return {k: x for k, x in v.items() if isinstance(x, str)} if isinstance(v, dict) else {}


class LabelIn(BaseModel):
    label: str = Field("", max_length=200)


@router.put("/monitor/session/{provider}/{sid}/label")
def rename(provider: str, sid: str, body: LabelIn, ctx: AppContext = Depends(require_auth_mutating)):
    """The name the app shows for a session (the agents have no rename command); empty: back to its own title."""
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    key = f"{provider}:{sid}"
    labels = _labels(ctx)
    if body.label.strip():
        labels[key] = body.label.strip()
    else:
        labels.pop(key, None)
    kv_put(ctx, LABELS_KEY, labels)
    watched = [{**w, "label": body.label.strip()} if f"{w['provider']}:{w['id']}" == key else w for w in _watched(ctx)]
    for w in watched:
        if not w.get("label"):
            w.pop("label", None)
    kv_put(ctx, "monitor_sessions", watched)
    return {"label": labels.get(key)}


async def _delete_native(ctx: AppContext, provider: str, sid: str) -> None:
    if provider == "claude":
        f = _claude_file(sid)
        if f is None:
            raise not_found(f"no Claude Code session {sid}", "SESSION_NOT_FOUND")
        f.unlink()
        side = f.with_suffix("")  # its subagent / tool-result folder, if any
        if side.is_dir():
            import shutil

            shutil.rmtree(side, ignore_errors=True)
        return
    exe = _bin(ctx, provider)
    if not exe:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", f"{provider} is not installed on the server")
    argv = [exe, "session", "delete", sid] if provider == "opencode" else [exe, "delete", sid]
    rc, out, err = await run_capture(argv, cwd=Path.home(), timeout=60)
    if rc != 0:
        raise ApiError(400, "DELETE_FAILED", f"{provider} could not delete {sid}: {(err or out).strip()[-300:]}")


def _forget(ctx: AppContext, keys: set[str]) -> None:
    kv_put(ctx, "monitor_sessions", [w for w in _watched(ctx) if f"{w['provider']}:{w['id']}" not in keys])
    labels = {k: v for k, v in _labels(ctx).items() if k not in keys}
    kv_put(ctx, LABELS_KEY, labels)
    for k in keys:
        _cache.pop(k, None)
    _oc_cache.clear()
    _auto_cache.clear()


class DeleteIn(BaseModel):
    sessions: list[WatchIn] = Field(min_length=1, max_length=500)


@router.post("/monitor/delete")
async def delete_sessions(body: DeleteIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Permanently delete sessions from their provider (OpenCode / Codex: their own delete command; Claude Code: the
    transcript file). Sessions being answered are skipped. Returns what was deleted and what failed."""
    deleted, failed = [], []
    for w in body.sessions:
        key = f"{w.provider}:{w.id}"
        if (_sending.get(key) or {}).get("state") == "running":
            failed.append({"provider": w.provider, "id": w.id, "error": "a message to it is being answered"})
            continue
        try:
            await _delete_native(ctx, w.provider, w.id)
            deleted.append(key)
        except ApiError as exc:
            # a session that does not exist on this machine (any more): nothing to delete, just stop monitoring it
            try:
                gone = not (await snapshot(ctx, w.provider, w.id)).found
            except Exception:  # noqa: BLE001 - reading failed: keep the original error
                gone = False
            if gone:
                deleted.append(key)
            else:
                failed.append({"provider": w.provider, "id": w.id, "error": exc.message})
    _forget(ctx, set(deleted))
    return {"deleted": deleted, "failed": failed}

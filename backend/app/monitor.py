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

Folders on a machine (Machines view) are written `<machine>:<path>`; their sessions are read there over SSH
(monitor_remote) and carry `machine` everywhere (watched entries, keys `provider:id@machine`, the session routes'
`?machine=`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .agents.base import ProviderError, agent_env, resolve_executable, run_capture
from .agents.codex import codex_home
from .db import new_id
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found
from .kv import kv_get, kv_put
from . import monitor_remote as remote
from .monitor_remote import RemoteError, split_folder

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
        self.missing: str | None = None            # why it was not found (a machine that cannot be reached…)
        self.unreachable = False                   # on a machine that could not be reached: unknown, not gone

    def status(self, now: float, sending: bool) -> dict[str, Any]:
        last_text = next((m["text"] for m in reversed(self.messages) if m["role"] == "assistant" and m.get("text")), "")
        age = (now - self.updated) if self.updated else None
        if not self.found:
            state, level, why = "missing", "error", self.missing or f"no {self.provider} session with this id was found on this machine"
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
                                                        stdin=asyncio.subprocess.DEVNULL, env=agent_env(None, Path.home()), cwd=str(Path.home()))
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
    rc, out = await _export(exe, sid)
    return parse_opencode(s, rc, out, (listing or {}).get(sid))


def parse_opencode(s: Snapshot, rc: int, out: str, meta: dict[str, Any] | None) -> Snapshot:
    """`opencode export` output (here or on a machine) into the snapshot."""
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
            elif pt == "file":
                texts.append(f"📎 {p.get('filename') or Path(str(p.get('url') or 'file')).name}")
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
    f = _codex_file(sid)
    if f is None:
        return Snapshot("codex", sid)
    try:
        return parse_codex(sid, f.read_text("utf-8", errors="replace"), f.stat().st_mtime)
    except OSError:
        return Snapshot("codex", sid)


def parse_codex(sid: str, text: str, mtime: float) -> Snapshot:
    """A Codex rollout (its text, read here or on a machine) into the snapshot."""
    s = Snapshot("codex", sid)
    s.found = True
    s.updated = mtime
    approvals: dict[str, str] = {}
    lines = text.splitlines()
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
    f = _claude_file(sid)
    if f is None:
        return Snapshot("claude", sid)
    try:
        return parse_claude(sid, f.read_text("utf-8", errors="replace"), f.stat().st_mtime)
    except OSError:
        return Snapshot("claude", sid)


def parse_claude(sid: str, text: str, mtime: float) -> Snapshot:
    """A Claude Code transcript (its text, read here or on a machine) into the snapshot."""
    s = Snapshot("claude", sid)
    s.found = True
    s.updated = mtime
    open_tools: dict[str, tuple[str, float | None]] = {}
    last_kind = None
    stop = None
    lines = text.splitlines()
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
    dirs = {str(Path.home()), *_local(folders or []), *_local(_folders(ctx))}
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


def mkey(provider: str, sid: str, machine: str | None = None) -> str:
    """A session's key: `provider:id`, `provider:id@machine` for one on a machine."""
    return f"{provider}:{sid}" + (f"@{machine}" if machine else "")


def _wkey(w: dict[str, Any]) -> str:
    return mkey(w["provider"], w["id"], w.get("machine"))


def _watched(ctx: AppContext) -> list[dict[str, Any]]:
    v = kv_get(ctx, "monitor_sessions", [])
    return [x for x in v if isinstance(x, dict) and x.get("provider") in PROVIDERS and x.get("id")] if isinstance(v, list) else []


def _folders(ctx: AppContext) -> list[str]:
    """The added folders: local paths and `<machine>:<path>`."""
    v = kv_get(ctx, "monitor_folders", [])
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _local(folders: list[str]) -> list[str]:
    return [f for f in folders if split_folder(f)[0] is None]


def _by_machine(folders: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in folders:
        m, p = split_folder(f)
        if m is not None and p not in out.setdefault(m, []):
            out[m].append(os.path.normpath(p))
    return out


_remote_errors: dict[str, str] = {}  # machine -> why its sessions could not be (fully) read, as of the last listing


def _errors_for(machines: set[str]) -> list[dict[str, str]]:
    return [{"machine": m, "error": e} for m, e in sorted(_remote_errors.items()) if m in machines]


async def _remote_listing(ctx: AppContext, machine: str, extra: list[str] | None = None) -> dict[str, Any]:
    folders = sorted({*(_by_machine(_folders(ctx)).get(machine) or []), *(extra or [])})
    got = await remote.listing(ctx, machine, folders)
    if got["errors"]:
        _remote_errors[machine] = "; ".join(got["errors"])
    else:
        _remote_errors.pop(machine, None)
    return got


_sending: dict[str, dict[str, Any]] = {}   # "provider:id" -> {state, started, rc, output}
_tasks: dict[str, asyncio.Task] = {}
_cache: dict[str, tuple[float | None, float, Snapshot]] = {}  # key -> (updated marker, read at, snapshot)


async def _remote_read(ctx: AppContext, machine: str, reqs: list[tuple[str, str]]) -> None:
    """Read Codex / Claude Code transcripts on `machine` into the cache (one ssh call; unchanged ones are not sent)."""
    if not reqs:
        return
    asks = []
    for provider, sid in reqs:
        hit = _cache.get(mkey(provider, sid, machine))
        asks.append({"provider": provider, "id": sid, **({"known": hit[0]} if hit and hit[0] is not None else {})})
    now = time.time()
    try:
        got = await remote.helper(ctx, machine, "read", asks)
    except RemoteError as exc:
        for provider, sid in reqs:
            s = Snapshot(provider, sid)
            s.missing, s.unreachable = str(exc), True
            hit = _cache.get(mkey(provider, sid, machine))
            _cache[mkey(provider, sid, machine)] = (hit[0], now, hit[2]) if hit and hit[2].found else (None, now, s)
        return
    for provider, sid in reqs:
        k = mkey(provider, sid, machine)
        r = got.get(f"{provider}:{sid}") or {}
        hit = _cache.get(k)
        if r.get("unchanged") and hit:
            _cache[k] = (hit[0], now, hit[2])
        elif r.get("found") and "text" in r:
            snap = (parse_codex if provider == "codex" else parse_claude)(sid, r["text"], float(r["mtime"]))
            _cache[k] = (float(r["mtime"]), now, snap)
        else:
            s = Snapshot(provider, sid)
            s.missing = f"no {provider} session with this id was found on {machine}"
            _cache[k] = (None, now, s)


async def _remote_snapshot(ctx: AppContext, provider: str, sid: str, machine: str) -> Snapshot:
    key = mkey(provider, sid, machine)
    hit = _cache.get(key)
    if provider != "opencode":
        if not (hit and time.time() - hit[1] < 4 and key not in _sending):
            await _remote_read(ctx, machine, [(provider, sid)])
        return _cache[key][2]
    listing = (await _remote_listing(ctx, machine))["opencode"]
    meta = listing.get(sid)
    marker = _ms((meta or {}).get("updated"))
    fresh = 300 if marker is not None else 20  # not in a listed folder: no change marker, read again now and then
    if hit and hit[0] == marker and time.time() - hit[1] < fresh and key not in _sending:
        return hit[2]
    s = Snapshot("opencode", sid)
    try:
        rc, out = await remote.opencode_export(ctx, machine, sid)
    except RemoteError as exc:
        if hit and hit[2].found:
            return hit[2]
        s.missing, s.unreachable = str(exc), True
        return s
    snap = parse_opencode(s, rc, out, meta)
    if not snap.found:
        snap.missing = f"no OpenCode session with this id was found on {machine}"
    _cache[key] = (marker, time.time(), snap)
    return snap


async def snapshot(ctx: AppContext, provider: str, sid: str, listing: dict[str, dict[str, Any]] | None = None,
                   machine: str | None = None) -> Snapshot:
    if machine:
        return await _remote_snapshot(ctx, provider, sid, machine)
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
    machine = w.get("machine") or None
    snap = await snapshot(ctx, w["provider"], w["id"], listing, machine)
    key = _wkey(w)
    send = _sending.get(key)
    st = snap.status(time.time(), bool(send and send["state"] == "running"))
    app = None if machine else _app_job_state(ctx, w["provider"], w["id"])
    if app and app["status"] == "waiting_provider_permission":
        st.update(state="needs_input", level="alarm", reason="a permission request of an app turn waits for your decision")
    elif app and st["state"] in ("idle", "error"):
        st.update(state="working", level="busy", reason="an app turn is running")
    if machine:
        sub = _under(snap.directory, [split_folder(f)[1] for f in folders if split_folder(f)[0] == machine])
        folder = remote.remote_folder(machine, sub) if sub else None
    else:
        folder = _under(snap.directory, _local(list(folders)))
    return {"provider": w["provider"], "id": w["id"], "machine": machine, "label": w.get("label") or _labels(ctx).get(key) or None,
            "title": snap.title, "directory": snap.directory,
            "folder": folder, "folder_name": folders.get(folder or ""), "model": snap.model, "found": snap.found,
            "status": st, "app_job": app, "send": send and {k: v for k, v in send.items() if k != "output"}}


def _folder_names(ctx: AppContext) -> dict[str, str]:
    names: dict[str, str] = {}
    if ctx.workspaces is not None:
        for w in ctx.workspaces.list():
            names[os.path.normpath(w["root"])] = w["name"]
    for f in _folders(ctx):
        m, p = split_folder(f)
        if m:
            names.setdefault(remote.remote_folder(m, os.path.normpath(p)), f"{Path(p).name or p} @{m}")
        else:
            names.setdefault(os.path.normpath(f), Path(f).name or f)
    return names


def _norm(f: str) -> str:
    m, p = split_folder(f)
    return remote.remote_folder(m, os.path.normpath(p)) if m else os.path.normpath(f)


# ---------------------------------------------------------------------- routes


@router.get("/monitor/folders")
def get_folders(ctx: AppContext = Depends(require_auth)):
    names = _folder_names(ctx)
    out = []
    for f in _folders(ctx):
        m, p = split_folder(f)
        out.append({"path": f, "name": names.get(_norm(f)) or Path(p).name or f, "machine": m, "exists": True if m else Path(f).is_dir()})
    return {"folders": out}


class FoldersIn(BaseModel):
    folders: list[str] = Field(default_factory=list, max_length=200)


@router.put("/monitor/folders")
async def put_folders(body: FoldersIn, ctx: AppContext = Depends(require_auth_mutating)):
    """The added folders: local ones, and `<machine>:<path>` on a machine (checked there when newly added)."""
    out: list[str] = []
    before = set(_folders(ctx))
    for f in body.folders:
        m, rp = split_folder(f)
        if m is not None:
            if f not in before:
                try:
                    t = remote.target(ctx, m)
                    rp = await t.expand(rp)
                    ok = await t.is_dir(rp)
                except RemoteError as exc:
                    raise bad_request(str(exc), "DEV_HOST_UNKNOWN")
                except Exception as exc:  # noqa: BLE001 - the machine cannot be reached
                    raise ApiError(502, getattr(exc, "code", "DEV_HOST_FAILED"), getattr(exc, "message", str(exc)))
                if not ok:
                    raise not_found(f"{rp} is not a folder on {m}", "FOLDER_NOT_FOUND")
            nf = remote.remote_folder(m, os.path.normpath(rp))
            if nf not in out:
                out.append(nf)
            continue
        p = Path(f).expanduser()
        if not p.is_absolute():
            raise bad_request("give absolute folder paths", "INVALID_PATH")
        if not p.is_dir():
            raise not_found(f"{p} is not a folder", "FOLDER_NOT_FOUND")
        if str(p) not in out:
            out.append(os.path.normpath(str(p)))
    kv_put(ctx, "monitor_folders", out)
    return get_folders(ctx)


async def _discover_remote(ctx: AppContext, machine: str, paths: list[str], names: dict[str, str]) -> list[dict[str, Any]]:
    got = await _remote_listing(ctx, machine, paths)
    out = []
    oc = [{"id": k, "title": v.get("title"), "directory": v.get("directory"), "updated": _ms(v.get("updated"))} for k, v in got["opencode"].items()]
    for provider, rows in (("opencode", oc), ("codex", got["codex"]), ("claude", got["claude"])):
        for r in rows:
            sub = _under(r.get("directory"), paths)
            if sub is None:
                continue
            f = remote.remote_folder(machine, sub)
            out.append({"provider": provider, "id": r["id"], "machine": machine, "title": r.get("title"), "directory": r.get("directory"),
                        "folder": f, "folder_name": names.get(f, f"{Path(sub).name} @{machine}"), "updated_at": _iso(r.get("updated")),
                        "updated": r.get("updated")})
    return out


async def _discover(ctx: AppContext, wanted: list[str]) -> list[dict[str, Any]]:
    names = _folder_names(ctx)
    rem = _by_machine(wanted)
    wanted = _local(wanted)
    (oc, cx, cl), *remote_rows = await asyncio.gather(
        asyncio.gather(opencode_listing(ctx, wanted), asyncio.to_thread(codex_listing), asyncio.to_thread(claude_listing)),
        *(_discover_remote(ctx, m, ps, names) for m, ps in rem.items()))
    out: list[dict[str, Any]] = [x for rows in remote_rows for x in rows]
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
        return [_norm(f) for f in cfg["folders"]]
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
    return [x for x in found if (x.get("updated") or 0) >= since and _wkey(x) not in excl]


class AutoIn(BaseModel):
    folders: list[str] | None = Field(None, max_length=200)
    days: int = Field(7, ge=0, le=365)
    exclude: list[str] = Field(default_factory=list, max_length=2000)


@router.get("/monitor/auto")
def get_auto(ctx: AppContext = Depends(require_auth)):
    return _auto_cfg(ctx)


@router.put("/monitor/auto")
def put_auto(body: AutoIn, ctx: AppContext = Depends(require_auth_mutating)):
    kv_put(ctx, AUTO_KEY, {"folders": [_norm(f) for f in body.folders] if body.folders is not None else None,
                           "days": body.days, "exclude": list(dict.fromkeys(body.exclude))})
    return _auto_cfg(ctx)


@router.get("/monitor/discover")
async def discover(folders: str = Query("", max_length=40_000), ctx: AppContext = Depends(require_auth)):
    """Native sessions of the given folders (and their sub-folders), every provider, newest first; `auto`: monitored
    because it is recent in a checked folder."""
    wanted = [_norm(f) for f in folders.split("\n") if f.strip()]
    watched = {_wkey(w) for w in _watched(ctx)}
    cfg = _auto_cfg(ctx)
    excl = set(cfg["exclude"])
    auto_folders = set(_auto_folders(ctx, cfg))
    since = time.time() - cfg["days"] * 86400 if cfg["days"] > 0 else float("inf")
    rows = await _discover(ctx, wanted)
    labels = _labels(ctx)
    out = []
    for x in rows:
        k = _wkey(x)
        if x.get("machine"):
            sub = _under(x.get("directory"), [split_folder(f)[1] for f in auto_folders if split_folder(f)[0] == x["machine"]])
            in_folder = x["folder"] in auto_folders or sub is not None
        else:
            in_folder = x["folder"] in auto_folders or _under(x.get("directory"), _local(list(auto_folders))) is not None
        in_auto = in_folder and (x.get("updated") or 0) >= since
        out.append({**{kk: v for kk, v in x.items() if kk != "updated"}, "watched": k in watched,
                    "auto": in_auto and k not in excl, "excluded": in_auto and k in excl, "label": labels.get(k)})
    return {"sessions": out[:1500], "providers": {p: bool(_bin(ctx, p)) for p in PROVIDERS},
            "remote_errors": _errors_for(set(_by_machine(wanted)))}


MACHINE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"


class WatchIn(BaseModel):
    provider: str = Field(pattern="^(opencode|codex|claude)$")
    id: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    label: str | None = Field(None, max_length=200)
    machine: str | None = Field(None, pattern=MACHINE_PATTERN)  # a session on a machine (Machines view)


def _entry(w: WatchIn) -> dict[str, Any]:
    return {"provider": w.provider, "id": w.id, **({"machine": w.machine} if w.machine else {}), **({"label": w.label} if w.label else {})}


class WatchListIn(BaseModel):
    sessions: list[WatchIn] = Field(default_factory=list, max_length=500)


@router.get("/monitor/watched")
def get_watched(ctx: AppContext = Depends(require_auth)):
    return {"sessions": _watched(ctx)}


@router.put("/monitor/watched")
def put_watched(body: WatchListIn, ctx: AppContext = Depends(require_auth_mutating)):
    seen: set[str] = set()
    out = []
    for w in body.sessions:
        k = mkey(w.provider, w.id, w.machine)
        if k in seen:
            continue
        seen.add(k)
        out.append(_entry(w))
    kv_put(ctx, "monitor_sessions", out)
    return {"sessions": out}


@router.post("/monitor/watched", status_code=201)
async def add_watched(body: WatchIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Watch a session given by its id (e.g. copied from a terminal). It is checked: is it on this machine (or on
    `machine`)?"""
    local_oc = body.provider == "opencode" and not body.machine
    snap = await snapshot(ctx, body.provider, body.id, await opencode_listing(ctx) if local_oc else None, body.machine)
    k = mkey(body.provider, body.id, body.machine)
    cur = [w for w in _watched(ctx) if _wkey(w) != k]
    cur.insert(0, _entry(body))
    kv_put(ctx, "monitor_sessions", cur)
    return {"found": snap.found, "title": snap.title, "directory": snap.directory}


@router.get("/monitor/status")
async def status(ctx: AppContext = Depends(require_auth)):
    """The watched sessions with their state; `alarms` counts those that need you."""
    watched = _watched(ctx)
    have = {_wkey(w) for w in watched}
    auto = [{"provider": x["provider"], "id": x["id"], "auto": True, **({"machine": x["machine"]} if x.get("machine") else {})}
            for x in await auto_sessions(ctx) if _wkey(x) not in have]
    watched = watched + auto
    listing = await opencode_listing(ctx) if any(w["provider"] == "opencode" and not w.get("machine") for w in watched) else None
    names = _folder_names(ctx)
    # the transcripts on each machine: one ssh call per machine for all of its Codex / Claude Code sessions
    per: dict[str, list[tuple[str, str]]] = {}
    for w in watched:
        if w.get("machine") and w["provider"] != "opencode":
            per.setdefault(w["machine"], []).append((w["provider"], w["id"]))
    await asyncio.gather(*(_remote_read(ctx, m, reqs) for m, reqs in per.items()))
    items = await asyncio.gather(*(_item(ctx, w, listing, names) for w in watched))
    for it, w in zip(items, watched, strict=True):
        it["auto"] = bool(w.get("auto"))
    machines = {w["machine"] for w in watched if w.get("machine")} | set(_by_machine(_folders(ctx)))
    return {"items": items, "alarms": sum(1 for i in items if i["status"]["level"] == "alarm"), "checked_at": _iso(time.time()),
            "providers": {p: bool(_bin(ctx, p)) for p in PROVIDERS}, "remote_errors": _errors_for(machines)}


MachineQ = Query(None, pattern=MACHINE_PATTERN, description="the machine the session is on (none: this server)")


def _label_of(ctx: AppContext, key: str) -> str | None:
    return next((w.get("label") for w in _watched(ctx) if _wkey(w) == key), None)


@router.get("/monitor/session/{provider}/{sid}")
async def session_detail(provider: str, sid: str, limit: int = Query(200, ge=1, le=MAX_MESSAGES), machine: str | None = MachineQ,
                         ctx: AppContext = Depends(require_auth)):
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    listing = await opencode_listing(ctx) if provider == "opencode" and not machine else None
    key = mkey(provider, sid, machine)
    item = await _item(ctx, {"provider": provider, "id": sid, "machine": machine, "label": _label_of(ctx, key)}, listing, _folder_names(ctx))
    snap = await snapshot(ctx, provider, sid, listing, machine)
    send = _sending.get(key)
    return {**item, "messages": snap.messages[-limit:], "total_messages": len(snap.messages),
            "send_output": (send or {}).get("output", "")[-6000:] if send else None}


MAX_IMAGES = 6
_CONTROL = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$"  # a model / variant / agent name (it reaches the provider's argv)


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    # control commands of the monitor's composer (/model, /effort, /agent): for this message (none: the session's own)
    model: str | None = Field(None, max_length=200, pattern=_CONTROL)
    variant: str | None = Field(None, max_length=40, pattern=_CONTROL)
    agent: str | None = Field(None, max_length=100, pattern=_CONTROL)
    # pictures pasted into the composer (data:image/…;base64 URLs), attached to the message
    images: list[Annotated[str, Field(max_length=17_000_000)]] = Field(default_factory=list, max_length=MAX_IMAGES)


def _send_argv(ctx: AppContext, provider: str, sid: str, text: str, cwd: str, version: str | None = None,
               exe: str | None = None, *, model: str | None = None, variant: str | None = None, agent: str | None = None,
               current_model: str | None = None, images: list[str] | None = None) -> tuple[list[str], bytes | None]:
    images = images or []
    exe = exe or _bin(ctx, provider)
    if not exe:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", f"{provider} is not installed on the server")
    if provider == "opencode":
        from .agents.opencode import is_v2

        v2 = is_v2(version)
        here = [] if v2 else ["--dir", cwd]  # v2 has no --dir: the process runs in cwd
        opts: list[str] = []
        if v2:  # "provider/model#variant": an effort alone goes with the session's current model
            m = (model or (current_model if variant else None) or "").split("#", 1)[0]
            if variant and not m:
                raise bad_request("the session's model is unknown: choose one with /model first", "MODEL_REQUIRED")
            if m:
                opts += ["--model", f"{m}#{variant}" if variant else m]
        else:
            opts += ["--model", model] if model else []
            opts += ["--variant", variant] if variant else []
        opts += ["--agent", agent] if agent else []
        # --file swallows the following arguments: right before "--" (which ends them)
        opts += [f"--file={p}" for p in images]
        return [exe, "run", "--session", sid, *here, *opts, "--format", "json", "--", text], None
    if agent:
        raise bad_request(f"{provider} has no agents to choose (/agent is for OpenCode)", "UNSUPPORTED_CONTROL")
    if provider == "codex":
        opts = ["-m", model] if model else []
        opts += ["-c", f'model_reasoning_effort="{variant}"'] if variant else []
        opts += [f"--image={p}" for p in images]  # "--image=PATH": the option takes several values
        return [exe, "exec", "resume", "--json", "--skip-git-repo-check", *opts, sid, "-"], text.encode("utf-8")
    if variant:
        raise bad_request("Claude Code has no reasoning level to choose here (/effort is for OpenCode and Codex)", "UNSUPPORTED_CONTROL")
    opts = ["--model", model] if model else []
    if images:  # Claude Code reads a picture whose path the message gives
        text += "\n\n" + "\n".join(f"[Attached image: {p}]" for p in images)
    return [exe, "-p", "--resume", sid, *opts, "--output-format", "text", text], None


def _remote_tool(ctx: AppContext, machine: str, provider: str) -> dict[str, Any]:
    """The provider's entry in the machine's last probe ({} when it was never probed)."""
    try:
        caps = json.loads(remote.registry(ctx).row(machine).capabilities_json or "null") or {}
    except Exception:  # noqa: BLE001 - a config host without a probe, a removed machine
        caps = {}
    return (caps.get("tools") or {}).get(provider) or {}


async def _remote_argv(ctx: AppContext, machine: str, provider: str, argv: list[str], cwd: str, *, tty: bool = False) -> list[str]:
    """`argv` run on the machine in `cwd`: the machine's tools first on PATH, the variables it gets from this server
    (a 0600 file, sourced then deleted), the opencode settings its setup copied there."""
    from .development.targets import TargetError
    from .machines import envfwd

    t = remote.target(ctx, machine)
    try:
        env_file = await envfwd.push(t)
        if provider == "opencode":
            cfg = await t.expand(f"{t.host.work_dir.rstrip('/')}/opencode/opencode.json") if t.host.work_dir.startswith(("~", "/")) else ""
            if cfg:
                argv = ["sh", "-c", 'if [ -f "$1" ]; then OPENCODE_CONFIG=$1; export OPENCODE_CONFIG; fi; shift; exec "$@"', "_", cfg, *argv]
    except TargetError as exc:
        raise ApiError(502 if exc.code == "DEV_HOST_UNREACHABLE" else 400, exc.code, exc.message)
    out, _, _ = t.spawn_argv(argv, cwd, {"PWD": cwd}, ssh_opts=["-tt"] if tty else None, env_file=env_file)
    return out


async def _remote_cwd(ctx: AppContext, machine: str, directory: str | None) -> str:
    t = remote.target(ctx, machine)
    try:
        if directory and await t.is_dir(directory):
            return directory
        return await t.expand("~")
    except Exception as exc:  # noqa: BLE001 - unreachable
        raise ApiError(502, getattr(exc, "code", "DEV_HOST_UNREACHABLE"), getattr(exc, "message", str(exc)))


def _local_images(ctx: AppContext, pictures: list[tuple[bytes, str]]) -> list[str]:
    """The pasted pictures as files of this server (the provider is given their paths)."""
    if not pictures:
        return []
    d = ctx.settings.snapshots_dir / "attachments" / f"monitor-{new_id()}"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for n, (raw, ext) in enumerate(pictures, 1):
        f = d / f"image-{n}.{ext}"
        f.write_bytes(raw)
        out.append(str(f))
    return out


async def _remote_images(ctx: AppContext, machine: str, pictures: list[tuple[bytes, str]]) -> list[str]:
    """The pasted pictures copied to the machine (its provider reads them there)."""
    if not pictures:
        return []
    from .development.targets import TargetError

    t = remote.target(ctx, machine)
    try:
        d = await t.expand(f"~/.cache/wiki-agent-workspace/attachments/monitor-{new_id()}")
        out = []
        for n, (raw, ext) in enumerate(pictures, 1):
            f = f"{d}/image-{n}.{ext}"
            script = f"mkdir -p {shlex.quote(d)} && umask 077 && cat > {shlex.quote(f)}"
            rc, _, err = await t.run(["sh", "-c", script], timeout=120, stdin=raw)
            if rc != 0:
                raise ApiError(502, "ATTACHMENT_COPY_FAILED", f"could not copy the picture to {machine}: {err.strip()[:300]}")
            out.append(f)
        return out
    except TargetError as exc:
        raise ApiError(502 if exc.code == "DEV_HOST_UNREACHABLE" else 400, exc.code, exc.message)


@router.post("/monitor/session/{provider}/{sid}/send", status_code=202)
async def send(provider: str, sid: str, body: SendIn, machine: str | None = MachineQ, ctx: AppContext = Depends(require_auth_mutating)):
    """Continue the session with a message, run non-interactively in the session's folder (on its machine)."""
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    key = mkey(provider, sid, machine)
    cur = _sending.get(key)
    if cur and cur["state"] == "running":
        raise conflict("SESSION_BUSY", "a message to this session is still being answered")
    snap = await snapshot(ctx, provider, sid, await opencode_listing(ctx) if provider == "opencode" and not machine else None, machine)
    if not snap.found:
        raise not_found(snap.missing or f"no {provider} session {sid} on this machine", "SESSION_NOT_FOUND")
    controls: dict[str, Any] = {"model": body.model, "variant": body.variant, "agent": body.agent, "current_model": snap.model}
    from .annotations import decode_image

    pictures = [decode_image(u) for u in body.images]  # checked before anything is written
    if machine:
        tool = _remote_tool(ctx, machine, provider)
        if tool and not tool.get("ok"):
            raise ApiError(503, "PROVIDER_UNAVAILABLE", f"{provider} is not installed on {machine} (as of its last probe)")
        cwd = await _remote_cwd(ctx, machine, snap.directory)
        controls["images"] = await _remote_images(ctx, machine, pictures)
        argv, stdin = _send_argv(ctx, provider, sid, body.text, cwd, str(tool.get("version") or "") or None, remote.bin_of(ctx, machine, provider),
                                 **controls)
        argv = await _remote_argv(ctx, machine, provider, argv, cwd)
        lcwd, lenv = None, None
    else:
        cwd = snap.directory if snap.directory and Path(snap.directory).is_dir() else str(Path.home())
        version = (await ctx.providers.capabilities("opencode")).version if provider == "opencode" else None
        controls["images"] = _local_images(ctx, pictures)
        argv, stdin = _send_argv(ctx, provider, sid, body.text, cwd, version, **controls)
        lcwd, lenv = cwd, agent_env(None, cwd)
    st: dict[str, Any] = {"state": "running", "started_at": _iso(time.time()), "rc": None, "output": "", "busy_warning": snap.turn_open}
    _sending[key] = st

    async def run() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=lcwd, env=lenv, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
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
            if machine:
                remote.forget()

    _tasks[key] = asyncio.get_running_loop().create_task(run())
    return {"state": "running", "busy_warning": snap.turn_open}


@router.post("/monitor/session/{provider}/{sid}/terminal", status_code=201)
async def open_terminal(provider: str, sid: str, machine: str | None = MachineQ, ctx: AppContext = Depends(require_auth_mutating)):
    """A terminal tab with the provider's own interactive UI attached to the session (answer prompts there); for a
    session on a machine, the UI runs there (in the machine's own tmux when it has one)."""
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    exe = remote.bin_of(ctx, machine, provider) if machine else _bin(ctx, provider)
    if not exe:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", f"{provider} is not installed on the server")
    snap = await snapshot(ctx, provider, sid, await opencode_listing(ctx) if provider == "opencode" and not machine else None, machine)
    argv = {"opencode": [exe, "--session", sid], "codex": [exe, "resume", sid], "claude": [exe, "--resume", sid]}[provider]
    label = f"{provider} {(snap.title or sid)[:30]}"
    assert ctx.terminals is not None
    if machine:
        from .machines.routes import open_terminal as machine_terminal

        cwd = await _remote_cwd(ctx, machine, snap.directory)
        return await machine_terminal(ctx, machine, cwd, f"{label} @{machine}", argv=argv)
    cwd = snap.directory if snap.directory and Path(snap.directory).is_dir() else str(Path.home())
    from .agents import base as agents_base

    if agents_base._env_script:  # the env script (tokens) for the TUI too — sourced, so no secret is on a command line
        argv = ["bash", "-c", '. "$1" >/dev/null 2>&1; shift; exec "$@"', "_", agents_base._env_script, *argv]
    return ctx.terminals.create(cwd, label, command=argv)


# ---------------------------------------------------------------------- rename / delete

LABELS_KEY = "monitor_labels"


def _labels(ctx: AppContext) -> dict[str, str]:
    v = kv_get(ctx, LABELS_KEY, {})
    return {k: x for k, x in v.items() if isinstance(x, str)} if isinstance(v, dict) else {}


class LabelIn(BaseModel):
    label: str = Field("", max_length=200)


async def _retitle_opencode(ctx: AppContext, sid: str, title: str, machine: str | None) -> bool:
    """Rename the session in OpenCode itself (v2's server: `opencode api PATCH /api/session/{id}`), so its TUI and
    every client show the new title. False when that is not possible (v1, no server, unknown session)."""
    argv_tail = ["api", "PATCH", f"/api/session/{sid}", "--data", json.dumps({"title": title})]
    try:
        if machine:
            rc, _, _ = await remote._run(ctx, machine, [remote.bin_of(ctx, machine, "opencode"), *argv_tail], timeout=30)
        else:
            exe = _bin(ctx, "opencode")
            if not exe:
                return False
            rc, _, _ = await run_capture([exe, *argv_tail], cwd=Path.home(), timeout=30)
    except (RemoteError, ProviderError, OSError) as exc:
        log.warning("renaming opencode session %s failed: %s", sid, exc)
        return False
    if rc == 0:
        _oc_cache.clear()
        remote.forget()
    return rc == 0


@router.put("/monitor/session/{provider}/{sid}/label")
async def rename(provider: str, sid: str, body: LabelIn, machine: str | None = MachineQ, ctx: AppContext = Depends(require_auth_mutating)):
    """Rename a session. OpenCode (v2): its own title is changed; otherwise (Codex, Claude Code, OpenCode v1) the
    name the app shows for it. Empty: back to its own title."""
    if provider not in PROVIDERS:
        raise not_found("unknown provider", "PROVIDER_UNKNOWN")
    name = body.label.strip()
    native = bool(name) and provider == "opencode" and await _retitle_opencode(ctx, sid, name, machine)
    key = mkey(provider, sid, machine)
    labels = _labels(ctx)
    if name and not native:
        labels[key] = name
    else:
        labels.pop(key, None)
    kv_put(ctx, LABELS_KEY, labels)
    watched = [{**w, "label": labels.get(key, "")} if _wkey(w) == key else w for w in _watched(ctx)]
    for w in watched:
        if not w.get("label"):
            w.pop("label", None)
    kv_put(ctx, "monitor_sessions", watched)
    _cache.pop(key, None)
    return {"label": labels.get(key), "title": name if native else None, "native": native}


async def _delete_native(ctx: AppContext, provider: str, sid: str, machine: str | None = None) -> None:
    if machine:
        try:
            if provider == "claude":
                if not (await remote.helper(ctx, machine, "delete", {"id": sid}, timeout=60)).get("deleted"):
                    raise not_found(f"no Claude Code session {sid} on {machine}", "SESSION_NOT_FOUND")
                return
            exe = remote.bin_of(ctx, machine, provider)
            argv = [exe, "session", "delete", sid] if provider == "opencode" else [exe, "delete", sid]
            rc, out, err = await remote._run(ctx, machine, argv, timeout=60)
        except RemoteError as exc:
            raise ApiError(502, "DEV_HOST_UNREACHABLE", str(exc))
        if rc != 0:
            raise ApiError(400, "DELETE_FAILED", f"{provider} on {machine} could not delete {sid}: {(err or out).strip()[-300:]}")
        return
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
    kv_put(ctx, "monitor_sessions", [w for w in _watched(ctx) if _wkey(w) not in keys])
    labels = {k: v for k, v in _labels(ctx).items() if k not in keys}
    kv_put(ctx, LABELS_KEY, labels)
    for k in keys:
        _cache.pop(k, None)
    _oc_cache.clear()
    _auto_cache.clear()
    remote.forget()


class DeleteIn(BaseModel):
    sessions: list[WatchIn] = Field(min_length=1, max_length=500)


@router.post("/monitor/delete")
async def delete_sessions(body: DeleteIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Permanently delete sessions from their provider (OpenCode / Codex: their own delete command; Claude Code: the
    transcript file). Sessions being answered are skipped. Returns what was deleted and what failed."""
    deleted, failed = [], []
    for w in body.sessions:
        key = mkey(w.provider, w.id, w.machine)
        if (_sending.get(key) or {}).get("state") == "running":
            failed.append({"provider": w.provider, "id": w.id, "error": "a message to it is being answered"})
            continue
        try:
            await _delete_native(ctx, w.provider, w.id, w.machine)
            deleted.append(key)
        except ApiError as exc:
            # a session that does not exist on this machine (any more): nothing to delete, just stop monitoring it
            try:
                _cache.pop(key, None)
                snap = await snapshot(ctx, w.provider, w.id, None, w.machine)
                gone = not snap.found and not snap.unreachable
            except Exception:  # noqa: BLE001 - reading failed: keep the original error
                gone = False
            if gone:
                deleted.append(key)
            else:
                failed.append({"provider": w.provider, "id": w.id, "error": exc.message})
    _forget(ctx, set(deleted))
    return {"deleted": deleted, "failed": failed}

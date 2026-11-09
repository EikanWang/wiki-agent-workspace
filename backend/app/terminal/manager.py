"""Terminal tabs: tmux sessions + PTY attach + WebSocket relay (DESIGN §11, §13)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.websockets import WebSocketState

from ..db import Database, iso
from ..deps import AppContext, require_auth, require_auth_mutating
from ..errors import ApiError, bad_request, not_found, TERMINAL_GONE
from ..events import EventBus
from ..models import Terminal
from .pty import PtyProcess
from .tmux import SESSION_PREFIX, Tmux, TmuxError

log = logging.getLogger("waw.terminal")
router = APIRouter()

MIN_COLS, MAX_COLS, MIN_ROWS, MAX_ROWS = 2, 1000, 1, 500


def default_label(taken) -> str:
    """Lowest "Terminal N" not used by a visible tab (closing Terminal 2 frees the name for the next one)."""
    used = set(taken)
    n = 1
    while f"Terminal {n}" in used:
        n += 1
    return f"Terminal {n}"


def terminal_dict(t: Terminal) -> dict[str, Any]:
    return {"id": t.id, "label": t.label, "cwd": t.cwd, "tab_order": t.tab_order, "state": t.state,
            "tmux_session": t.tmux_session, "created_at": iso(t.created_at), "updated_at": iso(t.updated_at)}


class TerminalManager:
    def __init__(self, db: Database, tmux: Tmux, bus: EventBus, default_cwd: Path):
        self.db = db
        self.tmux = tmux
        self.bus = bus
        self.default_cwd = default_cwd
        self.clients: dict[str, int] = {}
        # (current named workspace id, whether it is Default): tabs are listed and created per named workspace
        self.scope = lambda: ("", True)

    def _in_scope(self, q):
        """Only the current named workspace's tabs; NULL = Default (tabs from before named workspaces)."""
        from sqlalchemy import or_

        nid, is_default = self.scope()
        if not nid:
            return q
        if is_default:
            return q.filter(or_(Terminal.named_workspace_id == nid, Terminal.named_workspace_id.is_(None)))
        return q.filter(Terminal.named_workspace_id == nid)

    # ------------------------------------------------------------------ discovery
    def reconcile(self) -> dict[str, Any]:
        """On startup: match live app-owned tmux sessions with saved tab metadata."""
        result = {"live": 0, "exited": 0, "discovered": 0, "tmux_available": self.tmux.available, "error": None}
        if not self.tmux.available:
            with self.db.write() as s:
                for t in s.query(Terminal).filter(Terminal.state == "live").all():
                    t.state = "unavailable"
            result["error"] = "tmux is not installed"
            return result
        try:
            live = set(self.tmux.list_sessions())
        except TmuxError as exc:
            result["error"] = str(exc)
            return result
        with self.db.write() as s:
            rows = s.query(Terminal).all()
            known = {t.tmux_session for t in rows}
            for t in rows:
                if t.tmux_session in live:
                    if t.state != "live":
                        t.state = "live"
                    result["live"] += 1
                elif t.state == "live":
                    t.state = "exited"
                    result["exited"] += 1
            order = max([t.tab_order for t in rows], default=-1) + 1
            for name in sorted(live - known):
                s.add(Terminal(id=str(uuid.uuid4()), tmux_session=name, label=f"Terminal {name[len(SESSION_PREFIX):][:6]}",
                               cwd=str(self.default_cwd), tab_order=order, state="live"))
                order += 1
                result["discovered"] += 1
        return result

    def refresh_states(self) -> None:
        if not self.tmux.available:
            return
        try:
            live = set(self.tmux.list_sessions())
        except TmuxError:
            return
        with self.db.write() as s:
            for t in s.query(Terminal).filter(Terminal.state == "live").all():
                if t.tmux_session not in live:
                    t.state = "exited"
                    self.bus.publish("global", {"type": "terminal_state", "terminal_id": t.id, "state": "exited"})

    # ------------------------------------------------------------------ CRUD
    def list(self) -> list[dict[str, Any]]:
        self.refresh_states()
        with self.db.read() as s:
            rows = self._in_scope(s.query(Terminal).filter(Terminal.state != "closed")).order_by(Terminal.tab_order, Terminal.created_at).all()
            return [terminal_dict(t) for t in rows]

    def create(self, cwd: str | None, label: str | None) -> dict[str, Any]:
        if not self.tmux.available:
            raise ApiError(503, "TMUX_UNAVAILABLE", "tmux is not installed on the server; install tmux 3.2+ (e.g. `sudo apt install tmux`) and restart")
        start = Path(cwd).expanduser() if cwd else self.default_cwd
        if not start.is_absolute():
            start = (self.default_cwd / start)
        if not start.is_dir():
            raise bad_request(f"directory does not exist: {start}", "CWD_NOT_FOUND")
        tid = str(uuid.uuid4())
        name = f"{SESSION_PREFIX}{tid[:8]}"
        try:
            self.tmux.new_session(name, start)
        except TmuxError as exc:
            raise ApiError(503, "TMUX_ERROR", str(exc))
        with self.db.write() as s:
            # closed rows are kept (hidden) for history: number and place new tabs among the visible ones only
            shown = self._in_scope(s.query(Terminal).filter(Terminal.state != "closed")).all()
            order = max([t.tab_order for t in shown], default=-1) + 1
            nid, _ = self.scope()
            t = Terminal(id=tid, tmux_session=name, label=label or default_label(t.label for t in shown), cwd=str(start),
                         tab_order=order, state="live", named_workspace_id=nid or None)
            s.add(t)
            s.flush()
            d = terminal_dict(t)
        self.bus.publish("global", {"type": "terminal_state", "terminal_id": tid, "state": "live"})
        return d

    def get(self, tid: str) -> Terminal:
        with self.db.read() as s:
            t = s.get(Terminal, tid)
        if t is None or t.state == "closed":
            raise not_found("terminal not found", "TERMINAL_NOT_FOUND")
        return t

    def update(self, tid: str, label: str | None, tab_order: int | None) -> dict[str, Any]:
        with self.db.write() as s:
            t = s.get(Terminal, tid)
            if t is None:
                raise not_found("terminal not found", "TERMINAL_NOT_FOUND")
            if label is not None:
                t.label = label[:80]
            if tab_order is not None:
                t.tab_order = tab_order
            s.flush()
            return terminal_dict(t)

    def close(self, tid: str) -> dict[str, Any]:
        t = self.get(tid)
        if self.tmux.available:
            try:
                self.tmux.kill_session(t.tmux_session)
            except TmuxError as exc:
                raise ApiError(503, "TMUX_ERROR", str(exc))
        with self.db.write() as s:
            row = s.get(Terminal, tid)
            if row:
                row.state = "closed"
        self.bus.publish("global", {"type": "terminal_state", "terminal_id": tid, "state": "closed"})
        return {"id": tid, "state": "closed"}

    def history(self, tid: str, lines: int) -> dict[str, Any]:
        t = self.get(tid)
        if not self.tmux.available or not self.tmux.has_session(t.tmux_session):
            raise ApiError(410, TERMINAL_GONE, "the tmux session no longer exists")
        try:
            text = self.tmux.capture_history(t.tmux_session, lines)
        except TmuxError as exc:
            raise ApiError(503, "TMUX_ERROR", str(exc))
        return {"id": tid, "lines": lines, "text": text,
                "note": "Snapshot of tmux pane history (up to the configured history-limit). Taken at request time; concurrent output may be missing."}

    def info(self) -> dict[str, Any]:
        return {
            "tmux_available": self.tmux.available,
            "tmux_version": self.tmux.version(),
            "inner_term": self.tmux.inner_term,
            "locale_warning": getattr(self.tmux, "locale_warning", None),
            "history_limit": self.tmux.settings.tmux_history_limit,
            "install_hint": None if self.tmux.available else "Install tmux 3.2 or later (Debian/Ubuntu: sudo apt install tmux; Fedora: sudo dnf install tmux; or build from source into ~/.local).",
        }


# ---------------------------------------------------------------------- routes


class TerminalCreateIn(BaseModel):
    cwd: str | None = None
    label: str | None = None


class TerminalUpdateIn(BaseModel):
    label: str | None = None
    tab_order: int | None = None


def _mgr(ctx: AppContext) -> TerminalManager:
    assert ctx.terminals is not None
    return ctx.terminals


@router.get("/terminals")
def list_terminals(ctx: AppContext = Depends(require_auth)):
    m = _mgr(ctx)
    return {"terminals": m.list(), **m.info()}


@router.post("/terminals", status_code=201)
def create_terminal(body: TerminalCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _mgr(ctx).create(body.cwd, body.label)


@router.patch("/terminals/{tid}")
def update_terminal(tid: str, body: TerminalUpdateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _mgr(ctx).update(tid, body.label, body.tab_order)


@router.delete("/terminals/{tid}")
def delete_terminal(tid: str, ctx: AppContext = Depends(require_auth_mutating)):
    return _mgr(ctx).close(tid)


@router.get("/terminals/{tid}/history")
def terminal_history(tid: str, lines: int = Query(2000, ge=1, le=100000), ctx: AppContext = Depends(require_auth)):
    return _mgr(ctx).history(tid, lines)


def _valid_dims(cols: Any, rows: Any) -> tuple[int, int] | None:
    try:
        c, r = int(cols), int(rows)
    except (TypeError, ValueError):
        return None
    if not (MIN_COLS <= c <= MAX_COLS and MIN_ROWS <= r <= MAX_ROWS):
        return None
    return c, r


@router.websocket("/terminals/{tid}/ws")
async def terminal_ws(ws: WebSocket, tid: str):
    ctx: AppContext = ws.app.state.ctx
    try:
        subprotocol = ctx.auth.authenticate_websocket(ws)
    except ApiError as exc:
        await ws.close(code=4401 if exc.status == 401 else 4403, reason=exc.code)
        return
    mgr = _mgr(ctx)
    try:
        term = mgr.get(tid)
    except ApiError:
        await ws.close(code=4404, reason="TERMINAL_NOT_FOUND")
        return
    await ws.accept(subprotocol=subprotocol)

    async def send(obj: dict[str, Any]) -> None:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_text(json.dumps(obj))
            except Exception:
                pass

    if not mgr.tmux.available:
        await send({"type": "state", "state": "error", "message": "tmux is not installed on the server"})
        await ws.close(code=1011)
        return
    if not mgr.tmux.has_session(term.tmux_session):
        with mgr.db.write() as s:
            row = s.get(Terminal, tid)
            if row and row.state == "live":
                row.state = "exited"
        await send({"type": "state", "state": "exited", "message": "the tmux session for this terminal no longer exists; create a new terminal"})
        await ws.close(code=4410, reason=TERMINAL_GONE)
        return

    cols, rows = 120, 40
    qp = ws.query_params
    dims = _valid_dims(qp.get("cols", cols), qp.get("rows", rows))
    if dims:
        cols, rows = dims

    pty = PtyProcess(mgr.tmux.attach_args(term.tmux_session), env=mgr.tmux.env, cols=cols, rows=rows, cwd=term.cwd)
    exited = asyncio.Event()

    async def on_output(data: bytes) -> None:
        await send({"type": "output", "data": base64.b64encode(data).decode("ascii")})

    async def on_exit(rc: int | None) -> None:
        exited.set()

    pty.on_output = on_output
    pty.on_exit = on_exit
    try:
        pty.start()
    except OSError as exc:
        await send({"type": "state", "state": "error", "message": f"could not start tmux attach: {exc}"})
        await ws.close(code=1011)
        return
    mgr.clients[tid] = mgr.clients.get(tid, 0) + 1
    await send({"type": "state", "state": "connected", "message": f"attached to tmux session {term.tmux_session}"})

    async def watch_exit() -> None:
        await exited.wait()
        still = mgr.tmux.has_session(term.tmux_session)
        if still:
            await send({"type": "state", "state": "detached", "message": "tmux client detached; the session keeps running"})
        else:
            with mgr.db.write() as s:
                row = s.get(Terminal, tid)
                if row and row.state == "live":
                    row.state = "exited"
            mgr.bus.publish("global", {"type": "terminal_state", "terminal_id": tid, "state": "exited"})
            await send({"type": "state", "state": "exited", "message": "the terminal process exited"})
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close(code=1000)
            except Exception:
                pass

    watcher = asyncio.get_running_loop().create_task(watch_exit())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send({"type": "state", "state": "error", "message": "invalid JSON message"})
                continue
            mtype = msg.get("type")
            if mtype == "input":
                try:
                    data = base64.b64decode(msg.get("data", ""), validate=True)
                except (binascii.Error, ValueError, TypeError):
                    await send({"type": "state", "state": "error", "message": "invalid base64 input"})
                    continue
                pty.write(data)
            elif mtype == "resize":
                dims = _valid_dims(msg.get("cols"), msg.get("rows"))
                if dims is None:
                    await send({"type": "state", "state": "error", "message": f"invalid dimensions; cols {MIN_COLS}-{MAX_COLS}, rows {MIN_ROWS}-{MAX_ROWS}"})
                    continue
                pty.resize(*dims)
            elif mtype == "ping":
                await send({"type": "pong"})
            else:
                await send({"type": "state", "state": "error", "message": f"unknown message type {mtype!r}"})
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass
    finally:
        watcher.cancel()
        pty.close()  # detaches only this browser's tmux client
        mgr.clients[tid] = max(0, mgr.clients.get(tid, 1) - 1)

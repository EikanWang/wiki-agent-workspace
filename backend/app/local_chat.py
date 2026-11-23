"""Local chats: a conversation with the agent about a folder or a file of Local files (any path on the server).

A local chat is a folder chat (`.waw-chat/…`, a hidden-from-Chats document of the primary wiki folder, like trend
chats) marked with what it is about: kv `chat_local:<workspace id>:<chat path>` = {path, kind: folder|file, cwd}. Its
agent works in `cwd` — the opened Local files folder holding the path (else the folder itself / the file's folder) —
reads the files to answer, and edits them only when asked.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, not_found
from .kv import kv_get, kv_put

router = APIRouter()
LIST_KEY = "local_chats"


def _key(workspace_id: str, norm: str) -> str:
    return f"chat_local:{workspace_id}:{norm}"


def local_of(ctx: Any, workspace_id: str, norm: str) -> dict[str, str] | None:
    """{path, kind, cwd} when the chat is about a Local files folder / file, else None."""
    v = kv_get(ctx, _key(workspace_id, norm), None)
    return v if isinstance(v, dict) and v.get("path") and v.get("cwd") else None


def _cwd_for(ctx: AppContext, p: Path, is_dir: bool) -> Path:
    """The opened Local files folder holding `p` (the innermost one), else the folder itself / the file's folder."""
    from .localfs import KEY

    best: Path | None = None
    for r in kv_get(ctx, KEY, []) or []:
        if not isinstance(r, str):
            continue
        root = Path(r)
        if (p == root or root in p.parents) and root.is_dir() and (best is None or len(str(root)) > len(str(best))):
            best = root
    return best or (p if is_dir else p.parent)


class LocalChatIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    title: str = Field("", max_length=200)


@router.post("/local/chats", status_code=201)
def new_local_chat(body: LocalChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """A new, empty chat about a folder or a file of Local files (its document lives in the primary wiki folder)."""
    from .chats import chat_snapshot_path, new_chat
    from .localfs import _abs

    p = _abs(body.path)
    if not p.exists():
        raise not_found(f"{p} not found", "FILE_NOT_FOUND")
    if ctx.workspaces is None:
        raise ApiError(409, "NO_WIKI", "no wiki folder is open")
    is_dir = p.is_dir()
    about = {"path": str(p), "kind": "folder" if is_dir else "file", "cwd": str(_cwd_for(ctx, p, is_dir))}
    rt = ctx.workspaces.primary
    chat = new_chat(rt.docs, body.title.strip() or f"{p.name or p} · chat")
    snapshot = chat_snapshot_path(rt.docs.snapshots_dir, rt.id, chat["path"])
    assert snapshot is not None
    snapshot.write_text(f"# {chat['title']}\n\nAn open conversation with the agent about the {about['kind']} **{p}** "
                        "(Local files). The agent reads the files there to answer, and edits them only when asked.\n", encoding="utf-8")
    kv_put(ctx, _key(rt.id, chat["path"]), about)
    rows = [x for x in kv_get(ctx, LIST_KEY, []) or [] if isinstance(x, dict)]
    rows.append({"workspace_id": rt.id, "path": chat["path"]})
    kv_put(ctx, LIST_KEY, rows)
    return {**chat, "workspace_id": rt.id, "about": about}


@router.get("/local/chats")
def local_chats(path: str | None = Query(None, max_length=4096), ctx: AppContext = Depends(require_auth)):
    """Local chats, newest activity first: those about `path` (all of them without it)."""
    import datetime as dt

    from .chats import chat_snapshot_path, chat_title
    from .localfs import _abs
    from .models import Document, Message, Thread

    want = str(_abs(path)) if path else None
    out: list[dict[str, Any]] = []
    snaps = ctx.workspaces.primary.docs.snapshots_dir if ctx.workspaces else (ctx.documents.snapshots_dir if ctx.documents else None)
    with ctx.db.read() as s:
        for x in kv_get(ctx, LIST_KEY, []) or []:
            if not isinstance(x, dict) or not x.get("path") or not x.get("workspace_id"):
                continue
            about = local_of(ctx, x["workspace_id"], x["path"])
            if about is None or (want is not None and about["path"] != want):
                continue
            d = s.query(Document).filter_by(workspace_id=x["workspace_id"], path=x["path"]).one_or_none()
            p = chat_snapshot_path(snaps, x["workspace_id"], x["path"]) if snaps else None
            if d is None or d.hidden or p is None or not p.is_file():
                continue
            threads = s.query(Thread).filter_by(document_id=d.id).all()
            n = s.query(Message).filter(Message.thread_id.in_([t.id for t in threads])).count() if threads else 0
            times = [t.updated_at for t in threads if t.updated_at] + ([d.created_at] if d.created_at else [])
            last = max(times) if times else None
            out.append({"path": d.path, "document_id": d.id, "workspace_id": x["workspace_id"], "title": chat_title(p.read_text(encoding="utf-8")),
                        "messages": n, "about": about,
                        "updated_at": (last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)).isoformat() if last else None})
    out.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    return {"chats": out}


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def build_local_prompt(*, about: dict[str, str], question: str, prior: list[dict[str, str]] | None, extra: str | None = None,
                       brief: str | None = None, links: str | None = None, quote: str | None = None,
                       main: list[dict[str, str]] | None = None) -> str:
    """The prompt of a local chat's turn (`quote` + `main`: a side question about a part of an answer)."""
    cwd, path, kind = about["cwd"], about["path"], about["kind"]
    rel = os.path.relpath(path, cwd) if path != cwd else "."
    if path == cwd:
        focus = f"This conversation is about that whole folder ({path})."
    else:
        focus = (f"This conversation is about the {kind} `{rel}` (absolute: {path}): focus on it"
                 + (" and what it contains" if kind == "folder" else "") + ", and look at the rest of the folder when it helps.")
    parts = [
        f"You are chatting with the user about files on their server: the folder that is your working directory ({cwd}). " + focus,
        "Answer from the files: search and read them as needed, cite the files (and lines) you rely on"
        + (" (as links, see the end)" if links else " (paths)") + ", and say clearly when something is not in the files "
        "(then answer from general knowledge and mark it as such).",
    ]
    if extra:
        parts.append(extra)
    if brief:
        parts.append(brief)
    if main:
        parts.append("The main conversation (context):")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in main[-30:])
    if quote is not None:
        parts += ["The user selected this part of an answer and asks a side question about it:", _quoted(quote[:20000])]
    if prior:
        parts.append("The conversation so far:" if quote is None else "The side discussion so far:")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in prior[-40:])
    parts += ["User message:" if quote is None else "User's side question:", question]
    if quote is not None:
        parts.append("Instructions: answer the side question, not the main conversation. Do not modify files unless this "
                     "message explicitly asks for a change.")
    else:
        parts.append("Instructions: answer the user in Markdown. Only modify files if this user message explicitly asks for a "
                     "change; then edit the files in place and summarize what changed. Do not ask for additional "
                     "confirmation beyond your own configured permission rules.")
    if links:
        parts.append(links)
    return "\n\n".join(parts)

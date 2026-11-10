"""Folder chats: an open conversation with the agent about a wiki folder, not about one document.

A chat is the hidden read-only document `.waw-chat/<name>.md` of that folder (like PR tabs' `.waw-github/…`), so it
reuses discussions, sessions, streaming and Recent. Its file is a short Markdown stub kept in
`snapshots/chats/<folder id>/<name>.md` (never in the wiki). The agent works in the folder, answers from its pages,
and on request turns the conversation into wiki content: **distill** it into a new page, or **update** the pages
its content came from. Those edits are ordinary changes of the working tree, reviewed in the Diff view.
"""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import bad_request, not_found

router = APIRouter()

CHAT_PREFIX = ".waw-chat/"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}\.md$")


def is_chat_path(rel: str) -> bool:
    return rel.replace("\\", "/").lstrip("/").startswith(CHAT_PREFIX)


def chat_snapshot_path(snapshots_dir: Path, workspace_id: str, norm: str) -> Path | None:
    name = norm[len(CHAT_PREFIX):]
    if not _NAME.match(name) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", workspace_id or ""):
        return None
    return snapshots_dir / "chats" / workspace_id / name


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return s or "chat"


def chat_title(text: str) -> str:
    first = (text or "").split("\n", 1)[0]
    return first.lstrip("# ").strip() or "Chat"


class ChatIn(BaseModel):
    title: str = Field("", max_length=200)


@router.post("/chats", status_code=201)
def create_chat(body: ChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """A new, empty chat about this folder (the X-Workspace one)."""
    docs = ctx.documents
    assert docs is not None
    title = body.title.strip() or time.strftime("Chat %Y-%m-%d %H:%M")
    norm = f"{CHAT_PREFIX}{_slug(title)}-{secrets.token_hex(3)}.md"
    p = chat_snapshot_path(docs.snapshots_dir, docs.workspace_id, norm)
    assert p is not None
    p.parent.mkdir(parents=True, exist_ok=True)
    folder = docs.root.name
    p.write_text(f"# {title}\n\nAn open conversation with the agent about the **{folder}** folder — not about one document. "
                 "The agent searches and reads the wiki to answer, and can distill the conversation into a page or update "
                 "the pages it came from.\n", encoding="utf-8")
    doc = docs.get_or_create_document(norm)
    return {"path": norm, "document_id": doc.id, "title": title, "workspace_id": docs.workspace_id}


@router.get("/chats")
def list_chats(ctx: AppContext = Depends(require_auth)):
    """This folder's chats, newest activity first (archived ones are left out)."""
    from .models import Document, Message, Thread

    docs = ctx.documents
    assert docs is not None
    out: list[dict[str, Any]] = []
    with docs.db.read() as s:
        rows = s.query(Document).filter(Document.workspace_id == docs.workspace_id, Document.path.like(f"{CHAT_PREFIX}%"),
                                        Document.hidden.is_(False)).all()
        for d in rows:
            p = chat_snapshot_path(docs.snapshots_dir, docs.workspace_id, d.path)
            if p is None or not p.is_file():
                continue
            threads = s.query(Thread).filter_by(document_id=d.id).all()
            n = s.query(Message).filter(Message.thread_id.in_([t.id for t in threads])).count() if threads else 0
            times = [t.updated_at for t in threads if t.updated_at] + ([d.created_at] if d.created_at else [])
            last = max(times) if times else None
            out.append({"path": d.path, "document_id": d.id, "title": chat_title(p.read_text(encoding="utf-8")), "messages": n,
                        "updated_at": last.isoformat() if last else None})
    out.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    return {"chats": out}


@router.delete("/chats")
def archive_chat(path: str = Query(...), ctx: AppContext = Depends(require_auth_mutating)):
    """Archive a chat: it leaves the list (its discussion stays in the database, and in Recent if not hidden there)."""
    from .models import Document

    docs = ctx.documents
    assert docs is not None
    norm = docs.normalize_rel(path)
    if not is_chat_path(norm):
        raise bad_request("not a chat", "INVALID_PATH")
    with docs.db.write() as s:
        d = s.query(Document).filter_by(workspace_id=docs.workspace_id, path=norm).one_or_none()
        if d is None:
            raise not_found("chat not found", "FILE_NOT_FOUND")
        d.hidden = True
    return {"archived": norm}


# ---------------------------------------------------------------------- the agent's prompt

CHAT_ACTIONS = ("distill", "update")


def check_action(ctx: AppContext, action: str | None, target: str | None) -> str | None:
    """Validate a distill/update request; returns the normalized target page for distill."""
    if action is None:
        return None
    if action not in CHAT_ACTIONS:
        raise bad_request("chat_action must be distill or update", "INVALID_ACTION")
    if action == "update":
        return None
    docs = ctx.documents
    assert docs is not None
    norm = docs.normalize_rel(target or "")
    if not norm or not norm.lower().endswith(".md") or is_chat_path(norm) or norm.startswith(".waw-"):
        raise bad_request("give the new page's path in this folder, ending in .md", "INVALID_PATH")
    docs.resolve(norm, must_exist=False, for_write=True)
    if ctx.locks is not None:
        ctx.locks.assert_writable(norm, "distill a chat into")
    return norm


def build_chat_prompt(*, question: str, wiki_root: str, prior: list[dict[str, str]] | None, extra: str | None,
                      locked: list[str], action: str | None = None, target: str | None = None) -> str:
    parts = [
        f"You are chatting with the user about their wiki: the folder that is your working directory ({wiki_root}). "
        "This is an open conversation, not about one particular document.",
        "Answer from the wiki: search and read its pages as needed, cite the pages you rely on (relative paths), and say "
        "clearly when something is not covered by the wiki (then answer from general knowledge and mark it as such).",
    ]
    if extra:
        parts.append(extra)
    if locked:
        parts.append("These paths are LOCKED by the user and must NOT be modified: " + ", ".join(locked))
    if prior:
        parts.append("The conversation so far:")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in prior[-40:])
    parts += ["User message:", question]
    if action == "distill":
        parts.append(
            f"Instructions: distill this whole conversation into a wiki page at `{target}` (relative to the working "
            "directory; create it and any missing folders; if it exists, revise it rather than starting over). Write a "
            "standalone, well-structured article in the style of the existing pages (look at a few first): keep what the "
            "conversation established, drop the back-and-forth, keep the user's conclusions and corrections, and link the "
            "wiki pages the content came from with relative links. Do not invent facts. Do not modify any other file. "
            "Finish with a short summary of the page.")
    elif action == "update":
        parts.append(
            "Instructions: the content of this conversation comes largely from existing wiki pages. Update those source "
            "pages in place so they reflect what the conversation established (corrections, additions, clarifications, "
            "the user's conclusions), keeping each page's structure and style. Change only pages the conversation drew on "
            "or contradicted; do not create new pages unless the user asks; never modify locked paths. Finish with a list "
            "of the files you changed, one line each on what changed.")
    else:
        parts.append("Instructions: answer the user. Only modify files if this user message explicitly asks for a change; "
                     "then edit the wiki's files in place and summarize what changed. Do not ask for additional "
                     "confirmation beyond your own configured permission rules.")
    return "\n\n".join(parts)


def action_message(action: str, target: str | None, note: str) -> str:
    """What the discussion shows as the user's message for an action."""
    head = f"Distill this conversation into the wiki page `{target}`." if action == "distill" else \
        "Update the wiki pages this conversation's content came from."
    return head + (f"\n\n{note.strip()}" if note.strip() else "")

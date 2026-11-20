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
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, not_found

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
    about: str | None = Field(None, max_length=4096)  # a sub-folder or a file of the folder the chat is about
    scope: Literal["wiki", "control_center"] = "wiki"
    sources: list[Literal["reviews", "insights", "agents"]] = Field(default_factory=lambda: ["reviews", "insights", "agents"], max_length=3)


def _about_key(workspace_id: str, norm: str) -> str:
    return f"chat_about:{workspace_id}:{norm}"


def chat_about(ctx: AppContext, workspace_id: str, norm: str) -> dict[str, str] | None:
    """{path, kind: folder|file} when the chat is about a part of its folder (None: the whole folder)."""
    from .kv import kv_get

    v = kv_get(ctx, _about_key(workspace_id, norm), None)
    return v if isinstance(v, dict) and v.get("path") else None


def chat_scope(ctx: AppContext, workspace_id: str, norm: str) -> str:
    from .kv import kv_get

    value = kv_get(ctx, f"chat_scope:{workspace_id}:{norm}", "wiki")
    return "control_center" if value == "control_center" else "wiki"


def control_settings(ctx: AppContext, workspace_id: str, norm: str) -> dict[str, Any]:
    from .kv import kv_get

    return kv_get(ctx, f"chat_control:{workspace_id}:{norm}", {}) or {}


def can_read_chat(ctx: AppContext, workspace_id: str, norm: str) -> bool:
    if not is_chat_path(norm) or chat_scope(ctx, workspace_id, norm) == "wiki":
        return True
    owner = control_settings(ctx, workspace_id, norm).get("owner")
    return bool(owner and ctx.identity and owner.lower() == ctx.identity.lower())


def guard_chat_request(ctx: AppContext, request: Request) -> None:
    from .models import Document, Job, Thread

    params = request.path_params
    path = request.query_params.get("path") or ""
    if ctx.documents is not None and ".waw-chat" in path.replace("\\", "/").split("/"):
        path = ctx.documents.normalize_rel(path)
    workspace_id = ctx.workspace_id
    if any(key in params for key in ("doc_id", "thread_id", "job_id")):
        with ctx.db.read() as session:
            doc = session.get(Document, params["doc_id"]) if "doc_id" in params else None
            thread_id = params.get("thread_id")
            if "job_id" in params:
                job = session.get(Job, params["job_id"])
                thread_id = job.thread_id if job else None
            thread = session.get(Thread, thread_id) if thread_id else None
            if doc is None and thread is not None:
                doc = session.get(Document, thread.document_id)
            if doc is not None:
                path, workspace_id = doc.path, doc.workspace_id
    if is_chat_path(path) and not can_read_chat(ctx, workspace_id, path):
        raise ApiError(403, "CHAT_SCOPE_FORBIDDEN", "This Control Center conversation belongs to a different login")


@router.post("/chats", status_code=201)
def create_chat(body: ChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """A new, empty chat about this folder (the X-Workspace one)."""
    assert ctx.documents is not None
    if body.scope == "control_center" and body.about:
        raise bad_request("Control Center chats cannot target a wiki file or sub-folder", "INVALID_SCOPE")
    if body.scope == "wiki" and "sources" in body.model_fields_set:
        raise bad_request("Application sources require Control Center scope", "INVALID_SCOPE")
    about = None
    if body.about and body.about.strip("/"):
        rel, path = ctx.documents.resolve(body.about)
        if not path.exists():
            raise not_found(f"{rel} not found", "FILE_NOT_FOUND")
        about = {"path": rel, "kind": "folder" if path.is_dir() else "file"}
    out = new_chat(ctx.documents, body.title, about)
    if about:
        from .kv import kv_put

        kv_put(ctx, _about_key(ctx.documents.workspace_id, out["path"]), about)
    if body.scope == "control_center":
        from .kv import kv_put

        kv_put(ctx, f"chat_scope:{ctx.documents.workspace_id}:{out['path']}", body.scope)
        kv_put(ctx, f"chat_control:{ctx.documents.workspace_id}:{out['path']}",
               {"owner": ctx.identity, "sources": list(dict.fromkeys(body.sources))})
        snapshot = chat_snapshot_path(ctx.documents.snapshots_dir, ctx.documents.workspace_id, out["path"])
        assert snapshot is not None
        snapshot.write_text(f"# {out['title']}\n\nControl Center conversation. Read-only application context.\n", encoding="utf-8")
    out["about"] = about
    out["scope"] = body.scope
    out["sources"] = list(dict.fromkeys(body.sources)) if body.scope == "control_center" else []
    return out


def new_chat(docs: Any, title: str, about: dict[str, str] | None = None) -> dict[str, Any]:
    title = title.strip() or time.strftime("Chat %Y-%m-%d %H:%M")
    norm = f"{CHAT_PREFIX}{_slug(title)}-{secrets.token_hex(3)}.md"
    p = chat_snapshot_path(docs.snapshots_dir, docs.workspace_id, norm)
    assert p is not None
    p.parent.mkdir(parents=True, exist_ok=True)
    folder = docs.root.name
    what = f"the {about['kind']} **{about['path']}** of the **{folder}** folder" if about else f"the **{folder}** folder — not about one document"
    p.write_text(f"# {title}\n\nAn open conversation with the agent about {what}. "
                 "The agent searches and reads the wiki to answer, and can distill the conversation into a page or update "
                 "the pages it came from.\n", encoding="utf-8")
    doc = docs.get_or_create_document(norm)
    return {"path": norm, "document_id": doc.id, "title": title, "workspace_id": docs.workspace_id}


@router.get("/chats")
def list_chats(ctx: AppContext = Depends(require_auth)):
    """This folder's chats, newest activity first (archived ones are left out)."""
    from .insights.chat import trend_of
    from .models import Document, Message, Thread

    docs = ctx.documents
    assert docs is not None
    out: list[dict[str, Any]] = []
    with docs.db.read() as s:
        rows = s.query(Document).filter(Document.workspace_id == docs.workspace_id, Document.path.like(f"{CHAT_PREFIX}%"),
                                        Document.hidden.is_(False)).all()
        for d in rows:
            if not can_read_chat(ctx, docs.workspace_id, d.path):
                continue
            p = chat_snapshot_path(docs.snapshots_dir, docs.workspace_id, d.path)
            if p is None or not p.is_file() or trend_of(ctx, docs.workspace_id, d.path):
                continue  # (trend chats are listed in their repository's Trend page)
            threads = s.query(Thread).filter_by(document_id=d.id).all()
            n = s.query(Message).filter(Message.thread_id.in_([t.id for t in threads])).count() if threads else 0
            times = [t.updated_at for t in threads if t.updated_at] + ([d.created_at] if d.created_at else [])
            last = max(times) if times else None
            out.append({"path": d.path, "document_id": d.id, "title": chat_title(p.read_text(encoding="utf-8")), "messages": n,
                        "updated_at": last.isoformat() if last else None, "about": chat_about(ctx, docs.workspace_id, d.path),
                        "scope": chat_scope(ctx, docs.workspace_id, d.path),
                        "sources": control_settings(ctx, docs.workspace_id, d.path).get("sources", [])})
    out.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    return {"chats": out}


@router.get("/chats/context")
async def chat_context(path: str = Query(..., max_length=4096), ctx: AppContext = Depends(require_auth)):
    from .control_center import snapshot

    assert ctx.documents is not None
    norm = ctx.documents.normalize_rel(path)
    if not is_chat_path(norm) or chat_scope(ctx, ctx.workspace_id, norm) != "control_center":
        raise bad_request("Application context is only available to Control Center chats", "INVALID_SCOPE")
    return await snapshot(ctx, norm)


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
                      locked: list[str], action: str | None = None, target: str | None = None, brief: str | None = None,
                      about: dict[str, str] | None = None, links: str | None = None,
                      side: list[dict[str, Any]] | None = None, control_context: dict[str, Any] | None = None) -> str:
    parts = [
        f"You are chatting with the user about their wiki: the folder that is your working directory ({wiki_root}). "
        + (f"This conversation is about the {about['kind']} `{about['path']}` (relative to the working directory): focus on "
           f"it{' and what it contains' if about['kind'] == 'folder' else ''}, and look at the rest of the folder when it helps."
           if about else "This is an open conversation, not about one particular document."),
        "Answer from the wiki: search and read its pages as needed, cite the pages you rely on"
        + (" (as links, see the end)" if links else " (relative paths)") + ", and say "
        "clearly when something is not covered by the wiki (then answer from general knowledge and mark it as such).",
    ]
    if control_context is not None:
        from .control_center import context_prompt

        parts = [context_prompt(control_context)]
    if extra:
        parts.append(extra)
    if locked:
        parts.append("These paths are LOCKED by the user and must NOT be modified: " + ", ".join(locked))
    if brief:
        parts.append(brief)
    if prior:
        parts.append("The conversation so far:")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in prior[-40:])
    if side:
        parts.append("Side questions the user asked about parts of the conversation (they belong to it: include what they "
                     "established, the explanations and clarifications, where they fit):")
        for i, q in enumerate(side, 1):
            parts.append(f"Side question {i}, about this part of an answer:\n" + _quoted(q["quote"][:4000]))
            parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in q["messages"][-20:])
    parts += ["User message:", question]
    if control_context is not None:
        parts.append("Instructions: answer from the supplied application evidence and conversation only. Cite source URLs, "
                     "distinguish observations from recommendations, and report missing or stale data as unknown. "
                     "Do not use tools, execute commands, access additional files or APIs, or perform any action. "
                     "If an action is needed, describe the next step for the user in the existing application view.")
    elif action == "distill":
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
        if links:
            parts.append(links)
    return "\n\n".join(parts)


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def build_side_prompt(*, question: str, quote: str, wiki_root: str, main: list[dict[str, str]], prior: list[dict[str, str]] | None,
                      extra: str | None, locked: list[str], links: str | None = None,
                      control_context: dict[str, Any] | None = None) -> str:
    """A side question about a part of an answer in a folder chat: answered on the side, the chat itself goes on."""
    parts = [
        f"The user is chatting with you about their wiki: the folder that is your working directory ({wiki_root}). While reading "
        "one of the answers they selected a part of it and ask a side question about it, to understand it better. Answer that "
        "side question: explain clearly and to the point (search and read the wiki's pages when it helps and cite them"
        + (" as links, see the end" if links else " with relative paths") + "). Do not continue the main conversation.",
    ]
    if control_context is not None:
        from .control_center import context_prompt

        parts = [context_prompt(control_context), "Answer the side question, not the main conversation. "
                 "Use only the supplied evidence; do not use tools, execute commands, modify files, or change application state."]
    if extra:
        parts.append(extra)
    if locked:
        parts.append("These paths are LOCKED by the user and must NOT be modified: " + ", ".join(locked))
    if main:
        parts.append("The main conversation (context):")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in main[-30:])
    parts += ["The part of the answer the user selected:", _quoted(quote[:20000])]
    if prior:
        parts.append("The side discussion so far:")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in prior[-30:])
    parts += ["User's side question:", question,
              "Instructions: answer the side question. " + ("This conversation is read-only; do not perform actions."
               if control_context is not None else "Do not modify files unless this message explicitly asks for a change.")]
    if links:
        parts.append(links)
    return "\n\n".join(parts)


def action_message(action: str, target: str | None, note: str) -> str:
    """What the discussion shows as the user's message for an action."""
    head = f"Distill this conversation into the wiki page `{target}`." if action == "distill" else \
        "Update the wiki pages this conversation's content came from."
    return head + (f"\n\n{note.strip()}" if note.strip() else "")

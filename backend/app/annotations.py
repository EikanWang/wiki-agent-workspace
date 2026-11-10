"""Discussions anchored to document passages: threads, messages, reanchoring (DESIGN §5.2, §10, §13)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import anchors as anchoring
from .db import iso, new_id, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found, DOCUMENT_VERSION_CONFLICT, INVALID_ANCHOR
from .models import AgentSession, AnchorHistory, Document, Job, Message, Thread
from .sessions import SessionService
from .chats import action_message, build_chat_prompt, check_action, is_chat_path
from .documents import is_pr_path
from .textmodel import NO_TEXT_FORMATS, TextModel, build_text_model_from_html

router = APIRouter()


# ---------------------------------------------------------------------- prompt building

PR_INLINE_CHARS = 60_000


def build_pr_prompt(*, question: str, quote: str, copy_text: str, copy_path: str, wiki_root: str, target: dict[str, Any] | None,
                    prior: list[dict[str, str]] | None, extra: str | None = None, review: bool = False) -> str:
    """Prompt for a discussion about a GitHub pull request (review hub)."""
    head = copy_text.split("\n", 1)[0].lstrip("# ").strip()
    parts = [
        f"You are helping the user review the GitHub pull request {head}.",
        f"Your working directory is the user's wiki ({wiki_root}): use it for background knowledge about the code and the "
        "hardware/software concepts involved, and cite wiki pages you rely on.",
        f"The pull request (metadata, description and full diff) is in this file: {copy_path}",
    ]
    if extra:
        parts.append(extra)
    copy_text, _, discussion = copy_text.partition("\n\x00DISCUSSION\x00")
    if review:
        from .github_hub import numbered_diffs

        copy_text = numbered_diffs(copy_text)
    if len(copy_text) <= PR_INLINE_CHARS:
        parts.append("<<<PULL_REQUEST\n" + copy_text + "\nPULL_REQUEST>>>")
    else:
        parts.append("<<<PULL_REQUEST (first part; read the file for the rest)\n" + copy_text[:PR_INLINE_CHARS] + "\nPULL_REQUEST>>>")
    if discussion.strip():
        parts.append("What reviewers have said so far (conversation and review threads; consider it, don't repeat resolved points):")
        parts.append("<<<REVIEW_DISCUSSION\n" + discussion[:40_000] + "\nREVIEW_DISCUSSION>>>")
    if target and target.get("kind") == "text" and quote:
        where = target.get("label") or "the diff"
        parts.append(f"The user selected these lines of {where} in the pull request (verbatim):")
        parts.append("<<<SELECTED_PASSAGE\n" + quote + "\nSELECTED_PASSAGE>>>")
    if prior:
        parts.append("Earlier messages in this discussion thread (for context):")
        parts.extend(f"[{m['role']}] {m['content'][:2000]}" for m in prior[-8:])
    if review:
        from .github_hub import review_instructions

        parts.append(review_instructions())
    parts += ["User message:", question,
              "Instructions: answer as a careful code reviewer (correctness, performance, tests, API and design). Do not modify any "
              "files and never comment on, approve, push to or otherwise change the pull request on GitHub — only answer."]
    return "\n\n".join(parts)


def _pr_copy(docs, doc_path: str) -> tuple[str, str]:
    from .documents import pr_snapshot_path

    p = pr_snapshot_path(docs.snapshots_dir, doc_path)
    text = p.read_text(encoding="utf-8") if p and p.is_file() else ""
    disc = p.with_name(p.stem + ".discussion.md") if p else None
    if disc and disc.is_file():  # what reviewers said (written when the PR's comments load); kept apart from the diff
        text += "\n\x00DISCUSSION\x00" + disc.read_text(encoding="utf-8")
    return text, str(p) if p else ""


def describe_target(target: dict[str, Any] | None, document_path: str) -> str | None:
    """What part of a laid-out document the user points at (PDF page, slide, area, embedded image)."""
    if not target:
        return None
    unit = "slide" if target.get("unit") == "slide" else "page"
    n = target.get("page")
    where = f"{unit} {n}" if n else "the document"
    layout = Path(document_path).suffix.lower() != ".pdf"
    note = f" (pages are those of the document rendered to PDF by LibreOffice{'; slide numbers match' if unit == 'slide' else ''})" if layout and n else ""
    kind = target.get("kind")
    if kind == "page":
        return f"The user is asking about {where} of this document as a whole{note}. An image of that {unit} is attached."
    if kind == "area":
        return f"The user selected an area of {where}{note} (e.g. a figure or diagram). An image of exactly that area is attached."
    if kind == "image":
        return f"The user is asking about an image embedded in this document{' on ' + where if n else ''}. The image is attached."
    if kind == "text":
        return f"The user selected text on {where}{note}."
    return None


def build_prompt(*, question: str, quote: str, document_path: str, sha256: str, heading: str | None,
                 prior: list[dict[str, str]] | None, versions: list[dict[str, Any]] | None, whole_document: bool = False,
                 target: dict[str, Any] | None = None, extra: str | None = None) -> str:
    parts = [
        "You are assisting with a Markdown wiki document in this workspace via the Wiki Agent Workspace reader.",
        f"Document path: {document_path}",
        f"Document content SHA-256: {sha256}",
    ]
    if extra:
        parts.append(extra)
    if heading:
        parts.append(f"Nearest heading: {heading}")
    if versions:
        refs = ", ".join(f"{v['sha256'][:12]} ({v.get('git_commit') or 'uncommitted'}{', ' + v['git_branch'] if v.get('git_branch') else ''})" for v in versions[:5])
        parts.append(f"Known earlier recorded versions of this file (content hashes, Git commit if any): {refs}. "
                     "Only Git history and these recorded snapshots are available as history; do not claim to know earlier reasoning that is not in them.")
    tdesc = describe_target(target, document_path)
    if tdesc:
        parts.append(tdesc)
        if quote:
            parts.append("Selected text (verbatim):")
            parts.append("<<<SELECTED_PASSAGE\n" + quote + "\nSELECTED_PASSAGE>>>")
    elif whole_document or quote == document_path:
        from .textmodel import IMAGE_SUFFIXES

        if Path(document_path).suffix.lower() in IMAGE_SUFFIXES:
            parts.append("The user is asking about this image as a whole. It is attached to this message; the file is at the path above.")
        else:
            parts.append("The user is asking about this document as a whole (no specific passage selected). Read the file at the path above.")
    else:
        parts.append("The user selected this exact passage in the rendered document (verbatim):")
        parts.append("<<<SELECTED_PASSAGE\n" + quote + "\nSELECTED_PASSAGE>>>")
    if prior:
        parts.append("Earlier messages in this discussion thread (for context):")
        parts.extend(f"[{m['role']}] {m['content'][:2000]}" for m in prior[-8:])
    parts.append("User message:")
    parts.append(question)
    parts.append(
        f"Instructions: answer about {'what the user points at and the document' if tdesc else 'the document' if whole_document or quote == document_path else 'the selected passage and the document'}. Only modify files if this user message "
        "explicitly asks for a change; in that case edit the document file at the path above in place and summarize what changed. "
        "Do not ask for additional confirmation beyond your own configured permission rules."
    )
    return "\n\n".join(parts)


def image_attachments(docs, doc_path: str) -> list[str]:
    """The image being discussed, attached to the agent's turn (OpenCode --file, Codex --image) so it really sees it."""
    from .textmodel import document_format

    if document_format(Path(doc_path).name) != "image":
        return []
    try:
        _, p = docs.resolve(doc_path)
    except Exception:
        return []
    return [str(p.resolve())] if p.is_file() else []


MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024


def decode_image(data_url: str) -> tuple[bytes, str]:
    """A data: URL from the browser (page render, area crop, embedded picture) → (bytes, extension). PNG/JPEG/GIF/WebP only."""
    import base64

    m = re.match(r"^data:image/(png|jpeg|jpg|gif|webp);base64,(.+)$", data_url or "", re.S)
    if not m:
        raise bad_request("image must be a data:image/(png|jpeg|gif|webp);base64 URL", "INVALID_IMAGE")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except ValueError:
        raise bad_request("image is not valid base64", "INVALID_IMAGE")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise bad_request(f"image is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB", "IMAGE_TOO_LARGE")
    magic = {"png": b"\x89PNG", "jpeg": b"\xff\xd8", "jpg": b"\xff\xd8", "gif": b"GIF8", "webp": b"RIFF"}[m.group(1)]
    if not raw.startswith(magic):
        raise bad_request("image content does not match its type", "INVALID_IMAGE")
    return raw, ("jpg" if m.group(1) == "jpeg" else m.group(1))


def attachments_dir(snapshots_dir: Path, thread_id: str) -> Path:
    return snapshots_dir / "attachments" / thread_id


def thread_attachments(snapshots_dir: Path, t: Thread) -> list[str]:
    """Paths of the images stored with a discussion (sent with every turn of it)."""
    if not t.target_json:
        return []
    names = json.loads(t.target_json).get("attachments") or []
    d = attachments_dir(snapshots_dir, t.id)
    return [str(d / n) for n in names if (d / n).is_file()]


# ---------------------------------------------------------------------- service


def thread_dict(t: Thread, messages: list[Message], jobs_by_message: dict[str, Job]) -> dict[str, Any]:
    return {
        "id": t.id,
        "document_id": t.document_id,
        "original_anchor": anchoring.Anchor(t.orig_version_sha256, t.orig_quote, t.orig_prefix, t.orig_suffix, t.orig_start, t.orig_end, t.orig_heading).as_dict(),
        "anchor": anchoring.Anchor(t.cur_version_sha256, t.cur_quote, t.cur_prefix, t.cur_suffix, t.cur_start, t.cur_end, t.cur_heading).as_dict(),
        "anchor_state": t.anchor_state,
        "diff": json.loads(t.diff_json) if t.diff_json else None,
        "target": json.loads(t.target_json) if t.target_json else None,
        "last_session_id": t.last_session_id,
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
        "messages": [
            {
                "id": m.id, "role": m.role, "content": m.content, "status": m.status, "session_id": m.session_id,
                "job_id": m.job_id, "created_at": iso(m.created_at),
                "error_code": (jobs_by_message[m.id].error_code if m.id in jobs_by_message else None),
                "error_message": (jobs_by_message[m.id].error_message if m.id in jobs_by_message else None),
            }
            for m in messages
        ],
    }


class ThreadService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        assert ctx.documents and ctx.jobs
        self.docs = ctx.documents
        self.jobs = ctx.jobs
        self.sessions = SessionService(ctx)

    def _model(self, doc: Document) -> tuple[TextModel, str]:
        """Full text model (with headings) of the current content, any supported format."""
        _, data, sha = self.docs.current_content(doc)
        fmt, html = self.docs.render(doc.path, data)
        if fmt in NO_TEXT_FORMATS:
            return TextModel(text="", headings=[]), sha
        return build_text_model_from_html(html), sha

    # ------------------------------------------------------------------ listing
    def list_threads(self, doc_id: str) -> dict[str, Any]:
        doc = self.docs.get_document(doc_id)
        try:
            model, sha = self.docs.model_for_doc(doc)
        except ApiError as exc:
            if exc.code != "DOCUMENT_MISSING":
                raise
            model, sha = None, None
        with self.ctx.db.read() as s:
            threads = s.query(Thread).filter_by(document_id=doc_id).order_by(Thread.created_at).all()
            out = []
            for t in threads:
                msgs = s.query(Message).filter_by(thread_id=t.id).order_by(Message.created_at).all()
                jobs = {}
                for m in msgs:
                    if m.job_id:
                        j = s.get(Job, m.job_id)
                        if j:
                            jobs[m.id] = j
                d = thread_dict(t, msgs, jobs)
                if t.anchor_state in ("document", "page"):
                    d["anchor_state"] = t.anchor_state
                    d["current_start"] = None
                    d["current_end"] = None
                elif model is not None and sha is not None:
                    res = anchoring.resolve(anchoring.Anchor(t.cur_version_sha256, t.cur_quote, t.cur_prefix, t.cur_suffix, t.cur_start, t.cur_end, t.cur_heading), model, sha)
                    d["anchor_state"] = res.state
                    d["current_start"] = res.start
                    d["current_end"] = res.end
                    d["candidates"] = res.candidates
                else:
                    d["anchor_state"] = "orphaned"
                    d["current_start"] = None
                    d["current_end"] = None
                out.append(d)
        return {"document_id": doc_id, "sha256": sha, "missing": sha is None, "threads": out}

    # ------------------------------------------------------------------ sessions
    async def _session_for_new_thread(self, doc: Document, requested: str | None) -> AgentSession:
        if requested == "new":
            return await self._new_default_session(doc)
        if requested:
            sess = self.sessions.get(requested)
            return sess
        if doc.default_session_id:
            with self.ctx.db.read() as s:
                sess = s.get(AgentSession, doc.default_session_id)
            if sess is not None:
                return sess
        return await self._new_default_session(doc)

    async def _new_default_session(self, doc: Document) -> AgentSession:
        assert self.ctx.providers is not None
        provider = self.ctx.providers.default_provider()
        if not provider:
            raise ApiError(503, "PROVIDER_UNAVAILABLE",
                           "No default agent provider is configured or detected (opencode/codex). Configure agents.default_provider; "
                           "the reader and terminal remain usable.")
        sess = await self.sessions.create_for_provider(provider, f"Wiki: {doc.path}")
        with self.ctx.db.write() as s:
            d = s.get(Document, doc.id)
            if d and d.default_session_id is None:
                d.default_session_id = sess.id
        return sess

    # ------------------------------------------------------------------ create
    async def create_thread(self, doc_id: str, body: "ThreadCreateIn") -> dict[str, Any]:
        doc = self.docs.get_document(doc_id)
        model, sha = self._model(doc)
        if sha != body.version_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "the document changed since you selected the passage; reload and reselect",
                           current_sha256=sha, submitted_sha256=body.version_sha256)
        if body.quote == doc.path and body.start == body.end == 0:
            body.whole_document = True  # older clients: "Ask about this PDF" sent the path as the quote
        target: dict[str, Any] | None = None
        image: tuple[bytes, str] | None = decode_image(body.image) if body.image else None
        if body.target is not None:
            # a page / slide / area / picture of a laid-out document (or a picture in the text view): no text anchor
            target = {"kind": body.target.kind, "page": body.target.page, "unit": body.target.unit,
                      "rect": body.target.rect, "label": (body.target.label or None)}
            if body.target.kind in ("page", "area", "image") and image is None:
                raise bad_request(f"a question about a {body.target.kind} needs its image", "IMAGE_REQUIRED")
            body.whole_document = True
            body.quote = body.quote[:20000] if body.target.kind == "text" else ""
            body.prefix, body.suffix, body.start, body.end, body.heading = "", "", 0, 0, None
        elif body.whole_document:
            # a question about the document as a whole: no passage, nothing to validate or re-anchor later
            body.quote, body.prefix, body.suffix, body.start, body.end, body.heading = "", "", "", 0, 0, None
        err = None if body.whole_document else anchoring.validate_offsets(model.text, body.start, body.end, body.quote)
        if err:
            # Offsets did not match the server model (stale selection after a re-render, or a renderer edge case).
            # Re-locate the quote with its stored context exactly like re-anchoring does; accept a unique hit.
            res = anchoring.resolve(
                anchoring.Anchor(body.version_sha256 + "-stale", body.quote, body.prefix, body.suffix, body.start, body.end, body.heading),
                model.text, sha,
            )
            if res.start is None or res.end is None:
                raise bad_request(f"invalid anchor: {err}", INVALID_ANCHOR, occurrences=model.text.count(body.quote), state=res.state)
            body.start, body.end = res.start, res.end
            body.prefix = res.prefix if res.prefix is not None else body.prefix
            body.suffix = res.suffix if res.suffix is not None else body.suffix
        sess = await self._session_for_new_thread(doc, body.session_id)
        heading = None if body.whole_document else (body.heading or model.heading_for(body.start))
        user_msg_id = new_id()
        agent_msg_id = new_id()
        thread_id = new_id()
        with self.ctx.db.write() as s:
            t = Thread(
                id=thread_id, document_id=doc_id,
                orig_version_sha256=sha, orig_quote=body.quote, orig_prefix=body.prefix, orig_suffix=body.suffix,
                orig_start=body.start, orig_end=body.end, orig_heading=heading,
                cur_version_sha256=sha, cur_quote=body.quote, cur_prefix=body.prefix, cur_suffix=body.suffix,
                cur_start=body.start, cur_end=body.end, cur_heading=heading,
                anchor_state="page" if target else ("document" if body.whole_document else "attached"),
                last_session_id=sess.id,
            )
            s.add(t)
            s.flush()
            s.add(Message(id=user_msg_id, thread_id=thread_id, session_id=sess.id, role="user", content=body.question, status="completed"))
            s.add(Message(id=agent_msg_id, thread_id=thread_id, session_id=sess.id, role="agent", content="", status="queued"))
        attach = image_attachments(self.docs, doc.path)
        if target is not None:
            if image is not None:
                d = attachments_dir(self.docs.snapshots_dir, thread_id)
                d.mkdir(parents=True, exist_ok=True)
                name = f"{target['kind']}.{image[1]}"
                (d / name).write_bytes(image[0])
                target["attachments"] = [name]
                attach = attach + [str(d / name)]
            with self.ctx.db.write() as s:
                tt = s.get(Thread, thread_id)
                if tt:
                    tt.target_json = json.dumps(target, ensure_ascii=False)
        if is_chat_path(doc.path):
            prompt = build_chat_prompt(question=body.question, wiki_root=str(self.docs.root), prior=None, extra=self._extra(doc.path),
                                       locked=self._locked())
        elif is_pr_path(doc.path):
            text, cpath = _pr_copy(self.docs, doc.path)
            prompt = build_pr_prompt(question=body.question, quote=body.quote, copy_text=text, copy_path=cpath,
                                     wiki_root=str(self.docs.root), target=target, prior=None,
                                     extra=self._extra(doc.path), review=body.pr_review)
        else:
            prompt = build_prompt(question=body.question, quote=body.quote, document_path=doc.path, sha256=sha, heading=heading,
                                  prior=None, versions=self.docs.list_versions(doc_id), whole_document=body.whole_document, target=target,
                                  extra=self._extra(doc.path))
        job_id = self.jobs.enqueue_turn(session_id=sess.id, thread_id=thread_id, prompt=prompt,
                                        context={"document_id": doc_id, "document_path": doc.path, "sha256": sha, "quote": body.quote,
                                                 "attachments": attach},
                                        agent_message_id=agent_msg_id)
        with self.ctx.db.write() as s:
            m = s.get(Message, agent_msg_id)
            if m:
                m.job_id = job_id
        if body.pr_review and is_pr_path(doc.path):  # its comments become suggestions in the diff when it finishes
            from .github_hub import register_agent_review

            register_agent_review(self.ctx, doc.path, job_id, thread_id, agent_msg_id)
        return {"thread_id": thread_id, "job_id": job_id, "session_id": sess.id, "message_id": user_msg_id, "agent_message_id": agent_msg_id}

    # ------------------------------------------------------------------ reply
    async def add_message(self, thread_id: str, body: "MessageIn") -> dict[str, Any]:
        with self.ctx.db.read() as s:
            t = s.get(Thread, thread_id)
            if t is None:
                raise not_found("thread not found", "THREAD_NOT_FOUND")
            doc = s.get(Document, t.document_id)
            prior = [{"role": m.role, "content": m.content} for m in s.query(Message).filter_by(thread_id=thread_id).order_by(Message.created_at).all() if m.status == "completed"]
        assert doc is not None
        if body.session_id == "new":
            sess = await self._new_default_session_for_thread(doc)
        elif body.session_id:
            sess = self.sessions.get(body.session_id)
        elif t.last_session_id:
            sess = self.sessions.get(t.last_session_id)
        else:
            sess = await self._session_for_new_thread(doc, None)
        try:
            _, _, sha = self.docs.current_content(doc)
        except ApiError:
            sha = t.cur_version_sha256
        target = None
        shown = body.text
        if body.chat_action is not None:
            if not is_chat_path(doc.path):
                raise ApiError(400, "INVALID_ACTION", "distill / update are for folder chats")
            target = check_action(self.ctx, body.chat_action, body.target_path)
            shown = action_message(body.chat_action, target, body.text if body.text.strip() != "-" else "")
        user_msg_id = new_id()
        agent_msg_id = new_id()
        with self.ctx.db.write() as s:
            s.add(Message(id=user_msg_id, thread_id=thread_id, session_id=sess.id, role="user", content=shown, status="completed"))
            s.add(Message(id=agent_msg_id, thread_id=thread_id, session_id=sess.id, role="agent", content="", status="queued"))
            tt = s.get(Thread, thread_id)
            if tt:
                tt.last_session_id = sess.id
                tt.updated_at = utcnow()
        if is_chat_path(doc.path):
            prompt = build_chat_prompt(question=shown, wiki_root=str(self.docs.root), prior=prior, extra=self._extra(doc.path),
                                       locked=self._locked(), action=body.chat_action, target=target)
        elif is_pr_path(doc.path):
            text, cpath = _pr_copy(self.docs, doc.path)
            prompt = build_pr_prompt(question=body.text, quote=t.cur_quote, copy_text=text, copy_path=cpath, wiki_root=str(self.docs.root),
                                     target=json.loads(t.target_json) if t.target_json else None, prior=prior, extra=self._extra(doc.path))
        else:
            prompt = build_prompt(question=body.text, quote=t.cur_quote, document_path=doc.path, sha256=sha, heading=t.cur_heading,
                                  prior=prior, versions=self.docs.list_versions(doc.id), whole_document=t.anchor_state in ("document", "page"),
                                  target=json.loads(t.target_json) if t.target_json else None, extra=self._extra(doc.path))
        job_id = self.jobs.enqueue_turn(session_id=sess.id, thread_id=thread_id, prompt=prompt,
                                        context={"document_id": doc.id, "document_path": doc.path, "sha256": sha, "quote": t.cur_quote,
                                                 "attachments": image_attachments(self.docs, doc.path) + thread_attachments(self.docs.snapshots_dir, t)},
                                        agent_message_id=agent_msg_id)
        with self.ctx.db.write() as s:
            m = s.get(Message, agent_msg_id)
            if m:
                m.job_id = job_id
        return {"message_id": user_msg_id, "job_id": job_id, "session_id": sess.id, "agent_message_id": agent_msg_id}

    def _locked(self) -> list[str]:
        locks = self.ctx.locks
        return sorted({x["path"] or "(the whole folder)" for x in locks.list()}) if locks is not None else []

    def _extra(self, doc_path: str) -> str | None:
        """Extra reference folders the user added for this wiki folder / this PR's repository."""
        from .kv import extra_context

        return extra_context(self.ctx, doc_path, self.docs.workspace_id)

    async def _new_default_session_for_thread(self, doc: Document) -> AgentSession:
        """`new` for a reply: new session for this thread only (does not change the document default)."""
        assert self.ctx.providers is not None
        provider = self.ctx.providers.default_provider()
        if not provider:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", "no default agent provider configured")
        return await self.sessions.create_for_provider(provider, f"Wiki: {doc.path}")

    # ------------------------------------------------------------------ retry
    async def retry(self, thread_id: str, message_id: str, session_id: str | None) -> dict[str, Any]:
        """Explicit retry: a new job attached to the same discussion for a failed/cancelled/interrupted agent message."""
        with self.ctx.db.read() as s:
            t = s.get(Thread, thread_id)
            if t is None:
                raise not_found("thread not found", "THREAD_NOT_FOUND")
            msgs = s.query(Message).filter_by(thread_id=thread_id).order_by(Message.created_at).all()
        target = next((m for m in msgs if m.id == message_id), None)
        if target is None or target.role != "agent":
            raise not_found("agent message not found", "MESSAGE_NOT_FOUND")
        if target.status not in ("failed", "cancelled", "interrupted"):
            raise conflict("MESSAGE_NOT_RETRYABLE", f"message status is {target.status}")
        idx = msgs.index(target)
        user = next((m for m in reversed(msgs[:idx]) if m.role == "user"), None)
        if user is None:
            raise conflict("MESSAGE_NOT_RETRYABLE", "no user question found for this message")
        return await self.add_message(thread_id, MessageIn(text=user.content, session_id=session_id))

    # ------------------------------------------------------------------ reanchor
    def reanchor(self, thread_id: str, body: "AnchorUpdateIn") -> dict[str, Any]:
        with self.ctx.db.read() as s:
            t = s.get(Thread, thread_id)
            if t is None:
                raise not_found("thread not found", "THREAD_NOT_FOUND")
            doc = s.get(Document, t.document_id)
        assert doc is not None
        model, sha = self._model(doc)
        if sha != body.version_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "document changed; reload before reanchoring", current_sha256=sha)
        err = anchoring.validate_offsets(model.text, body.start, body.end, body.quote)
        if err:
            res = anchoring.resolve(anchoring.Anchor(sha + "-stale", body.quote, body.prefix, body.suffix, body.start, body.end, body.heading), model.text, sha)
            if res.start is None or res.end is None:
                raise bad_request(f"invalid anchor: {err}", INVALID_ANCHOR, state=res.state)
            body.start, body.end = res.start, res.end
        with self.ctx.db.write() as s:
            tt = s.get(Thread, thread_id)
            assert tt is not None
            tt.cur_version_sha256 = sha
            tt.cur_quote = body.quote
            tt.cur_prefix = body.prefix
            tt.cur_suffix = body.suffix
            tt.cur_start = body.start
            tt.cur_end = body.end
            tt.cur_heading = body.heading or model.heading_for(body.start)
            tt.anchor_state = "attached"
            tt.updated_at = utcnow()
            s.add(AnchorHistory(id=new_id(), thread_id=thread_id, kind="manual", version_sha256=sha, quote=body.quote,
                                prefix=body.prefix, suffix=body.suffix, start=body.start, end=body.end,
                                heading=tt.cur_heading, state="attached"))
        return {"thread_id": thread_id, "anchor_state": "attached", "version_sha256": sha}

    def anchor_history(self, thread_id: str) -> dict[str, Any]:
        with self.ctx.db.read() as s:
            t = s.get(Thread, thread_id)
            if t is None:
                raise not_found("thread not found", "THREAD_NOT_FOUND")
            rows = s.query(AnchorHistory).filter_by(thread_id=thread_id).order_by(AnchorHistory.created_at).all()
            return {
                "thread_id": thread_id,
                "original": anchoring.Anchor(t.orig_version_sha256, t.orig_quote, t.orig_prefix, t.orig_suffix, t.orig_start, t.orig_end, t.orig_heading).as_dict(),
                "history": [
                    {"kind": h.kind, "version_sha256": h.version_sha256, "quote": h.quote, "start": h.start, "end": h.end,
                     "heading": h.heading, "state": h.state, "created_at": iso(h.created_at)}
                    for h in rows
                ],
            }


# ---------------------------------------------------------------------- schemas


class TargetIn(BaseModel):
    kind: Literal["text", "page", "area", "image"]
    page: int | None = Field(None, ge=1, le=100000)
    unit: Literal["page", "slide"] = "page"
    rect: list[float] | None = Field(None, min_length=4, max_length=4)  # x, y, w, h as fractions of the page
    label: str | None = Field(None, max_length=200)


class ThreadCreateIn(BaseModel):
    version_sha256: str
    quote: str = Field("", max_length=20000)
    prefix: str = Field("", max_length=80)
    suffix: str = Field("", max_length=80)
    start: int = Field(0, ge=0)
    end: int = Field(0, ge=0)
    heading: str | None = None
    question: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None  # app session id, or "new"
    # A discussion about the document as a whole (no passage): used for PDFs and other formats without text anchors.
    whole_document: bool = False
    # A page / slide / area / picture of a laid-out document (PDF, Office layout view) or a picture in a text view.
    target: "TargetIn | None" = None
    # data:image/...;base64 — the page render, area crop or picture (required for page/area/image targets)
    image: str | None = Field(None, max_length=17_000_000)
    # PR tab: an agent review — its line comments are added to the diff as suggestions (Apply / Discard)
    pr_review: bool = False


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None
    # folder chats: turn the conversation into wiki content — distill (into target_path) or update the source pages;
    # `text` is then an optional note for the agent ("-" for none)
    chat_action: str | None = None
    target_path: str | None = Field(None, max_length=1000)


class AnchorUpdateIn(BaseModel):
    version_sha256: str
    quote: str = Field(min_length=1, max_length=20000)
    prefix: str = Field("", max_length=80)
    suffix: str = Field("", max_length=80)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    heading: str | None = None


class RetryIn(BaseModel):
    message_id: str
    session_id: str | None = None


class PermissionDecisionIn(BaseModel):
    request_id: str
    decision: str
    remember: bool | None = None


# ---------------------------------------------------------------------- routes


@router.get("/documents/{doc_id}/threads")
def list_threads(doc_id: str, ctx: AppContext = Depends(require_auth)):
    return ThreadService(ctx).list_threads(doc_id)


@router.get("/threads/{thread_id}/attachments/{name}")
def get_attachment(thread_id: str, name: str, ctx: AppContext = Depends(require_auth)):
    """An image stored with a discussion (page render, area crop, picture): shown as a thumbnail in the panel."""
    from fastapi.responses import FileResponse

    if not re.fullmatch(r"[a-z]+\.(png|jpg|gif|webp)", name):
        raise not_found("attachment not found", "ATTACHMENT_NOT_FOUND")
    with ctx.db.read() as s:
        t = s.get(Thread, thread_id)
    if t is None or name not in (json.loads(t.target_json).get("attachments", []) if t.target_json else []):
        raise not_found("attachment not found", "ATTACHMENT_NOT_FOUND")
    p = attachments_dir(ctx.documents.snapshots_dir, thread_id) / name
    if not p.is_file():
        raise not_found("attachment not found", "ATTACHMENT_NOT_FOUND")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.post("/documents/{doc_id}/threads", status_code=201)
async def create_thread(doc_id: str, body: ThreadCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await ThreadService(ctx).create_thread(doc_id, body)


@router.post("/threads/{thread_id}/messages", status_code=201)
async def add_message(thread_id: str, body: MessageIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await ThreadService(ctx).add_message(thread_id, body)


@router.post("/threads/{thread_id}/retry", status_code=201)
async def retry_message(thread_id: str, body: RetryIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await ThreadService(ctx).retry(thread_id, body.message_id, body.session_id)


@router.put("/threads/{thread_id}/anchor")
def put_anchor(thread_id: str, body: AnchorUpdateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return ThreadService(ctx).reanchor(thread_id, body)


@router.get("/threads/{thread_id}/anchor-history")
def get_anchor_history(thread_id: str, ctx: AppContext = Depends(require_auth)):
    return ThreadService(ctx).anchor_history(thread_id)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, ctx: AppContext = Depends(require_auth)):
    assert ctx.jobs
    return ctx.jobs.get(job_id)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = 0, ctx: AppContext = Depends(require_auth)):
    assert ctx.jobs
    ctx.jobs.get(job_id)  # 404 if unknown

    async def gen():
        async for ev in ctx.jobs.stream(job_id, after):
            if await request.is_disconnected():
                return
            if ev["type"] == "heartbeat":
                yield ": keepalive\n\n"
                continue
            yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    assert ctx.jobs
    return await ctx.jobs.cancel(job_id)


@router.post("/jobs/{job_id}/permission-decisions")
async def permission_decision(job_id: str, body: PermissionDecisionIn, ctx: AppContext = Depends(require_auth_mutating)):
    assert ctx.jobs
    decision = body.decision
    if body.remember and decision == "once":
        decision = "always"
    return await ctx.jobs.permission_decision(job_id, body.request_id, decision)

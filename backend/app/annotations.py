"""Discussions anchored to document passages: threads, messages, reanchoring (DESIGN §5.2, §10, §13)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import anchors as anchoring
from .db import iso, new_id, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found, DOCUMENT_VERSION_CONFLICT, INVALID_ANCHOR
from .models import AgentSession, AnchorHistory, Document, Job, Message, Thread
from .sessions import SessionService
from .textmodel import build_text_model

router = APIRouter()


# ---------------------------------------------------------------------- prompt building

def build_prompt(*, question: str, quote: str, document_path: str, sha256: str, heading: str | None,
                 prior: list[dict[str, str]] | None, versions: list[dict[str, Any]] | None) -> str:
    parts = [
        "You are assisting with a Markdown wiki document in this workspace via the Wiki Agent Workspace reader.",
        f"Document path: {document_path}",
        f"Document content SHA-256: {sha256}",
    ]
    if heading:
        parts.append(f"Nearest heading: {heading}")
    if versions:
        refs = ", ".join(f"{v['sha256'][:12]} ({v.get('git_commit') or 'uncommitted'}{', ' + v['git_branch'] if v.get('git_branch') else ''})" for v in versions[:5])
        parts.append(f"Known earlier recorded versions of this file (content hashes, Git commit if any): {refs}. "
                     "Only Git history and these recorded snapshots are available as history; do not claim to know earlier reasoning that is not in them.")
    parts.append("The user selected this exact passage in the rendered document (verbatim):")
    parts.append("<<<SELECTED_PASSAGE\n" + quote + "\nSELECTED_PASSAGE>>>")
    if prior:
        parts.append("Earlier messages in this discussion thread (for context):")
        parts.extend(f"[{m['role']}] {m['content'][:2000]}" for m in prior[-8:])
    parts.append("User message:")
    parts.append(question)
    parts.append(
        "Instructions: answer about the selected passage and the document. Only modify files if this user message "
        "explicitly asks for a change; in that case edit the document file at the path above in place and summarize what changed. "
        "Do not ask for additional confirmation beyond your own configured permission rules."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------- service


def thread_dict(t: Thread, messages: list[Message], jobs_by_message: dict[str, Job]) -> dict[str, Any]:
    return {
        "id": t.id,
        "document_id": t.document_id,
        "original_anchor": anchoring.Anchor(t.orig_version_sha256, t.orig_quote, t.orig_prefix, t.orig_suffix, t.orig_start, t.orig_end, t.orig_heading).as_dict(),
        "anchor": anchoring.Anchor(t.cur_version_sha256, t.cur_quote, t.cur_prefix, t.cur_suffix, t.cur_start, t.cur_end, t.cur_heading).as_dict(),
        "anchor_state": t.anchor_state,
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

    # ------------------------------------------------------------------ listing
    def list_threads(self, doc_id: str) -> dict[str, Any]:
        doc = self.docs.get_document(doc_id)
        try:
            text, _, sha = self.docs.current_content(doc)
        except ApiError as exc:
            if exc.code != "DOCUMENT_MISSING":
                raise
            text, sha = None, None
        model = build_text_model(text).text if text is not None else None
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
                if model is not None and sha is not None:
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
        text, _, sha = self.docs.current_content(doc)
        if sha != body.version_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "the document changed since you selected the passage; reload and reselect",
                           current_sha256=sha, submitted_sha256=body.version_sha256)
        model = build_text_model(text)
        err = anchoring.validate_offsets(model.text, body.start, body.end, body.quote)
        if err:
            # Offsets did not match the server model; accept if the quote occurs exactly once (client-side
            # model and server-side model can differ in rare renderer edge cases).
            occurrences = model.text.count(body.quote)
            if occurrences != 1:
                raise bad_request(f"invalid anchor: {err}", INVALID_ANCHOR, occurrences=occurrences)
        sess = await self._session_for_new_thread(doc, body.session_id)
        heading = body.heading or model.heading_for(body.start)
        user_msg_id = new_id()
        agent_msg_id = new_id()
        thread_id = new_id()
        with self.ctx.db.write() as s:
            t = Thread(
                id=thread_id, document_id=doc_id,
                orig_version_sha256=sha, orig_quote=body.quote, orig_prefix=body.prefix, orig_suffix=body.suffix,
                orig_start=body.start, orig_end=body.end, orig_heading=heading,
                cur_version_sha256=sha, cur_quote=body.quote, cur_prefix=body.prefix, cur_suffix=body.suffix,
                cur_start=body.start, cur_end=body.end, cur_heading=heading, anchor_state="attached",
                last_session_id=sess.id,
            )
            s.add(t)
            s.flush()
            s.add(Message(id=user_msg_id, thread_id=thread_id, session_id=sess.id, role="user", content=body.question, status="completed"))
            s.add(Message(id=agent_msg_id, thread_id=thread_id, session_id=sess.id, role="agent", content="", status="queued"))
        prompt = build_prompt(question=body.question, quote=body.quote, document_path=doc.path, sha256=sha, heading=heading,
                              prior=None, versions=self.docs.list_versions(doc_id))
        job_id = self.jobs.enqueue_turn(session_id=sess.id, thread_id=thread_id, prompt=prompt,
                                        context={"document_id": doc_id, "document_path": doc.path, "sha256": sha, "quote": body.quote},
                                        agent_message_id=agent_msg_id)
        with self.ctx.db.write() as s:
            m = s.get(Message, agent_msg_id)
            if m:
                m.job_id = job_id
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
        user_msg_id = new_id()
        agent_msg_id = new_id()
        with self.ctx.db.write() as s:
            s.add(Message(id=user_msg_id, thread_id=thread_id, session_id=sess.id, role="user", content=body.text, status="completed"))
            s.add(Message(id=agent_msg_id, thread_id=thread_id, session_id=sess.id, role="agent", content="", status="queued"))
            tt = s.get(Thread, thread_id)
            if tt:
                tt.last_session_id = sess.id
                tt.updated_at = utcnow()
        prompt = build_prompt(question=body.text, quote=t.cur_quote, document_path=doc.path, sha256=sha, heading=t.cur_heading,
                              prior=prior, versions=self.docs.list_versions(doc.id))
        job_id = self.jobs.enqueue_turn(session_id=sess.id, thread_id=thread_id, prompt=prompt,
                                        context={"document_id": doc.id, "document_path": doc.path, "sha256": sha, "quote": t.cur_quote},
                                        agent_message_id=agent_msg_id)
        with self.ctx.db.write() as s:
            m = s.get(Message, agent_msg_id)
            if m:
                m.job_id = job_id
        return {"message_id": user_msg_id, "job_id": job_id, "session_id": sess.id, "agent_message_id": agent_msg_id}

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
        text, _, sha = self.docs.current_content(doc)
        if sha != body.version_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "document changed; reload before reanchoring", current_sha256=sha)
        model = build_text_model(text)
        err = anchoring.validate_offsets(model.text, body.start, body.end, body.quote)
        if err and model.text.count(body.quote) != 1:
            raise bad_request(f"invalid anchor: {err}", INVALID_ANCHOR)
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


class ThreadCreateIn(BaseModel):
    version_sha256: str
    quote: str = Field(min_length=1, max_length=20000)
    prefix: str = Field("", max_length=80)
    suffix: str = Field("", max_length=80)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    heading: str | None = None
    question: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None  # app session id, or "new"


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None


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

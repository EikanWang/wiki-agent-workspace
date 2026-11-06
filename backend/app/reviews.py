"""GitHub-style reviews of a diff: stash comments, submit them together, let the agent triage.

Flow
1. `POST /reviews/comments` stashes a comment against the current pending review for (base, target) — no agent
   run, editable and deletable.
2. `POST /reviews/{id}/submit` turns every comment into a discussion thread (so history is kept the usual way)
   and starts ONE agent job that receives all comments with their diff context. The agent must answer with a
   JSON triage: for each comment `{"id", "decision": "address|reply|skip", "response"}`. For `address` items it
   edits the files (locked paths are read-only and announced as such); it never edits for `reply`/`skip`.
3. When the job finishes, the triage is parsed, stored on each comment, written back into the per-comment
   discussion threads as the agent's answer, and reported in the UI.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .db import iso, new_id, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .diffs import DiffService
from .errors import ApiError, bad_request, conflict, not_found
from .models import Job, Message, Review, ReviewComment
from .sessions import SessionService
from .textmodel import document_format

router = APIRouter()

DECISIONS = ("address", "reply", "skip")


def _comment_dict(c: ReviewComment) -> dict[str, Any]:
    return {
        "id": c.id, "path": c.path, "side": c.side, "line_no": c.line_no, "line_end": c.line_end, "line_text": c.line_text,
        "body": c.body, "thread_id": c.thread_id, "decision": c.decision, "response": c.response, "created_at": iso(c.created_at),
    }


def _review_dict(r: Review, comments: list[ReviewComment], job: Job | None = None) -> dict[str, Any]:
    return {
        "id": r.id, "base": r.base, "target": r.target, "status": r.status, "summary": r.summary, "session_id": r.session_id,
        "job_id": r.job_id, "job_status": job.status if job else None, "job_error": job.error_message if job else None,
        "result": json.loads(r.result_json) if r.result_json else None,
        "comments": [_comment_dict(c) for c in comments], "created_at": iso(r.created_at),
        "submitted_at": iso(r.submitted_at), "finished_at": iso(r.finished_at),
    }


class ReviewService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        assert ctx.documents and ctx.jobs
        self.docs = ctx.documents
        self.jobs = ctx.jobs

    # ------------------------------------------------------------------ pending review
    def pending(self, base: str, target: str, create: bool = False) -> Review | None:
        with self.ctx.db.write() as s:
            r = s.query(Review).filter_by(workspace_id=self.docs.workspace_id, base=base, target=target, status="pending").one_or_none()
            if r is None and create:
                r = Review(id=new_id(), workspace_id=self.docs.workspace_id, base=base, target=target, status="pending")
                s.add(r)
                s.flush()
            return r

    def get(self, review_id: str) -> dict[str, Any]:
        with self.ctx.db.read() as s:
            r = s.get(Review, review_id)
            if r is None:
                raise not_found("review not found", "REVIEW_NOT_FOUND")
            comments = s.query(ReviewComment).filter_by(review_id=review_id).order_by(ReviewComment.path, ReviewComment.line_no, ReviewComment.created_at).all()
            job = s.get(Job, r.job_id) if r.job_id else None
            return _review_dict(r, comments, job)

    def current(self, base: str, target: str) -> dict[str, Any]:
        """The pending review for this diff (possibly empty) plus the most recent finished one."""
        pending = self.pending(base, target)
        with self.ctx.db.read() as s:
            last = (s.query(Review).filter(Review.workspace_id == self.docs.workspace_id, Review.base == base, Review.target == target,
                                           Review.status != "pending").order_by(Review.created_at.desc()).first())
        return {
            "pending": self.get(pending.id) if pending else {"id": None, "base": base, "target": target, "status": "pending", "comments": []},
            "last": self.get(last.id) if last else None,
        }

    def add_comment(self, body: "ReviewCommentIn") -> dict[str, Any]:
        rel = self.docs.normalize_rel(body.path)
        if document_format(rel.rsplit("/", 1)[-1]) not in ("markdown", "text", "docx", "pptx"):
            raise bad_request("comments can only be attached to files the reader can open", "UNSUPPORTED_FORMAT")
        r = self.pending(body.base, body.target, create=True)
        assert r is not None
        with self.ctx.db.write() as s:
            c = ReviewComment(id=new_id(), review_id=r.id, path=rel, side=body.side, line_no=body.line_no, line_end=body.line_end,
                              line_text=body.line_text, hunk=(body.hunk or "")[:20000] or None, body=body.body)
            s.add(c)
            s.flush()
            out = _comment_dict(c)
        out["review_id"] = r.id
        return out

    def update_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        with self.ctx.db.write() as s:
            c = s.get(ReviewComment, comment_id)
            if c is None:
                raise not_found("comment not found", "COMMENT_NOT_FOUND")
            r = s.get(Review, c.review_id)
            if r is None or r.status != "pending":
                raise conflict("REVIEW_NOT_PENDING", "this review was already submitted; reply in its discussion instead")
            c.body = text
            return _comment_dict(c)

    def delete_comment(self, comment_id: str) -> dict[str, Any]:
        with self.ctx.db.write() as s:
            c = s.get(ReviewComment, comment_id)
            if c is None:
                raise not_found("comment not found", "COMMENT_NOT_FOUND")
            r = s.get(Review, c.review_id)
            if r is None or r.status != "pending":
                raise conflict("REVIEW_NOT_PENDING", "this review was already submitted")
            s.delete(c)
            return {"id": comment_id, "deleted": True}

    def discard(self, review_id: str) -> dict[str, Any]:
        with self.ctx.db.write() as s:
            r = s.get(Review, review_id)
            if r is None:
                raise not_found("review not found", "REVIEW_NOT_FOUND")
            if r.status != "pending":
                raise conflict("REVIEW_NOT_PENDING", "only pending reviews can be discarded")
            n = s.query(ReviewComment).filter_by(review_id=review_id).delete(synchronize_session=False)
            s.delete(r)
            return {"id": review_id, "discarded_comments": n}

    # ------------------------------------------------------------------ submit
    async def submit(self, review_id: str, body: "SubmitIn") -> dict[str, Any]:
        with self.ctx.db.read() as s:
            r = s.get(Review, review_id)
            if r is None:
                raise not_found("review not found", "REVIEW_NOT_FOUND")
            if r.status != "pending":
                raise conflict("REVIEW_NOT_PENDING", "this review was already submitted")
            comments = s.query(ReviewComment).filter_by(review_id=review_id).order_by(ReviewComment.path, ReviewComment.line_no).all()
        if not comments and not (body.summary or "").strip():
            raise bad_request("add at least one comment or a summary before submitting", "EMPTY_REVIEW")

        # One discussion thread per comment (anchored to the commented text), so each keeps its own history.
        diffs = DiffService(self.ctx)
        from .diffs import DiffCommentIn

        thread_by_comment: dict[str, dict[str, Any]] = {}
        for c in comments:
            created = await diffs.comment(DiffCommentIn(
                path=c.path, base=r.base, target=r.target, side=c.side, line_no=c.line_no, line_end=c.line_end, line_text=c.line_text,
                hunk=c.hunk, question=c.body, session_id=body.session_id, defer_agent=True,
            ))
            thread_by_comment[c.id] = created
        session_id = body.session_id
        if not session_id:
            if thread_by_comment:
                session_id = next(iter(thread_by_comment.values()))["session_id"]
            else:
                svc = SessionService(self.ctx)
                provider = self.ctx.providers.default_provider() if self.ctx.providers else None
                if not provider:
                    raise ApiError(503, "PROVIDER_UNAVAILABLE", "no default agent provider configured")
                session_id = (await svc.create_for_provider(provider, "Review")).id

        locks = self.ctx.locks
        prompt = self._build_prompt(r, comments, body.summary, locks)
        context = {"review_id": review_id, "comment_ids": [c.id for c in comments], "paths": sorted({c.path for c in comments})}
        job_id = self.jobs.enqueue_turn(session_id=session_id, thread_id=None, prompt=prompt, context=context, agent_message_id=None,
                                        kind="review", on_complete=self._make_on_complete(review_id))
        with self.ctx.db.write() as s:
            rr = s.get(Review, review_id)
            assert rr is not None
            rr.status = "submitted"
            rr.summary = (body.summary or "").strip() or None
            rr.session_id = session_id
            rr.job_id = job_id
            rr.submitted_at = utcnow()
            for c in comments:
                cc = s.get(ReviewComment, c.id)
                if cc is not None and c.id in thread_by_comment:
                    cc.thread_id = thread_by_comment[c.id]["thread_id"]
        return {"review_id": review_id, "job_id": job_id, "session_id": session_id, "comments": len(comments)}

    def _build_prompt(self, r: Review, comments: list[ReviewComment], summary: str | None, locks) -> str:
        parts = [
            "You are receiving a REVIEW of changes in this workspace (like a GitHub pull-request review). Each comment below "
            "refers to specific lines of a diff. Work through ALL comments and decide for each one:",
            "  - \"address\": the comment asks for a change you agree with -> make that change in the file now;",
            "  - \"reply\": the comment is a question, or you disagree / need to explain -> answer in `response`, change nothing;",
            "  - \"skip\": the comment needs no action (e.g. already handled by another comment) -> say why briefly.",
            "Only edit files for `address` decisions, only the lines the comment is about, and keep the author's style.",
        ]
        if locks is not None:
            locked = sorted({c.path for c in comments if locks.covering(c.path)})
            if locked:
                parts.append("These files are LOCKED by the user and must NOT be modified (use `reply` instead of `address`): " + ", ".join(locked))
        if summary:
            parts.append("Overall review note from the reviewer:\n" + summary.strip())
        parts.append(f"Diff being reviewed: base={r.base}, target={r.target}.")
        for i, c in enumerate(comments, 1):
            where = f"lines {c.line_no}-{c.line_end}" if c.line_end and c.line_no and c.line_end != c.line_no else f"line {c.line_no or '?'}"
            kind = {"deleted": "removed", "added": "added", "context": "unchanged", "mixed": "changed"}.get(c.side, c.side)
            parts.append(f"--- Comment {i} (id {c.id}) on {c.path}, {kind} {where}:\n"
                         + "\n".join("    " + ln for ln in c.line_text.splitlines()[:40])
                         + "\nReviewer says: " + c.body.strip()
                         + (("\nHunk:\n```diff\n" + c.hunk[:3000] + "\n```") if c.hunk else ""))
        parts.append(
            "After making any edits, output your triage as the LAST thing in your reply, as a fenced JSON block exactly like:\n"
            "```json\n{\"comments\": [{\"id\": \"<comment id>\", \"decision\": \"address|reply|skip\", \"response\": \"<one or two sentences>\"}], "
            "\"summary\": \"<one paragraph>\"}\n```"
        )
        return "\n\n".join(parts)

    def _make_on_complete(self, review_id: str):
        async def on_complete(job_id: str) -> None:
            with self.ctx.db.read() as s:
                job = s.get(Job, job_id)
                r = s.get(Review, review_id)
                comments = s.query(ReviewComment).filter_by(review_id=review_id).all()
                text = ""
                if job and job.status == "completed":
                    # the final text is in the last `completed` event payload
                    from .models import JobEvent

                    ev = s.query(JobEvent).filter_by(job_id=job_id, type="completed").order_by(JobEvent.seq.desc()).first()
                    text = (json.loads(ev.payload_json).get("text") or "") if ev else ""
            if r is None:
                return
            triage = _parse_triage(text) if text else None
            status = "completed" if job and job.status == "completed" else ("cancelled" if job and job.status == "cancelled" else "failed")
            with self.ctx.db.write() as s:
                rr = s.get(Review, review_id)
                if rr is None:
                    return
                rr.status = status
                rr.finished_at = utcnow()
                rr.result_json = json.dumps(triage or {"raw": text[:20000], "parsed": False}, ensure_ascii=False)
                by_id = {c["id"]: c for c in (triage or {}).get("comments", []) if isinstance(c, dict) and c.get("id")}
                for c in comments:
                    cc = s.get(ReviewComment, c.id)
                    if cc is None:
                        continue
                    item = by_id.get(c.id)
                    if item:
                        d = str(item.get("decision", "")).lower()
                        cc.decision = d if d in DECISIONS else "reply"
                        cc.response = str(item.get("response") or "").strip() or None
                    elif status == "completed":
                        cc.decision = "reply"
                        cc.response = triage.get("summary") if triage else (text[:2000] or None)
                    else:
                        cc.decision = None
                        cc.response = (job.error_message if job else None) or f"review {status}"
                    # write the verdict into the comment's discussion thread as the agent's answer
                    if cc.thread_id:
                        agent_msgs = s.query(Message).filter_by(thread_id=cc.thread_id, role="agent").order_by(Message.created_at.desc()).all()
                        label = {"address": "Addressed", "reply": "Reply", "skip": "Skipped"}.get(cc.decision or "", "Review")
                        content = f"**{label}.** {cc.response or ''}".strip()
                        if agent_msgs:
                            agent_msgs[0].content = content
                            agent_msgs[0].status = "completed" if status == "completed" else status
                            agent_msgs[0].job_id = job_id
                        else:
                            s.add(Message(id=new_id(), thread_id=cc.thread_id, session_id=rr.session_id, role="agent", content=content,
                                          status="completed" if status == "completed" else status, job_id=job_id))
            if self.ctx.events:
                self.ctx.events.publish("global", {"type": "review_finished", "review_id": review_id, "status": status})
                self.ctx.events.publish("global", {"type": "tree_changed", "workspace_id": self.ctx.workspace_id})

        return on_complete


def _parse_triage(text: str) -> dict[str, Any] | None:
    """Find the last fenced JSON block (or bare JSON object) in the agent's reply."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = blocks[::-1] if blocks else []
    if not candidates:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            candidates = [m.group(0)]
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("comments"), list):
            return data
    return None


# ---------------------------------------------------------------------- schemas


class ReviewCommentIn(BaseModel):
    path: str
    base: str = "HEAD"
    target: str = "worktree"
    side: str = Field("added", pattern="^(added|deleted|context|mixed)$")
    line_no: int | None = None
    line_end: int | None = None
    line_text: str = Field("", max_length=20000)
    hunk: str | None = Field(None, max_length=20000)
    body: str = Field(min_length=1, max_length=20000)


class CommentUpdateIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


class SubmitIn(BaseModel):
    summary: str | None = Field(None, max_length=20000)
    session_id: str | None = None


# ---------------------------------------------------------------------- routes


@router.get("/reviews/current")
def current_review(base: str = Query("HEAD"), target: str = Query("worktree"), ctx: AppContext = Depends(require_auth)):
    return ReviewService(ctx).current(base, target)


@router.get("/reviews/{review_id}")
def get_review(review_id: str, ctx: AppContext = Depends(require_auth)):
    return ReviewService(ctx).get(review_id)


@router.post("/reviews/comments", status_code=201)
def add_review_comment(body: ReviewCommentIn, ctx: AppContext = Depends(require_auth_mutating)):
    return ReviewService(ctx).add_comment(body)


@router.put("/reviews/comments/{comment_id}")
def update_review_comment(comment_id: str, body: CommentUpdateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return ReviewService(ctx).update_comment(comment_id, body.body)


@router.delete("/reviews/comments/{comment_id}")
def delete_review_comment(comment_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return ReviewService(ctx).delete_comment(comment_id)


@router.post("/reviews/{review_id}/submit", status_code=202)
async def submit_review(review_id: str, body: SubmitIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await ReviewService(ctx).submit(review_id, body)


@router.delete("/reviews/{review_id}")
def discard_review(review_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return ReviewService(ctx).discard(review_id)

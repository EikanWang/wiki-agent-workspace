"""Diff review: unified diffs for a file or a whole folder, and comments anchored to diff lines.

Bases: `HEAD` or any valid Git ref (committed state), or an app snapshot id/sha for a single file.
Target: the working tree (default) or another ref. Untracked Markdown files appear as whole-file additions
so new drafts can be reviewed too. All Git access is read-only.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .annotations import ThreadService, build_prompt
from .db import new_id
from .deps import AppContext, require_auth, require_auth_mutating
from .documents import MARKDOWN_SUFFIXES, DocumentService, sha256_bytes
from .kv import extra_context
from .errors import ApiError, bad_request, not_found
from .gitinfo import git_info, git_run, valid_ref
from .models import Document, FileReview, Message, Thread
from .textmodel import NO_TEXT_FORMATS, document_format

router = APIRouter()

MAX_FILES = 400
MAX_DIFF_BYTES = 4 * 1024 * 1024


class DiffService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        assert ctx.documents
        self.docs: DocumentService = ctx.documents
        self.root = self.docs.root

    # ------------------------------------------------------------------ helpers
    def _rel(self, path: str) -> str:
        return self.docs.normalize_rel(path) if path else ""

    @staticmethod
    def _as_text(rel: str, data: bytes) -> str:
        """Text to diff: Office documents are compared by their rendered text (one line per block: slide titles,
        bullets, table rows, notes), everything else by its UTF-8 content."""
        fmt = document_format(Path(rel).name)
        if fmt in NO_TEXT_FORMATS:
            # binary layouts (.doc, .vsd, PDF…): no readable text to compare; the file list still shows the change
            return f"<{Path(rel).suffix or 'binary'} document: compare it in the reader's Layout view>\n"
        if fmt in ("docx", "pptx", "visio"):
            try:
                from .textmodel import build_text_model_from_html, render_document

                return build_text_model_from_html(render_document(fmt, data, Path(rel).name)).text
            except Exception:
                return "<could not read this file>\n"
        return data.decode("utf-8", "replace")

    def _text_at_ref(self, ref: str, rel: str) -> str | None:
        code, out, _ = git_run(self.root, ["show", f"{ref}:{rel}"])
        if code != 0:
            return None
        return self._as_text(rel, out)

    def _text_in_worktree(self, rel: str) -> str | None:
        p = self.root / rel
        if not p.is_file():
            return None
        try:
            return self._as_text(rel, p.read_bytes())
        except OSError:
            return None

    @staticmethod
    def _lines(text: str) -> list[str]:
        """Split at "\n" only, like Git, GitHub and editors (str.splitlines also splits at form feeds, \x1c-\x1e,
        \u2028…, which shifted every later line number)."""
        if not text:
            return []
        parts = text.split("\n")
        out = [p + "\n" for p in parts[:-1]]
        if parts[-1]:
            out.append(parts[-1])  # last line without a newline
        return out

    def _unified(self, a: str | None, b: str | None, rel: str, a_label: str, b_label: str) -> str:
        lines = difflib.unified_diff(
            self._lines(a or ""), self._lines(b or ""),
            fromfile=f"a/{rel} ({a_label})" if a is not None else "/dev/null",
            tofile=f"b/{rel} ({b_label})" if b is not None else "/dev/null", n=3,
        )
        # difflib leaves a last line without "\n" unterminated, gluing it to the next diff line ("-old+new");
        # write it like Git does
        return "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in lines)

    def _changed_files(self, base: str, target: str | None, rel_dir: str, markdown_only: bool) -> list[dict[str, Any]]:
        """Files that differ between base and target (worktree when None), with Git status letters."""
        pathspec = [rel_dir] if rel_dir else []
        args = ["diff", "--name-status", "-z", "--no-renames", base] + ([target] if target else []) + ["--"] + pathspec
        code, out, err = git_run(self.root, args)
        if code != 0:
            raise ApiError(400, "GIT_ERROR", f"git diff failed: {err[:300]}")
        files: list[dict[str, Any]] = []
        parts = out.split(b"\0")
        i = 0
        while i + 1 < len(parts):
            status, rel = parts[i].decode("utf-8", "replace"), parts[i + 1].decode("utf-8", "replace")
            i += 2
            if not rel:
                continue
            files.append({"path": rel, "status": {"A": "added", "D": "deleted", "M": "modified"}.get(status[:1], status)})
        if target is None:
            # untracked files are not part of `git diff`; include them as additions
            code, out, _ = git_run(self.root, ["ls-files", "--others", "--exclude-standard", "-z", "--"] + pathspec)
            if code == 0:
                for raw in out.split(b"\0"):
                    rel = raw.decode("utf-8", "replace")
                    if rel and (self.root / rel).is_file():
                        files.append({"path": rel, "status": "untracked"})
        if markdown_only:
            files = [f for f in files if Path(f["path"]).suffix.lower() in MARKDOWN_SUFFIXES]
        files.sort(key=lambda f: f["path"])
        return files

    # ------------------------------------------------------------------ public
    def diff(self, path: str, base: str, target: str | None, markdown_only: bool) -> dict[str, Any]:
        rel = self._rel(path)
        info = git_info(self.root)
        if not info.is_repo:
            raise ApiError(409, "NOT_A_GIT_REPO", "the workspace is not a Git repository; folder diffs need Git history")
        full = self.root / rel if rel else self.root
        if rel:
            self.docs.resolve(rel)  # traversal/symlink checks
        if not valid_ref(self.root, base):
            raise bad_request(f"unknown base ref: {base}", "REF_NOT_FOUND")
        if target is not None and not valid_ref(self.root, target):
            raise bad_request(f"unknown target ref: {target}", "REF_NOT_FOUND")
        is_dir = full.is_dir()
        if is_dir:
            files = self._changed_files(base, target, rel, markdown_only)
        else:
            files = [{"path": rel, "status": "modified"}]
            if target is None:
                st = info.statuses.get(rel, "clean")
                files[0]["status"] = {"untracked": "untracked", "deleted": "deleted"}.get(st, "modified")
        if len(files) > MAX_FILES:
            raise ApiError(413, "DIFF_TOO_LARGE", f"{len(files)} changed files; narrow the folder (limit {MAX_FILES})")
        total = 0
        out_files: list[dict[str, Any]] = []
        with self.ctx.db.read() as s:
            doc_ids = {d.path: d.id for d in s.query(Document).filter_by(workspace_id=self.docs.workspace_id).all()}
        for f in files:
            frel = f["path"]
            a = None if f["status"] in ("untracked", "added") else self._text_at_ref(base, frel)
            b = self._text_at_ref(target, frel) if target else self._text_in_worktree(frel)
            if f["status"] == "deleted":
                b = None
            if a is None and b is None:
                continue
            unified = self._unified(a, b, frel, base, target or "worktree")
            total += len(unified)
            if total > MAX_DIFF_BYTES:
                raise ApiError(413, "DIFF_TOO_LARGE", "diff output exceeds 4 MiB; narrow the folder or review file by file")
            body = unified.split("\n")[2:]  # after the "---" / "+++" file headers ("----" inside is a deleted "---")
            adds = sum(1 for ln in body if ln.startswith("+"))
            dels = sum(1 for ln in body if ln.startswith("-"))
            out_files.append({
                "path": frel, "status": f["status"], "unified": unified, "additions": adds, "deletions": dels,
                "identical": a == b, "document_id": doc_ids.get(frel),
                "target_sha256": sha256_bytes(b.encode("utf-8")) if b is not None else None,
                "is_markdown": Path(frel).suffix.lower() in MARKDOWN_SUFFIXES,
                "commentable": document_format(Path(frel).name) in ("markdown", "text", "docx", "pptx", "visio"),
                "lock": self.ctx.locks.covering(frel) if self.ctx.locks is not None else None,
            })
        reviews = self.reviews_for(base, [f["path"] for f in out_files])
        for f in out_files:
            rv = reviews.get(f["path"])
            f["viewed"] = bool(rv) and rv["reviewed_sha256"] == f["target_sha256"]
            f["viewed_stale"] = bool(rv) and not f["viewed"]  # was viewed, but the file changed since
        viewed_count = sum(1 for f in out_files if f["viewed"])
        return {
            "path": rel, "is_dir": is_dir, "base": base, "target": target or "worktree",
            "git": self.docs.git_summary(info), "files": out_files, "viewed_count": viewed_count,
            "summary": {"files": len(out_files), "additions": sum(f["additions"] for f in out_files), "deletions": sum(f["deletions"] for f in out_files)},
        }

    # ------------------------------------------------------------------ viewed marks
    def reviews_for(self, base: str, paths: list[str]) -> dict[str, dict[str, Any]]:
        if not paths:
            return {}
        with self.ctx.db.read() as s:
            rows = s.query(FileReview).filter(FileReview.workspace_id == self.docs.workspace_id, FileReview.base == base,
                                              FileReview.path.in_(paths)).all()
            return {r.path: {"reviewed_sha256": r.reviewed_sha256, "created_at": r.created_at.isoformat()} for r in rows}

    def set_viewed(self, path: str, base: str, viewed: bool, target_sha256: str | None) -> dict[str, Any]:
        rel = self._rel(path)
        if not valid_ref(self.root, base):
            raise bad_request(f"unknown base ref: {base}", "REF_NOT_FOUND")
        with self.ctx.db.write() as s:
            row = s.query(FileReview).filter_by(workspace_id=self.docs.workspace_id, path=rel, base=base).one_or_none()
            if not viewed:
                if row is not None:
                    s.delete(row)
                return {"path": rel, "base": base, "viewed": False}
            if not target_sha256:
                # compute from the worktree when the client did not send the hash it reviewed
                text = self._text_in_worktree(rel)
                if text is None:
                    raise not_found("file not found in the working tree", "FILE_NOT_FOUND")
                target_sha256 = sha256_bytes(text.encode("utf-8"))
            if row is None:
                row = FileReview(id=new_id(), workspace_id=self.docs.workspace_id, path=rel, base=base, reviewed_sha256=target_sha256)
                s.add(row)
            else:
                row.reviewed_sha256 = target_sha256
            return {"path": rel, "base": base, "viewed": True, "reviewed_sha256": target_sha256}

    # ------------------------------------------------------------------ comments
    async def comment(self, body: "DiffCommentIn") -> dict[str, Any]:
        """A comment on a diff line becomes a normal discussion on the target document, so the agent can answer
        with the same workflow (session, streaming, retry). The anchor is the commented line's text when it exists
        in the current file; otherwise the thread records the diff context only."""
        rel = self._rel(body.path)
        if document_format(Path(rel).name) not in ("markdown", "text", "docx", "pptx", "visio"):
            raise bad_request("comments can only be attached to files the reader can open (Markdown, text/source, .docx, .pptx, .vsdx)", "UNSUPPORTED_FORMAT")
        doc = self.docs.get_or_create_document(rel)
        try:
            _, data, sha = self.docs.current_content(doc)
        except ApiError as exc:
            if exc.code != "DOCUMENT_MISSING":
                raise
            raise ApiError(409, "DOCUMENT_MISSING", "the file no longer exists in the worktree; comments need a current file")
        self.docs.record_version(doc.id, data, sha)
        svc = ThreadService(self.ctx)
        model, _ = svc._model(doc)
        line_text = body.line_text.strip()
        start = end = 0
        quote = line_text
        anchored = False
        whole = body.side == "file"  # a question about all of this file's changes (no line): a whole-document thread
        if whole:
            quote, line_text = rel, ""
        if line_text and body.side != "deleted":
            # Diff lines are Markdown *source*; the anchor model is *rendered* text. For a multi-line range the
            # rendered lines are usually contiguous in the model (one block per source line); try the joined
            # range first, then the range with markers stripped, then its first non-empty line.
            src_lines = [ln for ln in line_text.split("\n")]
            candidates: list[str] = []
            if len(src_lines) > 1:
                stripped = [_rendered_candidates(ln)[-1] for ln in src_lines if ln.strip()]
                candidates.append("\n".join(stripped))
                # Markdown joins consecutive paragraph lines with a space (soft break) in rendered text
                candidates.append(" ".join(stripped))
                first = next((ln for ln in src_lines if ln.strip()), "")
                last = next((ln for ln in reversed(src_lines) if ln.strip()), "")
                candidates.extend(_rendered_candidates(first))
                candidates.extend(_rendered_candidates(last))
            else:
                candidates.extend(_rendered_candidates(line_text))
            for cand in candidates:
                if not cand:
                    continue
                idx = model.text.find(cand)
                if idx >= 0 and model.text.find(cand, idx + 1) < 0:
                    start, end = idx, idx + len(cand)
                    quote = cand
                    anchored = True
                    break
            # Range anchors: if only the first and last lines were found individually, span them.
            if not anchored and len(src_lines) > 1:
                f = _rendered_candidates(next((ln for ln in src_lines if ln.strip()), ""))[-1]
                l = _rendered_candidates(next((ln for ln in reversed(src_lines) if ln.strip()), ""))[-1]
                fi = model.text.find(f) if f else -1
                li = model.text.find(l, fi + 1) if l and fi >= 0 else -1
                if fi >= 0 and li >= 0 and model.text.find(f, fi + 1) < 0 and model.text.find(l, li + 1) < 0:
                    start, end = fi, li + len(l)
                    quote = model.text[start:end]
                    anchored = True
        if not anchored and not whole:
            # Fall back to the first non-empty rendered line so the thread still has a valid anchor.
            first = next((ln for ln in model.text.split("\n") if ln.strip()), None)
            if first is None:
                raise ApiError(409, "EMPTY_DOCUMENT", "the document renders to no text; cannot attach a comment")
            start = model.text.index(first)
            end = start + len(first)
            quote = first
        prefix = "" if whole else model.text[max(0, start - 40):start]
        suffix = "" if whole else model.text[end:end + 40]
        heading = model.heading_for(start)
        sess = await svc._session_for_new_thread(doc, body.session_id)
        diff_ctx = {"base": body.base, "target": body.target, "side": body.side, "line_no": body.line_no, "line_end": body.line_end,
                    "line_text": body.line_text, "hunk": body.hunk[:4000] if body.hunk else None, "anchored": anchored or whole}
        question = body.question
        where = f"lines {body.line_no}-{body.line_end}" if body.line_end and body.line_no and body.line_end != body.line_no else f"line {body.line_no or '?'}"
        kind = {"deleted": "removed", "added": "added", "context": "unchanged", "mixed": "changed"}.get(body.side, body.side)
        if whole:
            prompt_question = f"[Question about all the changes of {rel} in a diff ({body.base} → {body.target})]\n\n" + question
            if body.hunk:
                prompt_question += "\n\nThe file's diff:\n```diff\n" + body.hunk[:20000] + "\n```"
        else:
            prompt_question = (
                f"[Comment made while reviewing a diff of {rel} ({body.base} → {body.target}); commented {kind} {where}:\n"
                + "\n".join("    " + ln for ln in body.line_text.strip().splitlines()[:40]) + "]\n\n" + question
            )
            if body.hunk:
                prompt_question += "\n\nDiff hunk for reference:\n```diff\n" + body.hunk[:4000] + "\n```"
        thread_id, user_msg_id, agent_msg_id = new_id(), new_id(), new_id()
        if body.defer_agent:
            with self.ctx.db.write() as s:
                t = Thread(
                    id=thread_id, document_id=doc.id,
                    orig_version_sha256=sha, orig_quote=quote, orig_prefix=prefix, orig_suffix=suffix, orig_start=start, orig_end=end, orig_heading=heading,
                    cur_version_sha256=sha, cur_quote=quote, cur_prefix=prefix, cur_suffix=suffix, cur_start=start, cur_end=end, cur_heading=heading,
                    anchor_state="document" if whole else ("attached" if anchored else "orphaned"), diff_json=json.dumps(diff_ctx, ensure_ascii=False), last_session_id=sess.id,
                )
                s.add(t)
                s.flush()
                s.add(Message(id=user_msg_id, thread_id=thread_id, session_id=sess.id, role="user", content=question, status="completed"))
                s.add(Message(id=agent_msg_id, thread_id=thread_id, session_id=sess.id, role="agent", content="", status="queued"))
            return {"thread_id": thread_id, "job_id": None, "session_id": sess.id, "document_id": doc.id, "anchored": anchored, "agent_message_id": agent_msg_id}
        with self.ctx.db.write() as s:
            t = Thread(
                id=thread_id, document_id=doc.id,
                orig_version_sha256=sha, orig_quote=quote, orig_prefix=prefix, orig_suffix=suffix, orig_start=start, orig_end=end, orig_heading=heading,
                cur_version_sha256=sha, cur_quote=quote, cur_prefix=prefix, cur_suffix=suffix, cur_start=start, cur_end=end, cur_heading=heading,
                anchor_state="document" if whole else ("attached" if anchored else "orphaned"), diff_json=json.dumps(diff_ctx, ensure_ascii=False), last_session_id=sess.id,
            )
            s.add(t)
            s.flush()
            s.add(Message(id=user_msg_id, thread_id=thread_id, session_id=sess.id, role="user", content=question, status="completed"))
            s.add(Message(id=agent_msg_id, thread_id=thread_id, session_id=sess.id, role="agent", content="", status="queued"))
        prompt = build_prompt(question=prompt_question, quote=quote, document_path=rel, sha256=sha, heading=heading, prior=None,
                              versions=self.docs.list_versions(doc.id), extra=extra_context(self.ctx, rel, self.docs.workspace_id))
        assert self.ctx.jobs
        job_id = self.ctx.jobs.enqueue_turn(session_id=sess.id, thread_id=thread_id, prompt=prompt,
                                            context={"document_id": doc.id, "document_path": rel, "sha256": sha, "quote": quote, "diff": diff_ctx},
                                            agent_message_id=agent_msg_id)
        with self.ctx.db.write() as s:
            m = s.get(Message, agent_msg_id)
            if m:
                m.job_id = job_id
        return {"thread_id": thread_id, "job_id": job_id, "session_id": sess.id, "document_id": doc.id, "anchored": anchored}

    def comments_for(self, paths: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Diff comments (threads with diff_json) per path, for showing markers in the diff view."""
        with self.ctx.db.read() as s:
            docs = {d.id: d.path for d in s.query(Document).filter(Document.workspace_id == self.docs.workspace_id, Document.path.in_(paths)).all()}
            if not docs:
                return {}
            threads = s.query(Thread).filter(Thread.document_id.in_(list(docs)), Thread.diff_json.isnot(None)).order_by(Thread.created_at).all()
            out: dict[str, list[dict[str, Any]]] = {}
            for t in threads:
                msgs = s.query(Message).filter_by(thread_id=t.id).order_by(Message.created_at).all()
                first = msgs[0] if msgs else None
                last = msgs[-1] if msgs else None
                out.setdefault(docs[t.document_id], []).append({
                    "thread_id": t.id, "diff": json.loads(t.diff_json or "{}"), "question": first.content if first else "",
                    "last_status": last.status if last else None, "message_count": len(msgs),
                    "last_session_id": t.last_session_id,
                    # the whole mini-thread so the diff view can show answers inline without leaving the review
                    "messages": [{"id": m.id, "role": m.role, "content": m.content, "status": m.status, "job_id": m.job_id,
                                  "created_at": m.created_at.isoformat() if m.created_at else None} for m in msgs],
                })
            return out


def _rendered_candidates(line: str) -> list[str]:
    """Plausible rendered forms of a Markdown source line, most specific first."""
    import re

    out: list[str] = [line]
    stripped = re.sub(r"^(\s*([-*+]|\d+[.)])\s+|\s*#{1,6}\s+|\s*>\s*)+", "", line).strip()
    stripped = re.sub(r"^\s*[-*+]\s+\[[ xX]\]\s+", "", stripped)  # task list
    if stripped and stripped not in out:
        out.append(stripped)
    plain = re.sub(r"(\*\*|__|\*|_|`|~~)", "", stripped)
    plain = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", plain).strip()
    if plain and plain not in out:
        out.append(plain)
    return out


class DiffCommentIn(BaseModel):
    path: str
    base: str = "HEAD"
    target: str = "worktree"
    side: str = Field("added", pattern="^(added|deleted|context|mixed|file)$")  # file: about the file's changes as a whole
    line_no: int | None = None          # first line of the range (new-side number when it exists, else old-side)
    line_end: int | None = None         # last line of the range (inclusive); None = single line
    line_text: str = Field("", max_length=20000)   # the selected lines joined with "\n" (without +/- markers)
    hunk: str | None = Field(None, max_length=20000)
    question: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None
    # Reviews: create the thread and the user's comment but do not start an agent turn (one review job answers all).
    defer_agent: bool = False


@router.get("/diff")
def get_diff(path: str = Query(""), base: str = Query("HEAD"), target: str | None = Query(None), markdown_only: bool = Query(True),
             ctx: AppContext = Depends(require_auth)):
    svc = DiffService(ctx)
    result = svc.diff(path, base, target, markdown_only)
    result["comments"] = svc.comments_for([f["path"] for f in result["files"]])
    return result


@router.post("/diff/comments", status_code=201)
async def post_diff_comment(body: DiffCommentIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await DiffService(ctx).comment(body)


class ViewedIn(BaseModel):
    path: str
    base: str = "HEAD"
    viewed: bool
    target_sha256: str | None = None  # hash of the content that was reviewed (from GET /diff); protects against races


@router.put("/diff/viewed")
def put_viewed(body: ViewedIn, ctx: AppContext = Depends(require_auth_mutating)):
    return DiffService(ctx).set_viewed(body.path, body.base, body.viewed, body.target_sha256)


@router.get("/refs")
def list_refs(ctx: AppContext = Depends(require_auth)):
    """Recent commits and branches to pick a diff base from."""
    assert ctx.documents
    root = ctx.documents.root
    code, out, _ = git_run(root, ["log", "--max-count=50", "--format=%H%x00%h%x00%s%x00%cI", "--"])
    commits = []
    if code == 0:
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split("\0")
            if len(parts) == 4:
                commits.append({"sha": parts[0], "short": parts[1], "subject": parts[2], "date": parts[3]})
    code, out, _ = git_run(root, ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"])
    branches = out.decode("utf-8", "replace").split() if code == 0 else []
    return {"commits": commits, "branches": branches}

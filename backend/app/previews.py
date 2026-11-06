"""Optional preview-before-apply mode (DESIGN §14).

The proposal runs in an isolated temporary copy of the workspace; the user's worktree is
not touched until Apply, which checks the exact current SHA-256 against the preview base.
"""

from __future__ import annotations

import asyncio
import difflib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .db import iso, new_id, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .documents import sha256_bytes
from .errors import ApiError, conflict, not_found, DOCUMENT_VERSION_CONFLICT, PREVIEW_UNAVAILABLE
from .models import Document, Job, Preview
from .sessions import SessionService

router = APIRouter()

COPY_IGNORE = {".git", "node_modules", ".venv", "venv", "__pycache__", ".codex", ".opencode", ".waw-preview"}
MAX_COPY_BYTES = 512 * 1024 * 1024


def _copy_workspace(root: Path, dest: Path) -> None:
    total = 0

    def ignore(dirpath: str, names: list[str]) -> set[str]:
        nonlocal total
        skip = {n for n in names if n in COPY_IGNORE}
        for n in names:
            if n in skip:
                continue
            p = Path(dirpath) / n
            if p.is_symlink():
                skip.add(n)
                continue
            if p.is_file():
                total += p.stat().st_size
                if total > MAX_COPY_BYTES:
                    raise ApiError(409, PREVIEW_UNAVAILABLE, "workspace is too large to copy for an isolated preview; use the direct-edit flow instead")
        return skip

    shutil.copytree(root, dest, ignore=ignore, symlinks=False, dirs_exist_ok=True)


class PreviewService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        assert ctx.documents and ctx.jobs and ctx.providers
        self.docs = ctx.documents
        self.jobs = ctx.jobs
        self.sessions = SessionService(ctx)

    async def create(self, doc_id: str, body: "PreviewCreateIn") -> dict[str, Any]:
        doc = self.docs.get_document(doc_id)
        _, _, sha = self.docs.current_content(doc)
        if sha != body.base_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "document changed; reload before requesting a preview", current_sha256=sha)
        assert self.ctx.providers
        if body.session_id:
            base_sess = self.sessions.get(body.session_id)
            provider = base_sess.provider
        else:
            provider = self.ctx.providers.default_provider()
            if not provider:
                raise ApiError(503, "PROVIDER_UNAVAILABLE", "no default agent provider configured")
        caps = await self.ctx.providers.capabilities(provider)
        if not caps.available:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", caps.error or f"{provider} unavailable")
        if caps.preview_isolation != "copy":
            raise ApiError(409, PREVIEW_UNAVAILABLE, f"{provider} cannot run isolated previews in this mode; use the direct-edit flow")

        preview_root = Path(self.ctx.settings.state_dir) / "previews"
        preview_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmpdir = Path(tempfile.mkdtemp(prefix="preview-", dir=preview_root))
        try:
            await asyncio.to_thread(_copy_workspace, self.docs.root, tmpdir)
        except ApiError:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        # The preview uses a fresh provider session so the user's real session history is not polluted
        # with an isolated-copy context. Codex sandbox write scope = the temporary copy.
        sess = await self.sessions.create_for_provider(provider, f"Preview: {doc.path}")
        with self.ctx.db.write() as s:
            srow = s.get(type(sess), sess.id)
            if srow:
                srow.origin = "preview"
        preview_id = new_id()
        prompt = (
            "You are preparing a PROPOSED change to a Markdown wiki document. You are working in an isolated copy of the "
            f"workspace at {tmpdir}. Edit ONLY the file {doc.path} (relative to that directory) in place according to the "
            "instruction below, then briefly summarize the change. Do not modify other files.\n\n"
            f"Document SHA-256 before the change: {sha}\n\nInstruction:\n{body.instruction}"
        )
        job_id = self.jobs.enqueue_turn(
            session_id=sess.id, thread_id=None, prompt=prompt, context={"document_id": doc_id, "document_path": doc.path, "sha256": sha, "preview_id": preview_id},
            agent_message_id=None, kind="preview", cwd=tmpdir, on_complete=self._make_on_complete(preview_id, tmpdir, doc.path),
        )
        with self.ctx.db.write() as s:
            s.add(Preview(id=preview_id, document_id=doc_id, job_id=job_id, base_sha256=sha, status="pending"))
        return {"preview_id": preview_id, "job_id": job_id, "session_id": sess.id, "base_sha256": sha}

    def _make_on_complete(self, preview_id: str, tmpdir: Path, rel_path: str):
        async def on_complete(job_id: str) -> None:
            try:
                with self.ctx.db.read() as s:
                    job = s.get(Job, job_id)
                status = job.status if job else "failed"
                proposed_ref = None
                proposed_sha = None
                if status == "completed":
                    target = tmpdir / rel_path
                    if target.is_file():
                        data = target.read_bytes()
                        proposed_sha = sha256_bytes(data)
                        proposed_ref = f"previews/{preview_id}.md"
                        dest = self.docs.snapshots_dir / proposed_ref
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                with self.ctx.db.write() as s:
                    p = s.get(Preview, preview_id)
                    if p:
                        if status == "completed" and proposed_sha:
                            p.status = "ready"
                            p.proposed_sha256 = proposed_sha
                            p.proposed_snapshot_ref = proposed_ref
                        else:
                            p.status = "failed"
                if self.ctx.events:
                    self.ctx.events.publish("global", {"type": "preview_update", "preview_id": preview_id, "status": "ready" if proposed_sha else "failed", "job_id": job_id})
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        return on_complete

    def get(self, preview_id: str) -> dict[str, Any]:
        with self.ctx.db.read() as s:
            p = s.get(Preview, preview_id)
            if p is None:
                raise not_found("preview not found", "PREVIEW_NOT_FOUND")
            doc = s.get(Document, p.document_id)
            job = s.get(Job, p.job_id)
        assert doc is not None
        out: dict[str, Any] = {
            "preview_id": p.id, "document_id": p.document_id, "job_id": p.job_id, "status": p.status,
            "base_sha256": p.base_sha256, "proposed_sha256": p.proposed_sha256, "created_at": iso(p.created_at),
            "applied_at": iso(p.applied_at), "job_status": job.status if job else None,
            "job_error": (job.error_message if job else None),
        }
        if p.status in ("ready", "applied") and p.proposed_snapshot_ref:
            base = self.docs.snapshot_text(doc.id, p.base_sha256) or ""
            proposed_path = self.docs.snapshots_dir / p.proposed_snapshot_ref
            proposed = proposed_path.read_text("utf-8", errors="replace") if proposed_path.exists() else ""
            out["unified"] = "".join(difflib.unified_diff(base.splitlines(keepends=True), proposed.splitlines(keepends=True),
                                                          fromfile=f"{doc.path}@base", tofile=f"{doc.path}@proposed", n=3))
            out["identical"] = base == proposed
            out["proposed_text"] = proposed
            try:
                _, _, cur = self.docs.current_content(doc)
                out["current_sha256"] = cur
                out["stale"] = cur != p.base_sha256
            except ApiError:
                out["stale"] = True
        return out

    def apply(self, preview_id: str, base_sha256: str) -> dict[str, Any]:
        with self.ctx.db.read() as s:
            p = s.get(Preview, preview_id)
            if p is None:
                raise not_found("preview not found", "PREVIEW_NOT_FOUND")
            doc = s.get(Document, p.document_id)
        assert doc is not None
        if p.status != "ready" or not p.proposed_snapshot_ref:
            raise conflict("PREVIEW_NOT_READY", f"preview status is {p.status}")
        if self.ctx.locks is not None:
            self.ctx.locks.assert_writable(doc.path, "applying a preview")
        if base_sha256 != p.base_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "base_sha256 does not match the preview base", preview_base=p.base_sha256)
        _, _, current = self.docs.current_content(doc)
        if current != p.base_sha256:
            with self.ctx.db.write() as s:
                pp = s.get(Preview, preview_id)
                if pp:
                    pp.status = "stale"
            raise conflict(DOCUMENT_VERSION_CONFLICT, "the file changed after the preview was created; the newer change was preserved",
                           current_sha256=current, preview_base=p.base_sha256)
        data = (self.docs.snapshots_dir / p.proposed_snapshot_ref).read_bytes()
        with self.ctx.db.write() as s:
            # record pre-state and write atomically inside the write lock so a concurrent observer cannot interleave
            new_sha = self.docs.write_atomic(doc.path, data)
            pp = s.get(Preview, preview_id)
            if pp:
                pp.status = "applied"
                pp.applied_at = utcnow()
        self.docs.record_version(doc.id, data, new_sha, source="preview_apply")
        if self.ctx.events:
            self.ctx.events.publish("global", {"type": "file_changed", "path": doc.path, "document_id": doc.id, "sha256": new_sha, "change": "modified", "source": "preview_apply"})
        return {"document_id": doc.id, "path": doc.path, "sha256": new_sha, "previous_sha256": p.base_sha256}


class PreviewCreateIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=100000)
    base_sha256: str
    session_id: str | None = None


class ApplyIn(BaseModel):
    base_sha256: str


@router.post("/documents/{doc_id}/previews", status_code=201)
async def create_preview(doc_id: str, body: PreviewCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await PreviewService(ctx).create(doc_id, body)


@router.get("/previews/{preview_id}")
def get_preview(preview_id: str, ctx: AppContext = Depends(require_auth)):
    return PreviewService(ctx).get(preview_id)


@router.post("/previews/{preview_id}/apply")
def apply_preview(preview_id: str, body: ApplyIn, ctx: AppContext = Depends(require_auth_mutating)):
    return PreviewService(ctx).apply(preview_id, body.base_sha256)

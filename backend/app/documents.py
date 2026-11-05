"""Documents: safe path handling, file tree, open/version snapshots, reading state,
review marks and diffs (DESIGN §5.1, §9, §12, §13)."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from . import anchors as anchoring
from .db import Database, iso, new_id, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found, DOCUMENT_VERSION_CONFLICT, INVALID_ANCHOR
from .gitinfo import GitInfo, git_info, git_show
from .models import Document, DocumentVersion, ReadingState, ReviewMark, Thread, Workspace
from .textmodel import build_text_model

MARKDOWN_SUFFIXES = {".md", ".markdown"}
EXCLUDED_DIRS = {".git", "node_modules", ".codex", ".opencode", "__pycache__"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DocumentService:
    def __init__(self, db: Database, root: Path, snapshots_dir: Path, max_bytes: int):
        self.db = db
        self.root = root.resolve()
        self.snapshots_dir = snapshots_dir
        self.max_bytes = max_bytes
        self.workspace_id = self._ensure_workspace()

    # ------------------------------------------------------------------ workspace
    def _ensure_workspace(self) -> str:
        with self.db.write() as s:
            ws = s.query(Workspace).filter_by(root_path=str(self.root)).one_or_none()
            if ws is None:
                ws = Workspace(id=new_id(), root_path=str(self.root))
                s.add(ws)
                s.flush()
            return ws.id

    # ------------------------------------------------------------------ paths
    def normalize_rel(self, rel: str) -> str:
        rel = rel.replace("\\", "/").strip()
        if rel.startswith("/"):
            raise bad_request("path must be relative", "INVALID_PATH")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise bad_request("path traversal is not allowed", "INVALID_PATH")
        if any("\0" in p for p in parts):
            raise bad_request("invalid path", "INVALID_PATH")
        return "/".join(parts)

    def resolve(self, rel: str, *, must_exist: bool = True) -> tuple[str, Path]:
        """Resolve a relative path under the root, rejecting traversal and symlink escapes."""
        norm = self.normalize_rel(rel)
        candidate = self.root / norm if norm else self.root
        # Reject any symlink component escaping the root (also when the final target is inside).
        cur = self.root
        for part in norm.split("/") if norm else []:
            cur = cur / part
            if cur.is_symlink():
                target = cur.resolve()
                if target != self.root and self.root not in target.parents:
                    raise ApiError(403, "PATH_ESCAPES_WORKSPACE", "symlink escapes the workspace")
        try:
            real = candidate.resolve(strict=must_exist)
        except FileNotFoundError:
            raise not_found(f"{norm} not found", "FILE_NOT_FOUND")
        if real != self.root and self.root not in real.parents:
            raise ApiError(403, "PATH_ESCAPES_WORKSPACE", "path escapes the workspace")
        return norm, candidate

    # ------------------------------------------------------------------ tree
    def list_dir(self, rel: str) -> dict[str, Any]:
        norm, path = self.resolve(rel)
        if not path.is_dir():
            raise bad_request("not a directory", "NOT_A_DIRECTORY")
        info = git_info(self.root)
        items = []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except PermissionError:
            raise ApiError(403, "PERMISSION_DENIED", "cannot read directory")
        for e in entries:
            if e.name in EXCLUDED_DIRS or e.name.startswith(".waw"):
                continue
            rel_item = f"{norm}/{e.name}" if norm else e.name
            if e.is_dir(follow_symlinks=False):
                items.append({"path": rel_item, "name": e.name, "type": "dir", "git_status": self._dir_status(info, rel_item)})
            elif e.is_file(follow_symlinks=False) or (e.is_symlink() and e.is_file()):
                if Path(e.name).suffix.lower() in MARKDOWN_SUFFIXES:
                    items.append({
                        "path": rel_item,
                        "name": e.name,
                        "type": "file",
                        "git_status": self._file_status(info, rel_item),
                    })
        return {
            "path": norm,
            "items": items,
            "git": self.git_summary(info),
        }

    @staticmethod
    def _file_status(info: GitInfo, rel: str) -> str | None:
        if not info.is_repo:
            return None
        return info.statuses.get(rel, "clean")

    @staticmethod
    def _dir_status(info: GitInfo, rel: str) -> str | None:
        if not info.is_repo:
            return None
        prefix = rel + "/"
        st = {v for k, v in info.statuses.items() if k.startswith(prefix)}
        if "modified" in st or "deleted" in st:
            return "modified"
        if "untracked" in st:
            return "untracked"
        return "clean"

    @staticmethod
    def git_summary(info: GitInfo) -> dict[str, Any]:
        return {
            "is_repo": info.is_repo,
            "branch": info.branch,
            "commit": info.commit,
            "remote_state": info.remote_state,
            "error": info.error,
        }

    # ------------------------------------------------------------------ reading files
    def read_bytes(self, rel: str) -> tuple[str, bytes]:
        norm, path = self.resolve(rel)
        if not path.is_file():
            raise not_found(f"{norm} is not a file", "FILE_NOT_FOUND")
        if Path(norm).suffix.lower() not in MARKDOWN_SUFFIXES:
            raise bad_request("only .md/.markdown files can be opened", "NOT_MARKDOWN")
        size = path.stat().st_size
        if size > self.max_bytes:
            raise ApiError(413, "DOCUMENT_TOO_LARGE", f"document is {size} bytes; limit is {self.max_bytes}")
        return norm, path.read_bytes()

    @staticmethod
    def decode(data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise bad_request(f"file is not valid UTF-8 (byte {exc.start})", "INVALID_UTF8")

    # ------------------------------------------------------------------ documents
    def get_or_create_document(self, norm: str) -> Document:
        with self.db.write() as s:
            doc = s.query(Document).filter_by(workspace_id=self.workspace_id, path=norm).one_or_none()
            if doc is None:
                doc = Document(id=new_id(), workspace_id=self.workspace_id, path=norm)
                s.add(doc)
                s.flush()
            return doc

    def get_document(self, doc_id: str) -> Document:
        with self.db.read() as s:
            doc = s.get(Document, doc_id)
        if doc is None:
            raise not_found("document not found", "DOCUMENT_NOT_FOUND")
        return doc

    def current_content(self, doc: Document) -> tuple[str, bytes, str]:
        """Returns (text, bytes, sha256) of the current file. Marks the doc missing if gone."""
        try:
            norm, data = self.read_bytes(doc.path)
        except ApiError as exc:
            if exc.code == "FILE_NOT_FOUND":
                with self.db.write() as s:
                    d = s.get(Document, doc.id)
                    if d and not d.missing:
                        d.missing = True
                raise not_found("document file is missing; relink it to another file", "DOCUMENT_MISSING", path=doc.path)
            raise
        text = self.decode(data)
        sha = sha256_bytes(data)
        return text, data, sha

    def record_version(self, doc_id: str, data: bytes, sha: str, *, source: str = "observed") -> DocumentVersion:
        info = git_info(self.root)
        with self.db.write() as s:
            existing = s.query(DocumentVersion).filter_by(document_id=doc_id, sha256=sha).one_or_none()
            if existing is None:
                ref = f"{doc_id}/{sha}.md"
                target = self.snapshots_dir / ref
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    tmp = target.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, target)
                existing = DocumentVersion(
                    id=new_id(), document_id=doc_id, sha256=sha, snapshot_ref=ref,
                    git_branch=info.branch, git_commit=info.commit, source=source,
                )
                s.add(existing)
            doc = s.get(Document, doc_id)
            if doc:
                doc.last_sha256 = sha
                doc.missing = False
            return existing

    def snapshot_text(self, doc_id: str, sha: str) -> str | None:
        with self.db.read() as s:
            v = s.query(DocumentVersion).filter_by(document_id=doc_id, sha256=sha).one_or_none()
        if v is None:
            return None
        p = self.snapshots_dir / v.snapshot_ref
        if not p.exists():
            return None
        return p.read_text("utf-8", errors="replace")

    def list_versions(self, doc_id: str) -> list[dict[str, Any]]:
        with self.db.read() as s:
            rows = s.query(DocumentVersion).filter_by(document_id=doc_id).order_by(DocumentVersion.created_at.desc()).all()
        return [
            {"id": v.id, "sha256": v.sha256, "git_branch": v.git_branch, "git_commit": v.git_commit,
             "source": v.source, "created_at": iso(v.created_at)}
            for v in rows
        ]

    def open_document(self, rel: str) -> dict[str, Any]:
        norm, data = self.read_bytes(rel)
        text = self.decode(data)
        sha = sha256_bytes(data)
        doc = self.get_or_create_document(norm)
        self.record_version(doc.id, data, sha)
        info = git_info(self.root)
        with self.db.read() as s:
            rs = s.get(ReadingState, doc.id)
            doc = s.get(Document, doc.id)
        return {
            "document_id": doc.id,
            "path": norm,
            "text": text,
            "sha256": sha,
            "git": {**self.git_summary(info), "file_status": self._file_status(info, norm)},
            "reading_state": self._reading_state_dict(rs),
            "default_session_id": doc.default_session_id,
            "missing": False,
        }

    def observe_file_change(self, norm: str) -> dict[str, Any] | None:
        """Called by the watcher. Records a new version for known documents."""
        with self.db.read() as s:
            doc = s.query(Document).filter_by(workspace_id=self.workspace_id, path=norm).one_or_none()
        if doc is None:
            return None
        path = self.root / norm
        if not path.exists():
            with self.db.write() as s:
                d = s.get(Document, doc.id)
                if d:
                    d.missing = True
            return {"document_id": doc.id, "path": norm, "missing": True}
        try:
            data = path.read_bytes()
        except OSError:
            return None
        sha = sha256_bytes(data)
        if sha == doc.last_sha256:
            return None
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        self.record_version(doc.id, data, sha)
        self.flag_review_marks(doc.id, data.decode("utf-8"), sha)
        return {"document_id": doc.id, "path": norm, "sha256": sha, "missing": False}

    # ------------------------------------------------------------------ reading state
    @staticmethod
    def _reading_state_dict(rs: ReadingState | None) -> dict[str, Any] | None:
        if rs is None:
            return None
        return {
            "version_sha256": rs.version_sha256,
            "heading": rs.heading,
            "offset": rs.offset,
            "scroll_fraction": rs.scroll_fraction,
            "updated_at": iso(rs.updated_at),
        }

    def save_reading_state(self, doc_id: str, body: "ReadingStateIn") -> dict[str, Any]:
        self.get_document(doc_id)
        with self.db.write() as s:
            rs = s.get(ReadingState, doc_id)
            if rs is None:
                rs = ReadingState(document_id=doc_id)
                s.add(rs)
            rs.version_sha256 = body.version_sha256
            rs.heading = body.heading
            rs.offset = body.offset
            rs.scroll_fraction = body.scroll_fraction
            rs.updated_at = utcnow()
            s.flush()
            return self._reading_state_dict(rs) or {}

    # ------------------------------------------------------------------ review marks
    def add_review_mark(self, doc_id: str, version_sha256: str, anchor: dict[str, Any]) -> dict[str, Any]:
        doc = self.get_document(doc_id)
        text, _, sha = self.current_content(doc)
        if sha != version_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "document changed; reload before marking reviewed", current_sha256=sha)
        model = build_text_model(text)
        err = anchoring.validate_offsets(model.text, anchor["start"], anchor["end"], anchor["quote"])
        if err:
            raise bad_request(err, INVALID_ANCHOR)
        with self.db.write() as s:
            m = ReviewMark(
                id=new_id(), document_id=doc_id, quote=anchor["quote"], prefix=anchor.get("prefix", ""),
                suffix=anchor.get("suffix", ""), start=anchor["start"], end=anchor["end"], heading=anchor.get("heading"),
                reviewed_version_sha256=version_sha256, state="reviewed",
            )
            s.add(m)
            s.flush()
            return self._mark_dict(m)

    @staticmethod
    def _mark_dict(m: ReviewMark) -> dict[str, Any]:
        return {
            "id": m.id, "quote": m.quote, "prefix": m.prefix, "suffix": m.suffix, "start": m.start, "end": m.end,
            "heading": m.heading, "reviewed_version_sha256": m.reviewed_version_sha256, "state": m.state,
            "updated_at": iso(m.updated_at),
        }

    def list_review_marks(self, doc_id: str, text: str, sha: str) -> list[dict[str, Any]]:
        self.flag_review_marks(doc_id, text, sha)
        with self.db.read() as s:
            rows = s.query(ReviewMark).filter_by(document_id=doc_id).order_by(ReviewMark.start).all()
        out = []
        model = build_text_model(text)
        for m in rows:
            d = self._mark_dict(m)
            res = anchoring.resolve(
                anchoring.Anchor(m.reviewed_version_sha256, m.quote, m.prefix, m.suffix, m.start, m.end, m.heading),
                model.text, sha,
            )
            d["anchor_state"] = res.state
            d["current_start"] = res.start
            d["current_end"] = res.end
            out.append(d)
        return out

    def flag_review_marks(self, doc_id: str, text: str, sha: str) -> None:
        """A reviewed passage whose text no longer matches exactly becomes `needs_review`."""
        with self.db.read() as s:
            rows = s.query(ReviewMark).filter_by(document_id=doc_id, state="reviewed").all()
        if not rows:
            return
        model = build_text_model(text)
        changed: list[str] = []
        for m in rows:
            if m.reviewed_version_sha256 == sha:
                continue
            res = anchoring.resolve(
                anchoring.Anchor(m.reviewed_version_sha256, m.quote, m.prefix, m.suffix, m.start, m.end, m.heading),
                model.text, sha,
            )
            if res.state in (anchoring.ORPHANED, anchoring.AMBIGUOUS):
                changed.append(m.id)
            elif res.state == anchoring.MOVED:
                # exact text still exists but surrounding context differs: content of the section changed
                old_ctx = (m.prefix, m.suffix)
                if (res.prefix, res.suffix) != old_ctx and m.prefix and m.suffix:
                    changed.append(m.id)
        if changed:
            with self.db.write() as s:
                for mid in changed:
                    row = s.get(ReviewMark, mid)
                    if row:
                        row.state = "needs_review"

    # ------------------------------------------------------------------ diff
    def diff(self, doc_id: str, from_ref: str, to_ref: str) -> dict[str, Any]:
        doc = self.get_document(doc_id)
        a = self._resolve_ref(doc, from_ref)
        b = self._resolve_ref(doc, to_ref)
        lines = list(difflib.unified_diff(
            a.splitlines(keepends=True), b.splitlines(keepends=True),
            fromfile=f"{doc.path}@{from_ref[:12]}", tofile=f"{doc.path}@{to_ref[:12]}", n=3,
        ))
        return {"document_id": doc_id, "from": from_ref, "to": to_ref, "unified": "".join(lines), "identical": a == b}

    def _resolve_ref(self, doc: Document, ref: str) -> str:
        if ref == "current":
            text, _, _ = self.current_content(doc)
            return text
        snap = self.snapshot_text(doc.id, ref)
        if snap is not None:
            return snap
        with self.db.read() as s:
            v = s.query(DocumentVersion).filter_by(id=ref).one_or_none()
        if v is not None:
            p = self.snapshots_dir / v.snapshot_ref
            if p.exists():
                return p.read_text("utf-8", errors="replace")
        ok, content = git_show(self.root, ref, doc.path)
        if ok:
            return content
        raise not_found(f"unknown snapshot or git ref: {ref}", "REF_NOT_FOUND", detail=content)

    # ------------------------------------------------------------------ relink
    def relink(self, doc_id: str, new_rel: str) -> dict[str, Any]:
        norm, data = self.read_bytes(new_rel)
        with self.db.write() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                raise not_found("document not found", "DOCUMENT_NOT_FOUND")
            other = s.query(Document).filter_by(workspace_id=self.workspace_id, path=norm).one_or_none()
            if other is not None and other.id != doc_id:
                raise conflict("DOCUMENT_EXISTS", "another document record already uses that path", document_id=other.id)
            doc.path = norm
            doc.missing = False
        sha = sha256_bytes(data)
        self.record_version(doc_id, data, sha)
        return {"document_id": doc_id, "path": norm, "sha256": sha}

    # ------------------------------------------------------------------ writes (preview apply)
    def write_atomic(self, rel: str, data: bytes) -> str:
        norm, path = self.resolve(rel)
        if path.is_symlink():
            raise ApiError(403, "PATH_ESCAPES_WORKSPACE", "refusing to write through a symlink")
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        tmp = path.with_name(f".waw-tmp-{os.getpid()}-{path.name}")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return sha256_bytes(data)


# ---------------------------------------------------------------------- schemas


class ReadingStateIn(BaseModel):
    version_sha256: str | None = None
    heading: str | None = None
    offset: int = 0
    scroll_fraction: float = Field(0.0, ge=0.0, le=1.0)


class AnchorIn(BaseModel):
    quote: str = Field(min_length=1, max_length=20000)
    prefix: str = Field("", max_length=80)
    suffix: str = Field("", max_length=80)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    heading: str | None = None


class ReviewMarkIn(BaseModel):
    version_sha256: str
    anchor: AnchorIn


class RelinkIn(BaseModel):
    path: str


class DefaultSessionIn(BaseModel):
    session_id: str | None


# ---------------------------------------------------------------------- routes

router = APIRouter()


def _svc(ctx: AppContext) -> DocumentService:
    assert ctx.documents is not None
    return ctx.documents


@router.get("/files")
def list_files(path: str = Query(""), ctx: AppContext = Depends(require_auth)):
    return _svc(ctx).list_dir(path)


@router.get("/raw")
def raw_file(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    """Serve a non-executable workspace file (images referenced by Markdown)."""
    import mimetypes

    from fastapi.responses import Response as RawResponse

    svc = _svc(ctx)
    norm, p = svc.resolve(path)
    if not p.is_file():
        raise not_found("file not found", "FILE_NOT_FOUND")
    if p.stat().st_size > svc.max_bytes:
        raise ApiError(413, "FILE_TOO_LARGE", "file exceeds the configured size limit")
    ctype = mimetypes.guess_type(norm)[0] or "application/octet-stream"
    if ctype in ("text/html", "application/xhtml+xml", "image/svg+xml", "application/javascript", "text/javascript"):
        ctype = "text/plain"
    return RawResponse(content=p.read_bytes(), media_type=ctype,
                       headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


@router.get("/documents/open")
def open_document(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    return _svc(ctx).open_document(path)


@router.get("/documents/{doc_id}/versions")
def list_versions(doc_id: str, ctx: AppContext = Depends(require_auth)):
    svc = _svc(ctx)
    svc.get_document(doc_id)
    return {"versions": svc.list_versions(doc_id)}


@router.get("/documents/{doc_id}/versions/{sha}")
def get_version(doc_id: str, sha: str, ctx: AppContext = Depends(require_auth)):
    svc = _svc(ctx)
    svc.get_document(doc_id)
    text = svc.snapshot_text(doc_id, sha)
    if text is None:
        raise not_found("version snapshot not found", "VERSION_NOT_FOUND")
    return {"document_id": doc_id, "sha256": sha, "text": text}


@router.put("/documents/{doc_id}/reading-state")
def put_reading_state(doc_id: str, body: ReadingStateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _svc(ctx).save_reading_state(doc_id, body)


@router.post("/documents/{doc_id}/review-marks", status_code=201)
def post_review_mark(doc_id: str, body: ReviewMarkIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _svc(ctx).add_review_mark(doc_id, body.version_sha256, body.anchor.model_dump())


@router.get("/documents/{doc_id}/review-marks")
def get_review_marks(doc_id: str, ctx: AppContext = Depends(require_auth)):
    svc = _svc(ctx)
    doc = svc.get_document(doc_id)
    text, _, sha = svc.current_content(doc)
    return {"marks": svc.list_review_marks(doc_id, text, sha), "sha256": sha}


@router.get("/documents/{doc_id}/diff")
def get_diff(doc_id: str, from_: str = Query(alias="from"), to: str = Query("current"), ctx: AppContext = Depends(require_auth)):
    return _svc(ctx).diff(doc_id, from_, to)


@router.post("/documents/{doc_id}/relink")
def relink_document(doc_id: str, body: RelinkIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _svc(ctx).relink(doc_id, body.path)


@router.get("/documents")
def list_documents(ctx: AppContext = Depends(require_auth)):
    svc = _svc(ctx)
    with svc.db.read() as s:
        docs = s.query(Document).filter_by(workspace_id=svc.workspace_id).order_by(Document.updated_at.desc()).all()
        counts = {d.id: s.query(Thread).filter_by(document_id=d.id).count() for d in docs}
    return {
        "documents": [
            {"id": d.id, "path": d.path, "missing": d.missing, "last_sha256": d.last_sha256,
             "default_session_id": d.default_session_id, "thread_count": counts[d.id], "updated_at": iso(d.updated_at)}
            for d in docs
        ]
    }

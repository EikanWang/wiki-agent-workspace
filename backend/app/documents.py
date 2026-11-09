"""Documents: safe path handling, file tree, open/version snapshots, reading state,
review marks and diffs (DESIGN §5.1, §9, §12, §13)."""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from . import anchors as anchoring
from .db import Database, iso, new_id, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found, DOCUMENT_VERSION_CONFLICT, INVALID_ANCHOR
from .gitinfo import GitInfo, git_info, git_show
from .models import (
    AgentSession,
    AnchorHistory,
    Document,
    DocumentVersion,
    Job,
    JobEvent,
    Message,
    Preview,
    ReadingState,
    ReviewMark,
    Thread,
    Workspace,
)
from .textmodel import NO_TEXT_FORMATS, OFFICE_FORMATS, build_text_model, build_text_model_from_html, document_format, render_document

MARKDOWN_SUFFIXES = {".md", ".markdown"}


class _Model:
    """Minimal stand-in for textmodel.TextModel when only the text is needed."""

    def __init__(self, text: str):
        self.text = text
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
        self.locks = None  # set by main: LockService

    # ------------------------------------------------------------------ workspace
    def _ensure_workspace(self) -> str:
        """Find or create the workspace row for the configured root.

        The app is single-workspace: if the database already holds exactly one workspace whose
        root differs from the configured one (the wiki checkout was moved, or the state folder
        was copied to another machine), that row is relocated so documents, discussions and
        reading state follow the new path instead of silently starting from scratch."""
        with self.db.write() as s:
            ws = s.query(Workspace).filter_by(root_path=str(self.root)).one_or_none()
            if ws is None:
                existing = s.query(Workspace).all()
                log = logging.getLogger("waw.documents")
                if len(existing) == 1 and not Path(existing[0].root_path).exists():
                    # the checkout itself was moved: follow it
                    ws = existing[0]
                    log.warning("workspace root %s no longer exists; relocating its documents and discussions to %s", ws.root_path, self.root)
                    ws.root_path = str(self.root)
                else:
                    if existing:
                        log.warning("configured workspace %s is new; data for %s stays in the database and reappears when that root is configured again",
                                    self.root, ", ".join(w.root_path for w in existing))
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

    def resolve(self, rel: str, *, must_exist: bool = True, for_write: bool = False) -> tuple[str, Path]:
        """Resolve a relative path under the root.

        Reads may follow symlinks that point outside the workspace (wikis commonly link to source corpora);
        the tree marks such entries as `escapes` so the user knows. Writes never go through a symlink that leaves
        the root, and `..` traversal is always rejected."""
        norm = self.normalize_rel(rel)
        candidate = self.root / norm if norm else self.root
        try:
            real = candidate.resolve(strict=must_exist)
        except FileNotFoundError:
            raise not_found(f"{norm} not found", "FILE_NOT_FOUND")
        inside = real == self.root or self.root in real.parents
        if not inside and for_write:
            raise ApiError(403, "PATH_ESCAPES_WORKSPACE", "refusing to write through a symlink that leaves the workspace")
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
            is_link = e.is_symlink()
            link_target: str | None = None
            escapes = False
            broken = False
            if is_link:
                try:
                    link_target = os.readlink(e.path)
                    real = Path(e.path).resolve(strict=True)
                    escapes = real != self.root and self.root not in real.parents
                except OSError:
                    broken = True
            if not broken and e.is_dir(follow_symlinks=True):
                items.append({"path": rel_item, "name": e.name, "type": "dir", "kind": "dir", "git_status": self._dir_status(info, rel_item),
                              "symlink": is_link, "link_target": link_target, "escapes": escapes})
            else:
                kind = "broken_link" if broken else self.classify(e.name)
                size = None
                if not broken:
                    try:
                        size = e.stat(follow_symlinks=True).st_size
                    except OSError:
                        size = None
                items.append({
                    "path": rel_item, "name": e.name, "type": "file", "kind": kind, "size": size,
                    "openable": document_format(e.name) != "unsupported" and not broken,
                    "format": document_format(e.name),
                    "git_status": self._file_status(info, rel_item),
                    "symlink": is_link, "link_target": link_target, "escapes": escapes,
                })
        if self.locks is not None:
            for it in items:
                lock = self.locks.covering(it["path"])
                it["locked"] = bool(lock)
                it["lock_inherited"] = bool(lock) and lock["path"] != it["path"]
        return {
            "path": norm,
            "items": items,
            "git": self.git_summary(info),
            "lock": self.locks.covering(norm) if self.locks is not None else None,
        }

    KINDS: dict[str, set[str]] = {
        "markdown": {".md", ".markdown"},
        "image": {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico", ".avif"},
        "pdf": {".pdf"},
        "slides": {".pptx", ".ppt", ".odp", ".key"},
        "word": {".docx", ".doc", ".odt", ".rtf"},
        "diagram": {".vsdx", ".vsd", ".vsdm", ".vss", ".vssx", ".vst", ".vstx", ".odg"},
        "notebook": {".ipynb"},
        "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".cc", ".cpp", ".h", ".hpp", ".cu", ".rs", ".go", ".java", ".kt", ".swift",
                 ".rb", ".php", ".cs", ".scala", ".lua", ".pl", ".r", ".m", ".mm", ".cl", ".glsl", ".asm", ".s"},
        "shell": {".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd"},
        "data": {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".csv", ".tsv", ".parquet", ".sqlite", ".sqlite3", ".db"},
        "text": {".txt", ".rst", ".adoc", ".org", ".log", ".tex", ".bib", ".html", ".htm", ".css"},
        "archive": {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar"},
        "binary": {".bin", ".so", ".o", ".a", ".dll", ".exe", ".wasm", ".pt", ".pth", ".onnx", ".npy", ".npz", ".pkl"},
    }
    SPECIAL_NAMES = {"makefile": "shell", "dockerfile": "shell", "license": "text", "readme": "text", ".gitignore": "data",
                     ".gitattributes": "data", ".editorconfig": "data", ".env": "data"}

    @classmethod
    def classify(cls, name: str) -> str:
        lower = name.lower()
        if lower in cls.SPECIAL_NAMES:
            return cls.SPECIAL_NAMES[lower]
        suffix = Path(lower).suffix
        for kind, exts in cls.KINDS.items():
            if suffix in exts:
                return kind
        return "file"

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
        if document_format(Path(norm).name) == "unsupported":
            raise bad_request("this file type cannot be opened in the reader (Markdown, text/source, PDF, Word, PowerPoint and Visio are supported)", "UNSUPPORTED_FORMAT")
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

    @staticmethod
    def render(norm: str, data: bytes) -> tuple[str, str]:
        """(format, html) for a document; PDFs and layout-only Office files render no HTML (they are shown as PDF)."""
        fmt = document_format(Path(norm).name)
        if fmt in NO_TEXT_FORMATS:
            return fmt, ""
        try:
            return fmt, render_document(fmt, data, Path(norm).name)
        except UnicodeDecodeError as exc:
            raise bad_request(f"file is not valid UTF-8 (byte {exc.start})", "INVALID_UTF8")
        except Exception as exc:  # corrupt docx etc.
            raise bad_request(f"could not render {fmt} file: {exc.__class__.__name__}", "RENDER_FAILED")

    def model_text(self, norm: str, data: bytes) -> str:
        """Rendered-text model for anchors, for any supported format."""
        fmt, html = self.render(norm, data)
        if fmt in NO_TEXT_FORMATS:
            return ""
        return build_text_model_from_html(html).text

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
        if doc.workspace_id != self.workspace_id:
            # never read a document's path relative to another root folder
            raise ApiError(409, "WRONG_WORKSPACE", "this document belongs to another folder of the window", {"workspace_id": doc.workspace_id})
        try:
            _, data = self.read_bytes(doc.path)
        except ApiError as exc:
            if exc.code == "FILE_NOT_FOUND":
                with self.db.write() as s:
                    d = s.get(Document, doc.id)
                    if d and not d.missing:
                        d.missing = True
                raise not_found("document file is missing; relink it to another file", "DOCUMENT_MISSING", path=doc.path)
            raise
        fmt = document_format(Path(doc.path).name)
        text = self.decode(data) if fmt in ("markdown", "text") else ""
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
        fmt, html = self.render(norm, data)
        text = self.decode(data) if fmt in ("markdown", "text") else ""
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
            "format": fmt,
            "editable": fmt in ("markdown", "text"),
            "text": text,
            "html": html if fmt in ("docx", "pptx", "visio", "text") else None,  # markdown is rendered client-side; others server-side
            "sha256": sha,
            "git": {**self.git_summary(info), "file_status": self._file_status(info, norm)},
            "reading_state": self._reading_state_dict(rs),
            "default_session_id": doc.default_session_id,
            "missing": False,
            "lock": self.locks.covering(norm) if self.locks is not None else None,
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
            # content is what we already know, e.g. the watcher seeing our own save (written atomically, then
            # recorded by save_content): not a change on disk
            return {"document_id": doc.id, "path": norm, "unchanged": True}
        try:
            model_text = self.model_text(norm, data)
        except ApiError:
            return None
        self.record_version(doc.id, data, sha)
        self.flag_review_marks(doc.id, model_text, sha, is_model=True)
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
            "active_thread_id": rs.active_thread_id,
            "updated_at": iso(rs.updated_at),
        }

    def save_reading_state(self, doc_id: str, body: "ReadingStateIn") -> dict[str, Any]:
        """Partial update: only the fields present in the request are changed."""
        self.get_document(doc_id)
        fields = body.model_dump(exclude_unset=True)
        with self.db.write() as s:
            rs = s.get(ReadingState, doc_id)
            if rs is None:
                rs = ReadingState(document_id=doc_id, offset=0, scroll_fraction=0.0)
                s.add(rs)
            for key in ("version_sha256", "heading", "offset", "scroll_fraction", "active_thread_id"):
                if key in fields:
                    setattr(rs, key, fields[key])
            rs.updated_at = utcnow()
            s.flush()
            return self._reading_state_dict(rs) or {}

    # ------------------------------------------------------------------ recent work
    def recent(self, limit: int = 50, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Documents the user worked on, most recent activity first (reading, discussions, edits)."""
        from collections import defaultdict

        with self.db.read() as s:
            q = s.query(Document).filter_by(workspace_id=self.workspace_id)
            if not include_hidden:
                q = q.filter(Document.hidden.is_(False))
            docs = {d.id: d for d in q.all()}
            states = {rs.document_id: rs for rs in s.query(ReadingState).filter(ReadingState.document_id.in_(list(docs))).all()} if docs else {}
            threads_by_doc: dict[str, list[Thread]] = defaultdict(list)
            for t in s.query(Thread).filter(Thread.document_id.in_(list(docs))).all() if docs else []:
                threads_by_doc[t.document_id].append(t)
            thread_doc = {t.id: t.document_id for ts in threads_by_doc.values() for t in ts}
            last_msg_by_doc: dict[str, Message] = {}
            msg_count: dict[str, int] = defaultdict(int)
            if thread_doc:
                for m in s.query(Message).filter(Message.thread_id.in_(list(thread_doc))).order_by(Message.created_at).all():
                    did = thread_doc[m.thread_id]
                    msg_count[did] += 1
                    last_msg_by_doc[did] = m  # ascending order: the last assignment is the newest
            sessions = {x.id: x for x in s.query(AgentSession).filter(AgentSession.workspace_id == self.workspace_id).all()}

            out: list[dict[str, Any]] = []
            for did, d in docs.items():
                rs = states.get(did)
                threads = threads_by_doc.get(did, [])
                last_msg = last_msg_by_doc.get(did)
                candidates = [x for x in (rs.updated_at if rs else None, last_msg.created_at if last_msg else None,
                                          max((t.updated_at for t in threads), default=None)) if x is not None]
                if not candidates:
                    continue  # merely opened once with nothing saved: not "work"
                last_activity = max(candidates)
                last_thread = None
                if rs and rs.active_thread_id and any(t.id == rs.active_thread_id for t in threads):
                    last_thread = next(t for t in threads if t.id == rs.active_thread_id)
                elif last_msg:
                    last_thread = next((t for t in threads if t.id == last_msg.thread_id), None)
                sess = sessions.get(d.default_session_id) if d.default_session_id else None
                out.append({
                    "document_id": d.id,
                    "path": d.path,
                    "missing": d.missing,
                    "hidden": d.hidden,
                    "last_activity": iso(last_activity),
                    "thread_count": len(threads),
                    "message_count": msg_count.get(did, 0),
                    "last_thread_id": last_thread.id if last_thread else None,
                    "last_thread_quote": last_thread.cur_quote[:120] if last_thread else None,
                    "last_message": ({"role": last_msg.role, "content": last_msg.content[:160], "status": last_msg.status,
                                      "created_at": iso(last_msg.created_at)} if last_msg else None),
                    "reading_state": self._reading_state_dict(rs),
                    "session": ({"provider": sess.provider, "short_id": sess.native_id[-8:] if sess.native_id else None,
                                 "display_name": sess.display_name, "status": sess.status} if sess else None),
                })
            out.sort(key=lambda e: e["last_activity"], reverse=True)
            return out[:limit]

    def set_hidden(self, doc_id: str, hidden: bool) -> dict[str, Any]:
        with self.db.write() as s:
            d = s.get(Document, doc_id)
            if d is None:
                raise not_found("document not found", "DOCUMENT_NOT_FOUND")
            d.hidden = hidden
            return {"document_id": doc_id, "hidden": hidden}

    def delete_history(self, doc_id: str) -> dict[str, Any]:
        """Permanently delete a document's app history: discussions, messages, jobs, versions, reading state,
        review marks, previews and snapshot files. The file in the wiki is never touched."""
        with self.db.read() as s:
            d0 = s.get(Document, doc_id)
        if d0 is not None and self.locks is not None:
            self.locks.assert_writable(d0.path, "deleting its history")
        with self.db.write() as s:
            d = s.get(Document, doc_id)
            if d is None:
                raise not_found("document not found", "DOCUMENT_NOT_FOUND")
            running = s.query(Job).filter(Job.thread_id.in_(s.query(Thread.id).filter_by(document_id=doc_id)),
                                          Job.status.in_(["queued", "running", "waiting_provider_permission"])).count()
            if running:
                raise conflict("DOCUMENT_BUSY", "an agent turn is still running for this document; cancel it first")
            thread_ids = [t.id for t in s.query(Thread).filter_by(document_id=doc_id).all()]
            counts = {"threads": len(thread_ids), "messages": 0, "versions": 0}
            if thread_ids:
                counts["messages"] = s.query(Message).filter(Message.thread_id.in_(thread_ids)).delete(synchronize_session=False)
                s.query(AnchorHistory).filter(AnchorHistory.thread_id.in_(thread_ids)).delete(synchronize_session=False)
                job_ids = [j.id for j in s.query(Job).filter(Job.thread_id.in_(thread_ids)).all()]
                if job_ids:
                    s.query(JobEvent).filter(JobEvent.job_id.in_(job_ids)).delete(synchronize_session=False)
                    s.query(Job).filter(Job.id.in_(job_ids)).delete(synchronize_session=False)
                s.query(Thread).filter(Thread.id.in_(thread_ids)).delete(synchronize_session=False)
            preview_job_ids = [p.job_id for p in s.query(Preview).filter_by(document_id=doc_id).all()]
            snapshot_refs = [p.proposed_snapshot_ref for p in s.query(Preview).filter_by(document_id=doc_id).all() if p.proposed_snapshot_ref]
            s.query(Preview).filter_by(document_id=doc_id).delete(synchronize_session=False)
            if preview_job_ids:
                s.query(JobEvent).filter(JobEvent.job_id.in_(preview_job_ids)).delete(synchronize_session=False)
                s.query(Job).filter(Job.id.in_(preview_job_ids)).delete(synchronize_session=False)
            s.query(ReviewMark).filter_by(document_id=doc_id).delete(synchronize_session=False)
            s.query(ReadingState).filter_by(document_id=doc_id).delete(synchronize_session=False)
            versions = s.query(DocumentVersion).filter_by(document_id=doc_id).all()
            counts["versions"] = len(versions)
            snapshot_refs += [v.snapshot_ref for v in versions]
            s.query(DocumentVersion).filter_by(document_id=doc_id).delete(synchronize_session=False)
            d.default_session_id = None
            d.last_sha256 = None
            d.hidden = False
        for ref in snapshot_refs:
            try:
                (self.snapshots_dir / ref).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (self.snapshots_dir / doc_id).rmdir()
        except OSError:
            pass
        return {"document_id": doc_id, "deleted": counts}


    # ------------------------------------------------------------------ review marks
    def add_review_mark(self, doc_id: str, version_sha256: str, anchor: dict[str, Any]) -> dict[str, Any]:
        doc = self.get_document(doc_id)
        model_text, sha = self.model_for_doc(doc)
        if sha != version_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "document changed; reload before marking reviewed", current_sha256=sha)
        model = _Model(model_text)
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

    def list_review_marks(self, doc_id: str, text: str, sha: str, *, is_model: bool = False) -> list[dict[str, Any]]:
        self.flag_review_marks(doc_id, text, sha, is_model=is_model)
        with self.db.read() as s:
            rows = s.query(ReviewMark).filter_by(document_id=doc_id).order_by(ReviewMark.start).all()
        out = []
        model = _Model(text) if is_model else build_text_model(text)
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

    def model_for_doc(self, doc: Document) -> tuple[str, str]:
        """(model_text, sha) of the current file content for any supported format."""
        _, data, sha = self.current_content(doc)
        return self.model_text(doc.path, data), sha

    def flag_review_marks(self, doc_id: str, text: str, sha: str, *, is_model: bool = False) -> None:
        """A reviewed passage whose text no longer matches exactly becomes `needs_review`."""
        with self.db.read() as s:
            rows = s.query(ReviewMark).filter_by(document_id=doc_id, state="reviewed").all()
        if not rows:
            return
        model = _Model(text) if is_model else build_text_model(text)
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
        if self.locks is not None:
            with self.db.read() as s:
                cur = s.get(Document, doc_id)
            if cur is not None:
                self.locks.assert_writable(cur.path, "relinking")
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
    def create_entry(self, rel: str, kind: str) -> dict[str, Any]:
        """New file or folder (VS Code's New File / New Folder). Missing parent folders are created ("notes/a.md").
        Refuses existing paths, locked folders, `.git` and app-internal names, and paths through symlinks that leave
        the folder. A new Markdown file starts with a heading made from its name; other files start empty."""
        norm = self.normalize_rel(rel)
        if not norm:
            raise bad_request("give a name", "INVALID_PATH")
        for part in norm.split("/"):
            if part in EXCLUDED_DIRS or part.startswith(".waw") or "\x00" in part or len(part.encode()) > 255 or part != part.strip():
                raise bad_request(f"invalid name: {part!r}", "INVALID_NAME")
        if kind not in ("file", "dir"):
            raise bad_request("kind must be file or dir", "INVALID_KIND")
        if self.locks is not None:
            self.locks.assert_writable(norm, "creating")
        _, path = self.resolve(norm, must_exist=False, for_write=True)  # refuses symlinks that leave the folder
        if path.exists() or path.is_symlink():
            raise conflict("ALREADY_EXISTS", f"{norm} already exists", path=norm)
        parent = path.parent
        if parent.exists() and not parent.is_dir():
            raise bad_request(f"{parent.relative_to(self.root).as_posix()} is a file, not a folder", "NOT_A_DIRECTORY")
        if kind == "dir":
            path.mkdir(parents=True)
        else:
            parent.mkdir(parents=True, exist_ok=True)
            content = f"# {Path(norm).stem}\n" if Path(norm).suffix.lower() in (".md", ".markdown") else ""
            with open(path, "x", encoding="utf-8") as fh:  # "x": never overwrite, even in a race
                fh.write(content)
        return {"path": norm, "kind": kind, "format": document_format(path.name) if kind == "file" else None,
                "openable": kind == "file" and document_format(path.name) != "unsupported"}

    def write_atomic(self, rel: str, data: bytes) -> str:
        if self.locks is not None:
            self.locks.assert_writable(self.normalize_rel(rel), "writing")
        _, path = self.resolve(rel, for_write=True)  # refuses symlinks that leave the folder
        if path.is_symlink():
            # a link to another file *inside* the folder: write the real target, which must not be locked either
            target = path.resolve()
            if self.locks is not None:
                self.locks.assert_writable(target.relative_to(self.root).as_posix(), "writing (symlink target)")
            path = target
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        tmp = path.with_name(f".waw-tmp-{os.getpid()}-{path.name}")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return sha256_bytes(data)

    # ------------------------------------------------------------------ direct editing
    def save_content(self, doc_id: str, text: str, base_sha256: str) -> dict[str, Any]:
        """Write user-edited text. Refuses when the file changed since the editor loaded it (409 with the current
        hash, so the browser can offer reload / overwrite), for non-text formats, oversize content and locks."""
        doc = self.get_document(doc_id)
        fmt = document_format(Path(doc.path).name)
        if fmt not in ("markdown", "text"):
            raise bad_request(f"{fmt} files cannot be edited as text", "NOT_EDITABLE")
        _, data, current = self.current_content(doc)
        if current != base_sha256:
            raise conflict(DOCUMENT_VERSION_CONFLICT, "the file changed on disk after you started editing", current_sha256=current)
        old_text = data.decode("utf-8")
        # keep the file's line-ending convention (browsers normalise textarea content to \n)
        if "\r\n" in old_text and "\r" not in text:
            text = text.replace("\n", "\r\n")
        new = text.encode("utf-8")
        if len(new) > self.max_bytes:
            raise ApiError(413, "DOCUMENT_TOO_LARGE", f"content is {len(new)} bytes; limit is {self.max_bytes}")
        if new == data:
            return {"document_id": doc_id, "sha256": current, "changed": False}
        sha = self.write_atomic(doc.path, new)
        self.record_version(doc_id, new, sha, source="user_edit")
        self.flag_review_marks(doc_id, self.model_text(doc.path, new), sha, is_model=True)
        return {"document_id": doc_id, "sha256": sha, "previous_sha256": current, "changed": True, "bytes": len(new)}


# ---------------------------------------------------------------------- schemas


class ReadingStateIn(BaseModel):
    version_sha256: str | None = None
    heading: str | None = None
    offset: int | None = Field(None, ge=0)
    scroll_fraction: float | None = Field(None, ge=0.0, le=1.0)
    active_thread_id: str | None = None


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
    if ctype == "application/pdf":
        # the browser's PDF viewer runs in a sandboxed frame; scripts inside PDFs are never executed by it
        pass
    return RawResponse(content=p.read_bytes(), media_type=ctype,
                       headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


def _office_file(ctx: AppContext, path: str) -> tuple[str, Path]:
    svc = _svc(ctx)
    norm, p = svc.resolve(path)
    if not p.is_file():
        raise not_found(f"{norm} not found", "FILE_NOT_FOUND")
    if document_format(p.name) not in OFFICE_FORMATS:
        raise bad_request("only Word, PowerPoint, Visio and OpenDocument files have a layout view", "UNSUPPORTED_FORMAT")
    if ctx.office is None:
        raise ApiError(503, "OFFICE_UNAVAILABLE", "the layout renderer is not set up")
    return norm, p


class CreateEntryIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    kind: str = Field("file", pattern="^(file|dir)$")


@router.post("/files", status_code=201)
def create_entry(body: CreateEntryIn, ctx: AppContext = Depends(require_auth_mutating)):
    """New file or folder in the folder the request is scoped to."""
    out = _svc(ctx).create_entry(body.path, body.kind)
    parent = out["path"].rsplit("/", 1)[0] if "/" in out["path"] else ""
    ctx.events.publish("global", {"type": "tree_changed", "workspace_id": ctx.workspace_id, "dirs": [parent]})
    return out


@router.get("/office/render")
def office_render(path: str = Query(...), retry: bool = False, ctx: AppContext = Depends(require_auth)):
    """State of the Layout view (PDF rendered by LibreOffice) of an Office file; starts the conversion if needed.
    Poll until `state` is `ready` (then GET /office/pdf), `failed` or `unavailable`."""
    norm, p = _office_file(ctx, path)
    return {"path": norm, **ctx.office.status(p, retry=retry)}


@router.get("/office/pdf")
def office_pdf(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    """The rendered PDF (shown by the browser's PDF viewer in the Layout view)."""
    from fastapi.responses import FileResponse

    norm, p = _office_file(ctx, path)
    pdf = ctx.office.pdf_path(p)
    if pdf is None:
        raise ApiError(409, "RENDER_NOT_READY", "the layout view is not rendered yet; poll /office/render first")
    from urllib.parse import quote

    name = Path(norm).stem + ".pdf"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"  # RFC 5987: any file name
    return FileResponse(pdf, media_type="application/pdf", headers={
        "Content-Disposition": disposition, "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-cache"})


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
    model_text, sha = svc.model_for_doc(doc)
    return {"marks": svc.list_review_marks(doc_id, model_text, sha, is_model=True), "sha256": sha}


@router.get("/documents/{doc_id}/diff")
def get_diff(doc_id: str, from_: str = Query(alias="from"), to: str = Query("current"), ctx: AppContext = Depends(require_auth)):
    return _svc(ctx).diff(doc_id, from_, to)


class ContentIn(BaseModel):
    text: str = Field(max_length=50 * 1024 * 1024)
    base_sha256: str = Field(min_length=64, max_length=64)


@router.put("/documents/{doc_id}/content")
def put_content(doc_id: str, body: ContentIn, ctx: AppContext = Depends(require_auth_mutating)):
    out = _svc(ctx).save_content(doc_id, body.text, body.base_sha256)
    if out.get("changed") and ctx.events:
        doc = _svc(ctx).get_document(doc_id)
        ctx.events.publish("global", {"type": "file_changed", "path": doc.path, "document_id": doc_id, "sha256": out["sha256"],
                                      "change": "modified", "source": "user_edit", "workspace_id": ctx.workspace_id})
    return out


@router.post("/documents/{doc_id}/relink")
def relink_document(doc_id: str, body: RelinkIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _svc(ctx).relink(doc_id, body.path)


@router.get("/recent")
def recent_work(limit: int = Query(50, ge=1, le=500), include_hidden: bool = Query(False), all_workspaces: bool = Query(False, alias="all"),
                ctx: AppContext = Depends(require_auth)):
    """Recent work of the current folder, or of every folder of the window with `all=true` (each item tagged)."""
    if not all_workspaces or ctx.workspaces is None:
        items = _svc(ctx).recent(limit, include_hidden)
        for it in items:
            it["workspace_id"] = ctx.workspace_id
        return {"items": items}
    names = {w["id"]: w["name"] for w in ctx.workspaces.list()}
    items: list[dict[str, Any]] = []
    for wid in ctx.workspaces.order:
        for it in ctx.workspaces.runtimes[wid].docs.recent(limit, include_hidden):
            it["workspace_id"] = wid
            it["workspace_name"] = names.get(wid)
            items.append(it)
    items.sort(key=lambda e: e["last_activity"], reverse=True)
    return {"items": items[:limit]}


class HiddenIn(BaseModel):
    hidden: bool


@router.put("/documents/{doc_id}/hidden")
def put_hidden(doc_id: str, body: HiddenIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _svc(ctx).set_hidden(doc_id, body.hidden)


@router.delete("/documents/{doc_id}/history")
def delete_history(doc_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return _svc(ctx).delete_history(doc_id)


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

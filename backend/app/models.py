"""SQLAlchemy ORM models (DESIGN §12).

Column-by-column documentation lives in docs/SCHEMA.md; tests/backend/test_schema_doc.py keeps
that file in sync with the migrated schema. Schema changes need an Alembic migration in
backend/migrations/versions/ plus an update to docs/SCHEMA.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    """One root folder of the window (VS Code-style "add folder to workspace"). All per-wiki state is keyed by it."""

    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)       # display name (default: folder name)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # order in the file tree
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")  # False = removed from the window (data kept)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class NamedWorkspace(Base):
    """A named set of folders (VS Code "workspace"): which folders the window shows, in order, with its own
    terminals. Folder data (documents, discussions, sessions…) belongs to the folder, so a folder in two named
    workspaces shares it. Exactly one row is `current`; the `is_default` row ("Default") always exists."""

    __tablename__ = "named_workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    folders: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")  # JSON list of workspaces.id
    # config.toml folders the user closed in this workspace (resolved paths): not re-added at startup
    closed_roots: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    current: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GithubRepoWiki(Base):
    """The wiki folder the agent works in for pull requests of a GitHub repository (review hub)."""

    __tablename__ = "github_repo_wikis"
    repo: Mapped[str] = mapped_column(Text, primary_key=True)  # owner/name, lower case
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class GithubPrDraft(Base):
    """A review comment written in the PR tab, not yet published to GitHub (sent together as one review)."""

    __tablename__ = "github_pr_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo: Mapped[str] = mapped_column(Text, nullable=False, index=True)  # owner/name, lower case
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(8), default="line", server_default="line")  # line | file (whole-file comment; line 0)
    side: Mapped[str] = mapped_column(String(5), nullable=False)  # LEFT (old) | RIGHT (new) — of the last line
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    start_side: Mapped[str | None] = mapped_column(String(5), nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)  # multi-line comments
    line_text: Mapped[str] = mapped_column(Text, default="")  # the commented lines, for display
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    origin: Mapped[str] = mapped_column(String(8), default="user", server_default="user")  # user | agent (from an agent review)
    # accepted: published with the review · proposed: an agent suggestion awaiting your decision · discarded: not published
    decision: Mapped[str] = mapped_column(String(10), default="accepted", server_default="accepted")
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # the agent review's discussion
    # review learning: what the agent first wrote (kept when you edit it), why you discarded it, its severity / category
    original_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    discard_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(8), nullable=True)  # blocker | major | minor | nit
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)


class ReviewCase(Base):
    """Review learning: one thing that happened to a review comment (an agent suggestion applied / edited / discarded,
    a comment the agent missed, one you taught), the material lessons are distilled from."""

    __tablename__ = "review_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo: Mapped[str] = mapped_column(Text, nullable=False, index=True)  # owner/name, lower case
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source: Mapped[str] = mapped_column(String(10), nullable=False)  # agent | user | reviewer | taught
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)  # applied | edited | discarded | missed | taught
    agent_body: Mapped[str | None] = mapped_column(Text, nullable=True)  # what the agent wrote
    final_body: Mapped[str | None] = mapped_column(Text, nullable=True)  # what was published / written by a person
    reason: Mapped[str | None] = mapped_column(String(16), nullable=True)  # why a suggestion was discarded
    severity: Mapped[str | None] = mapped_column(String(8), nullable=True)
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)  # the reviewer, for comments by others
    gh_ref: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)  # GitHub comment id (comments by others: once)
    distilled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReviewLesson(Base):
    """Review learning: a lesson for the agent's PR reviews (proposed by the distiller, taught or written by you);
    only active ones go into review prompts."""

    __tablename__ = "review_lessons"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False, index=True)  # owner/name (lower case) or * (every repository)
    paths: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")  # JSON globs; [] = any file
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # proposed | active | off | rejected
    source: Mapped[str] = mapped_column(String(10), nullable=False)  # distilled | taught | manual
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")  # JSON case ids
    replaces: Mapped[str | None] = mapped_column(String(36), nullable=True)  # a proposed revision of this lesson
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    contradicted_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    flagged: Mapped[str | None] = mapped_column(Text, nullable=True)  # why recent reviews contradict it (for you to look at)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class KvSetting(Base):
    """Small server-side settings stored as JSON: review hub preferences, extra agent context folders."""

    __tablename__ = "kv_settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="null")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("workspace_id", "path", name="uq_documents_workspace_path"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    default_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    last_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    missing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "sha256", name="uq_versions_doc_sha"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False)  # path relative to snapshots dir
    git_branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="observed")  # observed|preview_apply|agent_edit
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("provider", "native_id", "workspace_id", name="uq_sessions_provider_native_ws"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    native_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # uninitialized | ready | missing | unavailable | availability_unknown
    status: Mapped[str] = mapped_column(String(32), default="uninitialized", nullable=False)
    origin: Mapped[str] = mapped_column(String(32), default="app")  # app | linked | preview
    # Model override for app-driven turns in this session (provider's own id, e.g. "openai/gpt-5" or "o3");
    # NULL = the provider's configured default. `variant` = reasoning effort / variant where supported.
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    # original anchor (immutable)
    orig_version_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    orig_quote: Mapped[str] = mapped_column(Text, nullable=False)
    orig_prefix: Mapped[str] = mapped_column(Text, default="")
    orig_suffix: Mapped[str] = mapped_column(Text, default="")
    orig_start: Mapped[int] = mapped_column(Integer, nullable=False)
    orig_end: Mapped[int] = mapped_column(Integer, nullable=False)
    orig_heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    # current anchor (may be updated by reanchoring)
    cur_version_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cur_quote: Mapped[str] = mapped_column(Text, nullable=False)
    cur_prefix: Mapped[str] = mapped_column(Text, default="")
    cur_suffix: Mapped[str] = mapped_column(Text, default="")
    cur_start: Mapped[int] = mapped_column(Integer, nullable=False)
    cur_end: Mapped[int] = mapped_column(Integer, nullable=False)
    cur_heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor_state: Mapped[str] = mapped_column(String(16), default="attached")  # attached|moved|ambiguous|orphaned|document|page
    # set for comments made in the diff review: {base, target, side, line_no, line_text, hunk}
    diff_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # discussions about a page / slide / area of a laid-out document (PDF, Office layout view):
    # {kind: text|page|area|image, page, unit: page|slide, rect, attachments: [file names]}
    target_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AnchorHistory(Base):
    """Every explicit manual reanchor and every automatic move is recorded here."""

    __tablename__ = "anchor_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # manual | auto
    version_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, default="")
    suffix: Mapped[str] = mapped_column(Text, default="")
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | agent | system
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="completed")  # queued|running|completed|failed|cancelled|interrupted
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="turn")  # turn | preview
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    agent_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pending_permission_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_job_events_job_seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReadingState(Base):
    __tablename__ = "reading_state"
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    version_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    offset: Mapped[int] = mapped_column(Integer, default=0)
    scroll_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    active_thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ReviewMark(Base):
    __tablename__ = "review_marks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, default="")
    suffix: Mapped[str] = mapped_column(Text, default="")
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_version_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="reviewed")  # reviewed | needs_review
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Terminal(Base):
    __tablename__ = "terminals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tmux_session: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    tab_order: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="live")  # live | exited | unavailable | closed
    # the named workspace the tab belongs to (NULL: the Default workspace — terminals from before named workspaces)
    named_workspace_id: Mapped[str | None] = mapped_column(ForeignKey("named_workspaces.id"), nullable=True)
    # a machine of the Machines view whose own tmux runs the session (NULL: this server's tmux)
    machine: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    identity: Mapped[str | None] = mapped_column(Text, nullable=True)  # "token" or "github:<login>"; NULL: older logins
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    login_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Lock(Base):
    """A user-declared read-only path. A folder lock covers everything below it. Enforced for every write the
    app performs or drives (agent edits, preview apply, relink); the user's own terminal is never restricted."""

    __tablename__ = "locks"
    __table_args__ = (UniqueConstraint("workspace_id", "path", name="uq_locks_workspace_path"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)  # "" = whole workspace
    is_dir: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FileReview(Base):
    """GitHub-style 'Viewed' checkbox in the diff review: the user marked a file's diff (against `base`) as
    reviewed when its content hash was `reviewed_sha256`. If the file changes afterwards the mark no longer
    matches and the checkbox clears itself."""

    __tablename__ = "file_reviews"
    __table_args__ = (UniqueConstraint("workspace_id", "path", "base", name="uq_file_reviews_ws_path_base"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    base: Mapped[str] = mapped_column(Text, nullable=False)  # the base ref the diff was reviewed against
    reviewed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Review(Base):
    """A GitHub-style review: comments are stashed (`pending`) and submitted together. One agent job then
    triages every comment (address / reply / skip), edits files for the ones to address, and the comments become
    normal discussion threads carrying the agent's verdict."""

    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    base: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)  # "worktree" or a ref
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | submitted | completed | failed | cancelled
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # overall review note from the user
    session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # parsed triage from the agent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewComment(Base):
    __tablename__ = "review_comments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)  # added | deleted | context | mixed
    line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_text: Mapped[str] = mapped_column(Text, nullable=False)
    hunk: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id"), nullable=True)  # created at submission
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)  # address | reply | skip (from the agent)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)  # the agent's per-comment response
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Preview(Base):
    __tablename__ = "previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    base_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_snapshot_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|ready|applied|failed|stale
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DevWorkspace(Base):
    """A PR Development workspace: an app-owned Git worktree of a source repository (new branch or an existing PR's
    branch) on the app server or an SSH host, edited in a VS Code-compatible editor, with a whole wiki folder as the
    agent's reference. Its agent conversation is the hidden document `.waw-dev/<id>.md` of that wiki folder."""

    __tablename__ = "dev_workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False, default="local", server_default="local")  # local | a development.hosts name
    source_repo: Mapped[str] = mapped_column(Text, nullable=False)  # the checkout the worktree is created from (on the host)
    worktree_path: Mapped[str] = mapped_column(Text, nullable=False)  # on the host
    mode: Mapped[str] = mapped_column(String(8), nullable=False)  # branch (new work) | pr (an existing pull request)
    branch: Mapped[str] = mapped_column(Text, nullable=False)  # local branch of the worktree
    base_ref: Mapped[str | None] = mapped_column(Text, nullable=True)  # base branch the work is compared against / PR base
    github_repo: Mapped[str | None] = mapped_column(Text, nullable=True)  # owner/name of the PR target, when known
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    push_remote: Mapped[str | None] = mapped_column(Text, nullable=True)  # remote name or URL `git push` uses
    push_branch: Mapped[str | None] = mapped_column(Text, nullable=True)  # branch name on that remote
    clone_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # cloned into <work_dir>/src/<repo> first (no checkout needed there)
    wiki_workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), nullable=True)  # reference wiki folder
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)  # the agent conversation
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")  # language + task settings
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="creating")  # creating|ready|failed|archived
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DevTaskRun(Base):
    """One build / test / custom command run in a development workspace; its output is a log file in the state dir."""

    __tablename__ = "dev_task_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dev_id: Mapped[str] = mapped_column(ForeignKey("dev_workspaces.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # build | test | custom
    command: Mapped[str] = mapped_column(Text, nullable=False)  # shell command, run in the worktree
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="running")  # running|passed|failed|cancelled|interrupted
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    # run in tmux on a machine: the machine and the run's folder there (its log, pid and exit code); the local log is a copy
    machine: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Machine(Base):
    """A machine (or a bastion it is reached through) the app works on over SSH. Hosts of the config file
    (`source = config`) are mirrored here at startup for their status and probe results; those added in the Machines
    view (`source = ui`) are defined here."""

    __tablename__ = "machines"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="machine")  # machine | bastion
    source: Mapped[str] = mapped_column(String(8), nullable=False, default="ui")  # ui | config
    ssh: Mapped[str] = mapped_column(Text, nullable=False)  # destination: host, user@host or a ~/.ssh/config alias
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    via_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # bastion names, first hop first
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # allowlisted ssh -o options
    work_dir: Mapped[str] = mapped_column(Text, nullable=False, default="~/.cache/wiki-agent-workspace")
    code_server: Mapped[str] = mapped_column(Text, nullable=False, default="code-server")
    opencode: Mapped[str] = mapped_column(Text, nullable=False, default="opencode")
    codex: Mapped[str] = mapped_column(Text, nullable=False, default="codex")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")  # unknown | online | a failure code
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # the last check's hops (JSON)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capabilities_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # the last probe (OS, CPU, GPUs, tools, sudo)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # what the machine gets from this server: {forward_env, env_vars, forward_proxy, ca_bundle} (machines added in the app)
    env_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class MachineJob(Base):
    """Work on a machine as a whole (setup: installing the app's tools), with a log in the state dir."""

    __tablename__ = "machine_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    machine: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # bootstrap
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="running")  # running | passed | failed | cancelled | interrupted
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MachineRecipe(Base):
    """A setup step for machines (machines.recipes): a shell script, an optional check (exit 0: already done) and an
    optional snippet whose effect on the environment (conda activate, setvars.sh) becomes the machine's environment."""

    __tablename__ = "machine_recipes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    run: Mapped[str] = mapped_column(Text, nullable=False)
    check: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_out: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)  # content hash of run + check + env_out
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # the *.sh file it was imported from
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class MachineRecipeState(Base):
    """A recipe assigned to a machine (in order) and how it last went there."""

    __tablename__ = "machine_recipe_state"
    machine: Mapped[str] = mapped_column(Text, primary_key=True)
    recipe_id: Mapped[str] = mapped_column(ForeignKey("machine_recipes.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="not_run")  # not_run | ok | failed
    version_ok: Mapped[str | None] = mapped_column(String(16), nullable=True)  # the recipe's version when it last succeeded
    env_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # the export lines env_out produced there
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_messages_job", Message.job_id)
Index("ix_machine_jobs_machine", MachineJob.machine, MachineJob.started_at)

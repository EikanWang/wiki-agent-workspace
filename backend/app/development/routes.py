"""PR Development API (/api/v1/dev/…) and the integration extension's token-authenticated API (/api/v1/dev/ext/…)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..deps import AppContext, get_ctx, require_auth, require_auth_mutating
from ..errors import ApiError, bad_request, not_found, unauthorized
from ..models import Job, Message, Thread
from ..sessions import ModelChoiceIn
from . import completion
from .agent import KINDS, sync_wiki_snapshot
from .service import DevService, dev_dict

router = APIRouter()


def _svc(ctx: AppContext) -> DevService:
    return DevService(ctx)


def _editor(ctx: AppContext):
    return ctx.extra["dev_editor"]


def _tasks(ctx: AppContext):
    return ctx.extra["dev_tasks"]


class DevCreateIn(BaseModel):
    name: str | None = Field(None, max_length=200)
    host: str = Field("local", max_length=100)
    source_repo: str = Field(min_length=1, max_length=4096)
    mode: str = Field(pattern="^(branch|pr|folder)$")  # folder: work in the given checkout / folder itself
    init_git: bool = True  # folder mode: make a folder that is not in a Git repository one (git init)
    branch: str | None = Field(None, max_length=200)
    base: str | None = Field(None, max_length=200)
    github_repo: str | None = Field(None, max_length=200)
    pr_number: int | None = None
    wiki_workspace_id: str | None = Field(None, max_length=36)
    settings: dict[str, Any] | None = None


class DevUpdateIn(BaseModel):
    name: str | None = Field(None, max_length=200)
    wiki_workspace_id: str | None = Field(None, max_length=36)
    settings: dict[str, Any] | None = None


class CodeAnchorIn(BaseModel):
    """What a discussion is about: lines of a file (a selection, marked on those lines), or a whole file (no lines)."""
    root: str = Field("wt", pattern="^(wt|wiki)$")
    file: str = Field(min_length=1, max_length=4096)
    start_line: int | None = Field(None, ge=1)
    end_line: int | None = Field(None, ge=1)
    start_col: int | None = Field(None, ge=1)
    end_col: int | None = Field(None, ge=1)
    quote: str = Field("", max_length=20_000)
    language: str | None = Field(None, max_length=50)
    folder: bool = False  # `file` is a folder: a discussion about that folder


class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    kind: str = Field("ask", max_length=20)
    thread_id: str | None = Field(None, max_length=36)  # continue this conversation thread; default: a new one
    context: dict[str, Any] | None = None
    session_id: str | None = Field(None, max_length=36)
    anchor: CodeAnchorIn | None = None  # a new discussion about these lines
    model_choice: ModelChoiceIn | None = None  # another model / reasoning level for this discussion
    # a picture that goes with the question: a PDF page / area, or an image (data:image/png;base64,…)
    image: str | None = Field(None, max_length=17_000_000)


class DraftIn(BaseModel):
    paths: list[str] | None = Field(None, max_length=2000)  # a commit message for these files only


class CommitIn(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    paths: list[str] | None = None


class PrCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field("", max_length=60_000)
    draft: bool = False


class TaskIn(BaseModel):
    kind: str = Field("build", pattern="^(build|test|custom)$")
    command: str | None = Field(None, max_length=8000)


class ExtensionsIn(BaseModel):
    host: str = Field("local", max_length=100)
    extensions: list[str] | None = None


# ---------------------------------------------------------------------- overview


@router.get("/dev/info")
def dev_info(ctx: AppContext = Depends(require_auth)):
    return {"editor": _editor(ctx).info(), "hosts": _svc(ctx).hosts(), "completion": completion.info(ctx.settings.development.completion),
            "default_extensions": list(ctx.settings.development.extensions), "kinds": list(KINDS)}


@router.get("/dev/workspaces")
def list_dev(archived: bool = Query(False), ctx: AppContext = Depends(require_auth)):
    return {"workspaces": _svc(ctx).list(include_archived=archived)}


@router.post("/dev/workspaces", status_code=201)
async def create_dev(body: DevCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _svc(ctx).create(body)


@router.get("/dev/workspaces/{dev_id}")
def get_dev(dev_id: str, ctx: AppContext = Depends(require_auth)):
    return dev_dict(_svc(ctx).get(dev_id), ctx)


@router.patch("/dev/workspaces/{dev_id}")
async def update_dev(dev_id: str, body: DevUpdateIn, ctx: AppContext = Depends(require_auth_mutating)):
    out = _svc(ctx).update(dev_id, body)
    row = _svc(ctx).get(dev_id)
    if _editor(ctx).status(dev_id)["state"] == "running":
        await _editor(ctx).write_workspace_file(row)  # the running editor reloads its workspace settings
    return out


@router.delete("/dev/workspaces/{dev_id}")
async def archive_dev(dev_id: str, remove_worktree: bool = Query(False), force: bool = Query(False),
                      ctx: AppContext = Depends(require_auth_mutating)):
    return await _svc(ctx).archive(dev_id, remove_worktree, force)


@router.post("/dev/workspaces/{dev_id}/wiki-snapshot")
async def refresh_wiki(dev_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await sync_wiki_snapshot(ctx, _svc(ctx).get(dev_id), force=True)


# ---------------------------------------------------------------------- editor


@router.post("/dev/workspaces/{dev_id}/editor/start")
async def editor_start(dev_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await _editor(ctx).start(dev_id)


@router.post("/dev/workspaces/{dev_id}/editor/stop")
async def editor_stop(dev_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await _editor(ctx).stop(dev_id)


@router.post("/dev/workspaces/{dev_id}/editor/restart")
async def editor_restart(dev_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await _editor(ctx).restart(dev_id)


@router.get("/dev/extensions")
def extensions_state(host: str = Query("local"), ctx: AppContext = Depends(require_auth)):
    return _editor(ctx).install_state(host)


@router.post("/dev/extensions", status_code=202)
async def install_extensions(body: ExtensionsIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _editor(ctx).install_language_support(body.host, body.extensions)


# ---------------------------------------------------------------------- agent


async def ask(ctx: AppContext, dev_id: str, body: AskIn) -> dict[str, Any]:
    """A turn in the workspace's conversation (a thread of its hidden document), in a new or an existing thread."""
    from ..annotations import MessageIn, ThreadCreateIn, ThreadService
    from dataclasses import replace

    row = _svc(ctx).get(dev_id)
    if body.kind not in KINDS:
        raise bad_request(f"kind must be one of {', '.join(KINDS)}", "INVALID_KIND")
    if body.image:
        body.context = {**(body.context or {}), "_images": [_save_image(ctx, body.image)]}
    if not row.document_id or ctx.workspaces is None:
        raise ApiError(409, "DEV_NOT_READY", "this workspace has no conversation")
    with ctx.db.read() as s:
        from ..models import Document

        doc = s.get(Document, row.document_id)
    if doc is None:
        raise not_found("conversation not found", "DOCUMENT_NOT_FOUND")
    rt = ctx.workspaces.get(doc.workspace_id)
    scoped = replace(ctx, documents=rt.docs, locks=rt.locks, watcher=rt.watcher, workspace_id=rt.id, workspace_root=rt.root)
    svc = ThreadService(scoped)
    if body.thread_id:
        with ctx.db.read() as s:
            t = s.get(Thread, body.thread_id)
        if t is None or t.document_id != doc.id:
            raise not_found("thread not found", "THREAD_NOT_FOUND")
        out = await svc.add_message(body.thread_id, MessageIn(text=body.text, session_id=body.session_id, dev_kind=body.kind,
                                                               dev_context=body.context, model_choice=body.model_choice))
        out["thread_id"] = body.thread_id
        a = _json_target(t)
        if a and a.get("file") and (body.model_choice is not None or not file_sessions(ctx, dev_id).get(f"{a.get('root') or 'wt'}:{a['file']}")):
            set_file_session(ctx, dev_id, a.get("root") or "wt", a["file"], out["session_id"])
        return out
    _, _, sha = rt.docs.current_content(doc)
    context = dict(body.context or {})
    if body.anchor is not None:
        a = body.anchor
        context.setdefault("file", a.file)
        context.setdefault("language", a.language)
        if a.folder:
            context["folder"] = a.file
            context.pop("file", None)
        elif a.start_line:
            context.setdefault("selection", {"start_line": a.start_line, "end_line": a.end_line or a.start_line, "text": a.quote})
        else:  # about the whole file: its content goes with the first turn
            context["whole_file"] = True
            files = [f for f in context.get("files") or [] if isinstance(f, dict)]
            if not any(f.get("root", "wt") == a.root and f.get("path") == a.file for f in files):
                context["files"] = [{"root": a.root, "path": a.file}, *files]
    session_id = body.session_id
    if body.anchor is not None and not session_id:
        # each file has its own agent session: discussions about a file continue in it ("new" the first time)
        session_id = file_session(ctx, dev_id, body.anchor.root, body.anchor.file) or "new"
    out = await svc.create_thread(doc.id, ThreadCreateIn(version_sha256=sha, question=body.text, whole_document=True,
                                                         session_id=session_id,
                                                         dev_kind=body.kind, dev_context=context, model_choice=body.model_choice))
    if body.anchor is not None:
        import json as _json

        set_file_session(ctx, dev_id, body.anchor.root, body.anchor.file, out["session_id"])

        with ctx.db.write() as s:
            t = s.get(Thread, out["thread_id"])
            if t is not None:
                t.target_json = _json.dumps({"kind": "code", **body.anchor.model_dump()}, ensure_ascii=False)
    return out


def _save_image(ctx: AppContext, data_url: str) -> str:
    """Store a picture sent with a question; its path is attached to the agent's turn."""
    import base64
    import re as _re

    from ..db import new_id

    m = _re.match(r"^data:image/(png|jpeg|webp|gif);base64,(.+)$", data_url, _re.S)
    if not m:
        raise bad_request("image must be a data:image/png|jpeg|webp|gif;base64 URL", "INVALID_IMAGE")
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except ValueError:
        raise bad_request("invalid image data", "INVALID_IMAGE")
    d = ctx.settings.snapshots_dir / "attachments" / f"dev-{new_id()}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"image.{'jpg' if m.group(1) == 'jpeg' else m.group(1)}"
    p.write_bytes(raw)
    return str(p)


def _json_target(t: Thread) -> dict[str, Any] | None:
    import json as _json

    try:
        v = _json.loads(t.target_json) if t.target_json else None
    except ValueError:
        return None
    return v if isinstance(v, dict) and v.get("kind") == "code" else None


def _fs_key(dev_id: str) -> str:
    return f"dev_file_sessions:{dev_id}"


def file_sessions(ctx: AppContext, dev_id: str) -> dict[str, str]:
    """{"<root>:<path>": session id} — the agent session of each file's discussions."""
    from ..kv import kv_get

    v = kv_get(ctx, _fs_key(dev_id), {})
    return {k: x for k, x in v.items() if isinstance(x, str)} if isinstance(v, dict) else {}


def file_session(ctx: AppContext, dev_id: str, root: str, path: str) -> str | None:
    """The file's session; else the session of its latest discussion (discussions from before sessions per file)."""
    from ..models import AgentSession, DevWorkspace

    sid = file_sessions(ctx, dev_id).get(f"{root}:{path}")
    with ctx.db.read() as s:
        if sid and s.get(AgentSession, sid) is not None:
            return sid
        row = s.get(DevWorkspace, dev_id)
        if row is None or not row.document_id:
            return None
        for t in s.query(Thread).filter_by(document_id=row.document_id).order_by(Thread.updated_at.desc()).all():
            a = _json_target(t)
            if a and a.get("file") == path and (a.get("root") or "wt") == root and t.last_session_id:
                return t.last_session_id
    return None


def set_file_session(ctx: AppContext, dev_id: str, root: str, path: str, session_id: str | None) -> None:
    from ..kv import kv_put

    m = file_sessions(ctx, dev_id)
    key = f"{root}:{path}"
    if session_id:
        m[key] = session_id
    else:
        m.pop(key, None)
    kv_put(ctx, _fs_key(dev_id), m)


class FileSessionIn(BaseModel):
    root: str = Field("wt", pattern="^(wt|wiki)$")
    file: str = Field(min_length=1, max_length=4096)
    session_id: str | None = Field(None, max_length=36)  # an app session id, "new" (a fresh one), or None (forget)


@router.put("/dev/workspaces/{dev_id}/file-session")
async def put_file_session(dev_id: str, body: FileSessionIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Choose the agent session of a file's discussions (its next new discussion uses it)."""
    from ..sessions import SessionService, session_dict

    row = _svc(ctx).get(dev_id)
    sid = body.session_id
    if sid == "new":
        assert ctx.providers is not None
        provider = ctx.providers.default_provider()
        if not provider:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", "no default agent provider configured")
        cur = file_session(ctx, dev_id, body.root, body.file)
        ss = SessionService(ctx)
        sess = await ss.create_for_provider(provider, f"Dev: {row.name} · {body.file.split('/')[-1]}",
                                            inherit_from=ss.get(cur) if cur else None)
        sid = sess.id
    elif sid:
        SessionService(ctx).get(sid)  # 404 if unknown
    set_file_session(ctx, dev_id, body.root, body.file, sid)
    if not sid:
        return {"session": None}
    return {"session": session_dict(SessionService(ctx).get(sid))}


@router.post("/dev/workspaces/{dev_id}/ask", status_code=201)
async def ask_route(dev_id: str, body: AskIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await ask(ctx, dev_id, body)


@router.get("/dev/workspaces/{dev_id}/conversation")
def conversation(dev_id: str, ctx: AppContext = Depends(require_auth)):
    """The workspace's discussion threads (newest last), with messages."""
    from ..annotations import thread_dict

    row = _svc(ctx).get(dev_id)
    out: list[dict[str, Any]] = []
    with ctx.db.read() as s:
        threads = s.query(Thread).filter_by(document_id=row.document_id).order_by(Thread.created_at).all()
        for t in threads:
            msgs = s.query(Message).filter_by(thread_id=t.id).order_by(Message.created_at).all()
            jobs = {m.id: j for m in msgs if m.job_id and (j := s.get(Job, m.job_id)) is not None}
            out.append(thread_dict(t, msgs, jobs))
    keys = {f"{(a.get('root') or 'wt')}:{a['file']}" for t in out if (a := t.get("target")) and a.get("kind") == "code" and a.get("file")}
    fs = file_sessions(ctx, dev_id)
    for k in keys - set(fs):
        root, _, path = k.partition(":")
        sid = file_session(ctx, dev_id, root, path)
        if sid:
            fs[k] = sid
    return {"threads": out, "document_id": row.document_id, "file_sessions": fs}


@router.post("/dev/workspaces/{dev_id}/draft", status_code=202)
async def draft(dev_id: str, what: str = Query(pattern="^(commit|pr)$"), body: DraftIn | None = None,
                ctx: AppContext = Depends(require_auth_mutating)):
    """Let the agent draft a commit message or a pull request description from the real diff (a job, no thread)."""
    from ..sessions import SessionService

    svc = _svc(ctx)
    row = svc.get(dev_id)
    if what == "commit":
        paths = (body.paths if body else None) or None
        if paths:  # only the files that go into the commit
            parts = [(await svc.diff(dev_id, p))["diff"] for p in paths[:200]]
            d = {"diff": "".join(parts)}
        else:
            d = await svc.diff(dev_id)
        if not d["diff"].strip():
            raise ApiError(409, "NOTHING_TO_COMMIT", "there are no changes")
        prompt = ("Write a Git commit message for the following changes. Rules: first line a concise imperative subject of at most "
                  "72 characters; a blank line; then a short body explaining what changed and why, based only on the diff. Output "
                  "ONLY the message.\n\n```diff\n" + d["diff"][:60000] + "\n```")
    else:
        t = svc.target(row)
        base = row.base_ref or "HEAD~1"
        _, log_text, _ = await t.git(row.worktree_path, ["log", "--format=%s%n%b", f"{base}..HEAD"])
        _, diff_text, _ = await t.git(row.worktree_path, ["diff", "--no-color", f"{base}...HEAD"])
        prompt = ("Write a GitHub pull request title and description for these commits. Output exactly: the title on the first "
                  "line, a blank line, then a Markdown description (what and why, how it was tested, notes for reviewers). Base it "
                  "only on the commits and the diff.\n\nCommits:\n" + log_text[:20000] + "\n\n```diff\n" + diff_text[:50000] + "\n```")
    sess_svc = SessionService(ctx)
    session_id = None
    with ctx.db.read() as s:
        t = s.query(Thread).filter_by(document_id=row.document_id).order_by(Thread.updated_at.desc()).first()
        session_id = t.last_session_id if t else None
    if not session_id:
        provider = ctx.providers.default_provider() if ctx.providers else None
        if not provider:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", "no default agent provider configured")
        session_id = (await sess_svc.create_for_provider(provider, f"Dev drafts: {row.name}")).id
    from .agent import dev_turn

    _, extra = await dev_turn(ctx, row, question="draft", kind="ask", context=None, prior=None)
    assert ctx.jobs is not None
    job_id = ctx.jobs.enqueue_turn(session_id=session_id, thread_id=None, prompt=prompt,
                                   context={k: v for k, v in extra.items() if k in ("_cwd", "_remote", "dev_id")} | {"dev_draft": what},
                                   agent_message_id=None, kind="dev_draft")
    return {"job_id": job_id}


# ---------------------------------------------------------------------- git / PR


@router.get("/dev/workspaces/{dev_id}/status")
async def git_status(dev_id: str, ctx: AppContext = Depends(require_auth)):
    return await _svc(ctx).status(dev_id)


@router.get("/dev/workspaces/{dev_id}/diff")
async def git_diff(dev_id: str, path: str | None = Query(None), ctx: AppContext = Depends(require_auth)):
    return await _svc(ctx).diff(dev_id, path)


@router.get("/dev/workspaces/{dev_id}/compare")
async def git_compare(dev_id: str, base: str = Query("HEAD", max_length=250), target: str = Query("worktree", max_length=250),
                      commit: str | None = Query(None, max_length=250), path: str | None = Query(None, max_length=4096),
                      ctx: AppContext = Depends(require_auth)):
    """The Changes view: changed files between two states, each with its unified diff (see DevService.compare)."""
    return await _svc(ctx).compare(dev_id, base=base, target=target, commit=commit, path=path)


class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    create: bool = True  # a new branch (from the current one, or `start`); false: switch to an existing one
    start: str | None = Field(None, max_length=250)


@router.get("/dev/workspaces/{dev_id}/branches")
async def git_branches(dev_id: str, ctx: AppContext = Depends(require_auth)):
    return await _svc(ctx).branches(dev_id)


@router.post("/dev/workspaces/{dev_id}/branch")
async def git_branch(dev_id: str, body: BranchIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _svc(ctx).branch(dev_id, body.name, body.create, body.start)


@router.get("/dev/workspaces/{dev_id}/refs")
async def git_refs(dev_id: str, ctx: AppContext = Depends(require_auth)):
    return await _svc(ctx).refs(dev_id)


@router.get("/dev/workspaces/{dev_id}/compare/file")
async def git_file_at(dev_id: str, path: str = Query(min_length=1, max_length=4096), ref: str = Query("worktree", max_length=250),
                      ctx: AppContext = Depends(require_auth)):
    """A file's lines in the worktree or at a commit: more context around a change."""
    return await _svc(ctx).file_at(dev_id, path, ref)


@router.post("/dev/workspaces/{dev_id}/commit")
async def git_commit(dev_id: str, body: CommitIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _svc(ctx).commit(dev_id, body.message, body.paths)


@router.post("/dev/workspaces/{dev_id}/push")
async def git_push(dev_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await _svc(ctx).push(dev_id)


@router.post("/dev/workspaces/{dev_id}/pull-request", status_code=201)
async def pr_create(dev_id: str, body: PrCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _svc(ctx).create_pr(dev_id, body.title, body.body, body.draft)


@router.get("/dev/workspaces/{dev_id}/checks")
async def pr_checks(dev_id: str, ctx: AppContext = Depends(require_auth)):
    return await _svc(ctx).checks(dev_id)


# ---------------------------------------------------------------------- tasks


@router.get("/dev/workspaces/{dev_id}/tasks")
def list_tasks(dev_id: str, ctx: AppContext = Depends(require_auth)):
    _svc(ctx).get(dev_id)
    return {"runs": _tasks(ctx).list(dev_id)}


@router.post("/dev/workspaces/{dev_id}/tasks", status_code=201)
async def start_task(dev_id: str, body: TaskIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _tasks(ctx).start(dev_id, body.kind, body.command)


@router.get("/dev/tasks/{run_id}/log")
def task_log(run_id: str, offset: int = Query(0, ge=0), ctx: AppContext = Depends(require_auth)):
    return _tasks(ctx).read_log(run_id, offset)


@router.post("/dev/tasks/{run_id}/cancel")
async def task_cancel(run_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await _tasks(ctx).cancel(run_id)


@router.post("/dev/tasks/{run_id}/fix", status_code=201)
async def task_fix(run_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    """Ask the agent to fix what made this run fail (its output goes with the request)."""
    tr = _tasks(ctx)
    r = tr.get(run_id)
    return await ask(ctx, r.dev_id, AskIn(text=f"The {r.kind} command failed. Find the cause and fix it, then run it again.",
                                          kind="fix", context={"task_command": r.command, "task_output": tr.tail(run_id)}))


# ---------------------------------------------------------------------- the integration extension (token auth)


def ext_auth(dev_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> AppContext:
    """The editor's integration extension: `Authorization: Bearer <token>` handed to its code-server process at start.
    Never accepted with cookies (the extension has none); the token dies with the editor process."""
    h = request.headers.get("authorization", "")
    token = h[7:] if h.lower().startswith("bearer ") else ""
    if not _editor(ctx).check_token(dev_id, token):
        raise unauthorized("invalid editor token")
    return ctx


@router.get("/dev/ext/{dev_id}/info")
def ext_info(dev_id: str, ctx: AppContext = Depends(ext_auth)):
    row = _svc(ctx).get(dev_id)
    return {"id": row.id, "name": row.name, "branch": row.branch, "pr_number": row.pr_number, "github_repo": row.github_repo,
            "completion": completion.info(ctx.settings.development.completion), "kinds": list(KINDS)}


@router.post("/dev/ext/{dev_id}/ask", status_code=201)
async def ext_ask(dev_id: str, body: AskIn, ctx: AppContext = Depends(ext_auth)):
    return await ask(ctx, dev_id, body)


@router.get("/dev/ext/{dev_id}/jobs/{job_id}")
def ext_job(dev_id: str, job_id: str, ctx: AppContext = Depends(ext_auth)):
    with ctx.db.read() as s:
        j = s.get(Job, job_id)
        if j is None or f'"dev_id": "{dev_id}"' not in (j.context_json or ""):
            raise not_found("job not found", "JOB_NOT_FOUND")
        m = s.get(Message, j.agent_message_id) if j.agent_message_id else None
        return {"status": j.status, "text": m.content if m else None, "error": j.error_message, "thread_id": j.thread_id}


class CompleteIn(BaseModel):
    language: str = Field("", max_length=50)
    path: str = Field("", max_length=1000)
    prefix: str = Field("", max_length=100_000)
    suffix: str = Field("", max_length=50_000)


@router.post("/dev/ext/{dev_id}/complete")
async def ext_complete(dev_id: str, body: CompleteIn, ctx: AppContext = Depends(ext_auth)):
    return await completion.complete(ctx.settings.development.completion, language=body.language, path=body.path,
                                     prefix=body.prefix, suffix=body.suffix)


# ---------------------------------------------------------------------- the web editor


class FileWriteIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    text: str = Field(max_length=6 * 1024 * 1024)
    base_sha256: str | None = Field(None, max_length=64)
    create: bool = False


class PathIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    new_path: str | None = Field(None, max_length=4096)


class StatIn(BaseModel):
    root: str = Field("wt", pattern="^(wt|wiki)$")
    paths: list[str] = Field(default_factory=list, max_length=200)


def _files(ctx: AppContext, dev_id: str):
    from .files import DevFiles

    return DevFiles(ctx, dev_id)


@router.get("/dev/workspaces/{dev_id}/roots")
async def file_roots(dev_id: str, ctx: AppContext = Depends(require_auth)):
    return {"roots": await _files(ctx, dev_id).roots()}


@router.get("/dev/workspaces/{dev_id}/fs/list")
async def file_list(dev_id: str, root: str = Query("wt"), path: str = Query(""), ctx: AppContext = Depends(require_auth)):
    return {"items": await _files(ctx, dev_id).list_dir(root, path)}


@router.get("/dev/workspaces/{dev_id}/fs/all")
async def file_all(dev_id: str, root: str = Query("wt"), ctx: AppContext = Depends(require_auth)):
    return {"files": await _files(ctx, dev_id).all_files(root)}


@router.get("/dev/workspaces/{dev_id}/fs/file")
async def file_read(dev_id: str, root: str = Query("wt"), path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    return await _files(ctx, dev_id).read(root, path)


INLINE_TYPES = {".pdf": "application/pdf"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
               ".svg": "image/svg+xml", ".bmp": "image/bmp", ".ico": "image/x-icon"}


@router.get("/dev/workspaces/{dev_id}/fs/raw")
async def file_raw(dev_id: str, root: str = Query("wt"), path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    """An image or PDF of the worktree / wiki, for the editor's viewers (other types are sent as an attachment)."""
    from fastapi.responses import Response

    _, data = await _files(ctx, dev_id).read_bytes(root, path, max_size=200 * 1024 * 1024)
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    ctype = IMAGE_TYPES.get(ext) or INLINE_TYPES.get(ext)
    headers = {"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"}
    if ctype == "image/svg+xml":
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    if not ctype:
        headers["Content-Disposition"] = "attachment"
    return Response(content=data, media_type=ctype or "application/octet-stream", headers=headers)


@router.put("/dev/workspaces/{dev_id}/fs/file")
async def file_write(dev_id: str, body: FileWriteIn, ctx: AppContext = Depends(require_auth_mutating)):
    out = await _files(ctx, dev_id).write(body.path, body.text, body.base_sha256, create=body.create)
    if ctx.events:
        ctx.events.publish("global", {"type": "dev_changed", "dev_id": dev_id, "file": body.path})
    return out


@router.post("/dev/workspaces/{dev_id}/fs/mkdir")
async def file_mkdir(dev_id: str, body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _files(ctx, dev_id).mkdir(body.path)


@router.post("/dev/workspaces/{dev_id}/fs/rename")
async def file_rename(dev_id: str, body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    if not body.new_path:
        raise bad_request("new_path is required", "INVALID_PATH")
    return await _files(ctx, dev_id).rename(body.path, body.new_path)


@router.post("/dev/workspaces/{dev_id}/fs/delete")
async def file_delete(dev_id: str, body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _files(ctx, dev_id).delete(body.path)


@router.post("/dev/workspaces/{dev_id}/fs/stat")
async def file_stat(dev_id: str, body: StatIn, ctx: AppContext = Depends(require_auth)):
    return {"sha256": await _files(ctx, dev_id).stat_many(body.root, body.paths)}


@router.get("/dev/workspaces/{dev_id}/fs/search")
async def file_search(dev_id: str, q: str = Query(..., max_length=1000), root: str = Query("wt"), regex: bool = Query(False),
                      case: bool = Query(False), glob: str | None = Query(None, max_length=200), ctx: AppContext = Depends(require_auth)):
    return await _files(ctx, dev_id).search(root, q, regex=regex, case=case, glob=glob or None)


@router.get("/dev/workspaces/{dev_id}/lsp-info")
async def lsp_info_route(dev_id: str, ctx: AppContext = Depends(require_auth)):
    from .lsp import lsp_info

    return await lsp_info(ctx, dev_id)


@router.post("/dev/workspaces/{dev_id}/complete")
async def web_complete(dev_id: str, body: CompleteIn, ctx: AppContext = Depends(require_auth_mutating)):
    """AI inline completion for the web editor (the same model as the VS Code extension's)."""
    _svc(ctx).get(dev_id)
    return await completion.complete(ctx.settings.development.completion, language=body.language, path=body.path,
                                     prefix=body.prefix, suffix=body.suffix)


@router.get("/dev/workspaces/{dev_id}/skills")
async def dev_skills(dev_id: str, ctx: AppContext = Depends(require_auth)):
    """The skills the agent of this workspace gets: worktree, checkout, reference wiki and every open folder."""
    from .. import skills as sk
    from .agent import local_skill_folders

    svc = _svc(ctx)
    row = svc.get(dev_id)
    t = svc.target(row)
    folders = local_skill_folders(ctx, row)
    note = None if t.is_local else (f"Copied to {t.name} (read-only) before each turn; the worktree's own skills load "
                                    "there natively.")
    return {"skills": sk.discover(folders), "folders": [str(f) for f in folders], "note": note}


# ---------------------------------------------------------------------- terminals in the worktree


def _dev_terms_key(dev_id: str) -> str:
    return f"dev_terminals:{dev_id}"


@router.get("/dev/workspaces/{dev_id}/terminals")
def dev_terminals(dev_id: str, ctx: AppContext = Depends(require_auth)):
    """The workspace's terminals (tmux tabs that survive reloads and restarts; also listed in the Terminal view)."""
    from ..kv import kv_get

    _svc(ctx).get(dev_id)
    assert ctx.terminals is not None
    ids = kv_get(ctx, _dev_terms_key(dev_id), [])
    live = [t for t in ctx.terminals.list() if t["id"] in ids and t["state"] != "closed"]
    return {"terminals": live}


@router.post("/dev/workspaces/{dev_id}/terminals", status_code=201)
def dev_terminal_create(dev_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    """A new shell in the worktree: on this server, or an ssh login on the workspace's host that starts there."""
    from pathlib import Path as _P

    from ..kv import kv_get, kv_put

    svc = _svc(ctx)
    row, t = svc._ready(dev_id)
    assert ctx.terminals is not None
    n = len(dev_terminals(dev_id, ctx)["terminals"]) + 1
    label = f"{row.name[:30]}{f' {n}' if n > 1 else ''}"
    if t.is_local:
        term = ctx.terminals.create(row.worktree_path, label)
    else:
        argv, _, _ = t.spawn_argv(["sh", "-c", 'exec "${SHELL:-sh}" -l'], row.worktree_path, None, ssh_opts=["-tt"])
        # the remote login shell starts in the worktree; tmux runs the ssh command (shell-quoted argv)
        assert all(isinstance(a, str) for a in argv)
        term = ctx.terminals.create(str(_P.home()), f"{label} ({t.name})", command=argv)
    ids = [i for i in kv_get(ctx, _dev_terms_key(dev_id), []) if isinstance(i, str)] + [term["id"]]
    kv_put(ctx, _dev_terms_key(dev_id), ids[-20:])
    return term


# ---------------------------------------------------------------------- navigation without a language server


class NavIn(BaseModel):
    root: str = Field("wt", pattern="^(wt|wiki)$")
    path: str = Field(min_length=1, max_length=4096)
    word: str = Field("", max_length=300)
    language: str = Field("", max_length=50)
    line_text: str = Field("", max_length=20_000)
    col: int = Field(1, ge=1)


@router.post("/dev/workspaces/{dev_id}/nav/definition")
async def nav_definition(dev_id: str, body: NavIn, ctx: AppContext = Depends(require_auth)):
    from .nav import Nav

    return {"locations": await Nav(_files(ctx, dev_id)).definition(body.root, body.path, body.word, body.language, body.line_text, body.col)}


@router.post("/dev/workspaces/{dev_id}/nav/references")
async def nav_references(dev_id: str, body: NavIn, ctx: AppContext = Depends(require_auth)):
    from .nav import Nav

    return {"locations": await Nav(_files(ctx, dev_id)).references(body.root, body.word, body.language)}

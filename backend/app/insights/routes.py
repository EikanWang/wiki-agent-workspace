"""/insights/* — Repo Insights."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..deps import AppContext, require_auth, require_auth_mutating
from ..errors import ApiError
from . import pipeline as pl
from . import settings as st
from . import store

router = APIRouter()


def _repo(owner: str, name: str) -> str:
    r = f"{owner}/{name}"
    if not st.REPO.match(r):
        raise ApiError(400, "INVALID_REPO", "use owner/name")
    return r


@router.get("/insights/settings")
def get_settings(ctx: AppContext = Depends(require_auth)):
    return {**st.load(ctx), "endpoint": st.llm_public(ctx)}


class SettingsIn(BaseModel):
    data_dir: str | None = Field(None, max_length=4096)
    repos: list[dict[str, Any]] | None = None
    llm: dict[str, Any] | None = None
    highlight: dict[str, Any] | None = None
    presets: list[dict[str, Any]] | None = None
    max_chars: int | None = Field(None, ge=20_000, le=20_000_000)
    agent_max_chars: int | None = Field(None, ge=20_000, le=20_000_000)
    concurrency: int | None = Field(None, ge=1, le=16)
    timezone_offset: int | None = Field(None, ge=-12, le=14)
    endpoint: dict[str, Any] | None = None  # {base_url, model, api_key} (the key is never sent back)


@router.put("/insights/settings")
def put_settings(body: SettingsIn, ctx: AppContext = Depends(require_auth_mutating)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None and k != "endpoint"}
    out = st.save(ctx, patch)
    if body.endpoint:
        st.save_secrets(ctx, {k: body.endpoint.get(k) for k in ("base_url", "model", "api_key") if body.endpoint.get(k) is not None})
    return {**out, "endpoint": st.llm_public(ctx)}


@router.get("/insights/repos/{owner}/{name}")
def repo_overview(owner: str, name: str, ctx: AppContext = Depends(require_auth)):
    """The repository's summaries (daily and longer), highlight files and reports."""
    repo = _repo(owner, name)
    rows = store.index(ctx, repo)
    daily = sorted((r for r in rows if r["daily"]), key=lambda r: r["since"], reverse=True)
    longer = sorted((r for r in rows if not r["daily"]), key=lambda r: r["mtime"], reverse=True)
    hls = sorted(store.index(ctx, repo, "highlight"), key=lambda r: r["mtime"], reverse=True)
    read = store.read_new(ctx, repo)
    daily = [{**{k: v for k, v in r.items() if k != "new_names"}, "unread": store.unread(r["new_names"], read.get(f"f:{r['name']}"))} for r in daily[:400]]
    longer = [{k: v for k, v in r.items() if k != "new_names"} for r in longer]
    return {"repo": repo, "config": st.repo_cfg(ctx, repo), "daily": daily, "daily_count": len(daily),
            "first_day": daily[-1]["since"] if daily else None, "last_day": daily[0]["since"] if daily else None,
            "longer": longer[:200], "highlight_files": hls[:200], "reports": pl.reports(ctx, repo),
            "jobs": [_public(j) for j in pl.jobs(repo)][-20:]}


def _under(ctx: AppContext, path: str) -> Path:
    root = st.data_dir(ctx).resolve()
    p = Path(path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ApiError(400, "INVALID_PATH", "not a Repo Insights file")
    if not p.is_file():
        raise ApiError(404, "FILE_NOT_FOUND", "no such file")
    return p


@router.get("/insights/file")
def read_file(path: str = Query(..., max_length=4096), ctx: AppContext = Depends(require_auth)):
    p = _under(ctx, path)
    if p.suffix == ".json":
        import json

        return {"path": str(p), "name": p.name, "json": json.loads(p.read_text())}
    text = p.read_text(encoding="utf-8", errors="replace")
    m = store.HEADER.search(text[:400])
    read = store.read_new(ctx, m["repo"]).get(f"f:{p.name}", []) if m else []
    return {"path": str(p), "name": p.name, "text": text, "repo": m["repo"] if m else None, "read_new": read}


READ_KEY = re.compile(r"^(r:[A-Za-z0-9-]{1,64}|f:[A-Za-z0-9_.-]{1,200})$")


class ReadIn(BaseModel):
    repo: str = Field(..., max_length=200)
    key: str = Field(..., max_length=210)  # a report ("r:<id>") or a summary file ("f:<name>")
    names: list[str] = Field(..., max_length=2000)
    read: bool = True


@router.put("/insights/read")
def mark_read(body: ReadIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Marks new things read (or unread again) in a report or a summary: the lists count only the unread ones."""
    repo = body.repo.strip()
    if not st.REPO.match(repo):
        raise ApiError(400, "INVALID_REPO", "use owner/name")
    if not READ_KEY.match(body.key):
        raise ApiError(400, "INVALID_KEY", "r:<report id> or f:<summary file name>")
    return {"key": body.key, "read_new": store.mark_read(ctx, repo, body.key, [n[:200] for n in body.names], body.read)}


class ReadAllIn(BaseModel):
    repo: str = Field(..., max_length=200)
    keys: list[str] = Field(..., max_length=2000)  # reports ("r:<id>") and summary files ("f:<name>")
    read: bool = True


@router.put("/insights/read-all")
def mark_all_read(body: ReadAllIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Marks every new thing of these reports / summaries read (or unread again): "select all, mark read" in a list."""
    repo = body.repo.strip()
    if not st.REPO.match(repo):
        raise ApiError(400, "INVALID_REPO", "use owner/name")
    bad = [k for k in body.keys if not READ_KEY.match(k)]
    if bad:
        raise ApiError(400, "INVALID_KEY", f"r:<report id> or f:<summary file name>, not {bad[0][:60]!r}")
    files = {r["name"]: r["new_names"] for r in store.index(ctx, repo)}
    items = {k: (pl.report_new_names(ctx, repo, k[2:]) if k.startswith("r:") else files.get(k[2:], [])) for k in dict.fromkeys(body.keys)}
    items = {k: v for k, v in items.items() if v}
    if items:
        store.mark_read_many(ctx, repo, items, body.read)
    return {"marked": len(items), "names": sum(len(v) for v in items.values())}


@router.get("/insights/repos/{owner}/{name}/highlights")
def get_highlights(owner: str, name: str, since: str = Query(...), until: str = Query(...), view: str = Query("flat", pattern="^(flat|day)$"),
                   ctx: AppContext = Depends(require_auth)):
    """XPU highlights of a period: `flat` — each item once (duplicates across days removed) with the days it was on;
    `day` — per day, with the items an earlier day already had marked."""
    repo = _repo(owner, name)
    since, until = _date(since), _date(until)
    if view == "day":
        return {"days": store.highlights_by_day(ctx, repo, since, until)}
    return {"items": store.highlights(ctx, repo, since, until)}


def _date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise ApiError(400, "INVALID_DATE", f"{s!r}: use YYYY-MM-DD")


class ReportIn(BaseModel):
    repo: str = Field(max_length=200)
    period: str = Field("custom", pattern="^(day|week|month|quarter|year|custom)$")
    value: str | None = Field(None, max_length=20)  # day / a day of the week: YYYY-MM-DD; month: YYYY-MM; quarter: YYYY-Qn; year: YYYY
    since: str | None = None
    until: str | None = None
    sources: dict[str, bool] | None = None
    focus: str = Field("", max_length=20000)
    preset: str | None = Field(None, max_length=40)
    baseline: dict[str, str | None] | None = None  # {mode, date}: what new things are compared with (default: the repository's)


@router.post("/insights/reports", status_code=201)
async def create_report(body: ReportIn, ctx: AppContext = Depends(require_auth_mutating)):
    repo = body.repo.strip()
    if not st.REPO.match(repo):
        raise ApiError(400, "INVALID_REPO", "use owner/name")
    since, until, label, kind = pl.calendar_period(body.period, body.value, body.since, body.until)
    if (dt.date.fromisoformat(until) - dt.date.fromisoformat(since)).days > 3700:
        raise ApiError(400, "INVALID_DATE", "at most 10 years")
    cfg = st.repo_cfg(ctx, repo)
    src = {**cfg["sources"], "highlights": cfg["highlights"], **(body.sources or {})}
    focus = body.focus.strip()
    if body.preset:
        p = next((x for x in st.load(ctx)["presets"] if x["id"] == body.preset), None)
        if p:
            focus = (p["text"] + ("\n" + focus if focus else "")).strip()
    baseline = st.baseline_of(body.baseline) if body.baseline is not None else None
    return pl.new_report(ctx, repo, since, until, {k: bool(v) for k, v in src.items()}, focus, label, body.preset, kind, baseline=baseline)


@router.get("/insights/reports/{owner}/{name}/{rid}")
def get_report(owner: str, name: str, rid: str, ctx: AppContext = Depends(require_auth)):
    return pl.report(ctx, _repo(owner, name), rid)


@router.delete("/insights/reports/{owner}/{name}/{rid}")
def delete_report(owner: str, name: str, rid: str, ctx: AppContext = Depends(require_auth_mutating)):
    pl.delete_report(ctx, _repo(owner, name), rid)
    return {"deleted": rid}


class ChatIn(BaseModel):
    workspace_id: str | None = Field(None, max_length=36)
    # a discussion about a part of the report: the selected text, and your question (else a default one)
    quote: str = Field("", max_length=20_000)
    question: str = Field("", max_length=8_000)
    # reference folders for this discussion only (besides the repository's)
    dirs: list[str] = Field(default_factory=list, max_length=30)
    # the material's discussions (e.g. "report:<id>"): one more discussion in the document they share, shown next to the
    # material (instead of a chat of its own)
    discussion: str | None = Field(None, max_length=600)


@router.get("/insights/repos/{owner}/{name}/discussion")
def material_discussion(owner: str, name: str, key: str = Query(..., max_length=600), ctx: AppContext = Depends(require_auth)):
    """The document with the discussions about one report / summary / highlights (null before the first one)."""
    from .chat import discussion_doc

    return {"discussion": discussion_doc(ctx, _repo(owner, name), key.strip())}


@router.post("/insights/reports/{owner}/{name}/{rid}/chat", status_code=201)
async def report_chat(owner: str, name: str, rid: str, body: ChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Continue with the agent: a chat in a wiki folder with the report as its material."""
    return await _chat_on(ctx, _repo(owner, name), [rid], body)


class ChatManyIn(ChatIn):
    ids: list[str] = Field(..., min_length=1, max_length=40)


@router.post("/insights/reports/{owner}/{name}/chat", status_code=201)
async def reports_chat(owner: str, name: str, body: ChatManyIn, ctx: AppContext = Depends(require_auth_mutating)):
    """A chat with several reports as its material (e.g. to compare months)."""
    return await _chat_on(ctx, _repo(owner, name), list(dict.fromkeys(body.ids)), body)


class FileChatIn(ChatIn):
    path: str = Field(..., max_length=4096)


@router.post("/insights/file/chat", status_code=201)
async def file_chat(body: FileChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """A chat about a daily summary (or a highlights file), e.g. a selected part of it."""
    p = _under(ctx, body.path)
    text = p.read_text(encoding="utf-8", errors="replace") if p.suffix == ".md" else ""
    m = store.HEADER.search(text[:400]) or store.HL_HEADER.search(text[:400])
    if not m:
        raise ApiError(409, "NOT_A_SUMMARY", "only a summary or a highlights file can be discussed")
    hl = not store.HEADER.search(text[:400])
    a, b = m["a"], m["b"]
    span = a if (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days <= 1 else f"{a} – {b}"
    mat = {"label": span, "since": a, "until": b, "text": text}
    return await _chat_on(ctx, m["repo"], [], body, mats=[mat], what="highlights" if hl else "summary")


async def _chat_on(ctx: AppContext, repo: str, ids: list[str], body: ChatIn, mats: list[dict[str, Any]] | None = None, what: str = "report"):
    """A chat with reports (`ids`), or other material (`mats`: label, since, until, text), of the repository."""
    from dataclasses import replace

    from ..annotations import ThreadCreateIn, ThreadService
    from ..chats import new_chat
    from ..github_hub import clean_dirs
    from ..kv import kv_get, kv_put
    from .chat import discussion_doc, mark_discussion

    workspace_id = body.workspace_id
    key = (body.discussion or "").strip()
    dirs = clean_dirs(body.dirs)
    quote = body.quote.strip()

    reps = mats if mats is not None else [pl.report(ctx, repo, rid) for rid in ids]
    if any(not r.get("text") for r in reps):
        raise ApiError(409, "REPORT_NOT_READY", "the report is not ready")
    reps.sort(key=lambda r: (r["since"], r["until"]))
    if ctx.workspaces is None:
        raise ApiError(409, "NO_WIKI", "no wiki folder is open")
    shared = discussion_doc(ctx, repo, key) if key else None
    rt = ctx.workspaces.runtimes.get(shared["workspace_id"] if shared else workspace_id) if (shared or workspace_id) else ctx.workspaces.primary
    if rt is None:
        raise ApiError(404, "WORKSPACE_NOT_FOUND", "that wiki folder is not open")
    label = lambda r: r.get("label") or f"{r['since']} – {r['until']}"  # noqa: E731
    noun = {"report": "report", "summary": "daily summary", "highlights": "highlights"}[what]
    head = f"{repo} · {'' if what == 'report' else noun + ' '}{', '.join(label(r) for r in reps)}"
    if quote and not key:
        head += " · “" + " ".join(quote.split())[:60] + ("…”" if len(" ".join(quote.split())) > 60 else "”")
    if shared:
        chat = {**shared, "title": head[:160]}
    else:
        chat = new_chat(rt.docs, head[:160])
        kv_put(ctx, f"chat_insights:{rt.id}:{chat['path']}", repo)  # its repository's reference folders apply
        if key:
            mark_discussion(ctx, repo, key, rt.id, chat["path"])
    if dirs:  # (the discussions of one material share their document, and so these folders)
        had = kv_get(ctx, f"context:chat:{rt.id}:{chat['path']}", []) or []
        kv_put(ctx, f"context:chat:{rt.id}:{chat['path']}", list(dict.fromkeys([*had, *dirs])))
    sctx = replace(ctx, documents=rt.docs, locks=rt.locks, watcher=rt.watcher, workspace_id=rt.id, workspace_root=rt.root)
    opened = rt.docs.open_document(chat["path"])
    room = 380_000 // len(reps)
    parts = [f"<<<REPORT {label(r)} ({r['since']} to {r['until']})\n{r['text'][:room]}\nREPORT>>>" for r in reps]
    brief = (f"Material: {f'the Repo Insights {noun}' if len(reps) == 1 else f'{len(reps)} Repo Insights reports'} of {repo} (made "
             "from GitHub issues, pull requests, RFCs and the git history; 🆕 marks what is new in each):\n" + "\n".join(parts))
    if len(reps) == 1:
        q = f"Here is the {noun} of {repo} for {label(reps[0])}. Give me the three things I should look at first, with their links."
    else:
        q = (f"Here are the reports of {repo} for {', '.join(label(r) for r in reps)}. Compare them: what changed between them, "
             "what is new in each, and the three things I should look at first, with their links.")
    asked = body.question.strip()
    if quote:
        quoted = "\n".join("> " + line for line in quote.splitlines())
        q = (f"About this part of the {noun if len(reps) == 1 else 'reports'}:\n\n{quoted}\n\n"
             + (asked or "Explain it: what it is about, why it matters, and what I should look at, with the links."))
    elif asked:
        q = asked
    t = await ThreadService(sctx).create_thread(chat["document_id"], ThreadCreateIn(version_sha256=opened["sha256"], question=q,
                                                                                   whole_document=True, chat_brief=brief))
    return {**chat, "workspace_id": rt.id, "thread_id": t["thread_id"], "job_id": t["job_id"]}


class HighlightsChatIn(ChatIn):
    since: str = Field(..., max_length=10)
    until: str = Field(..., max_length=10)
    day: str | None = Field(None, max_length=10)  # one day of the per-day view, else every item of the dates once
    min_score: int = Field(0, ge=0, le=100)
    only_new: bool = False  # (per day) leave out what an earlier day had


def _hl_line(x: dict[str, Any]) -> str:
    score = x.get("xpu_relevance_score")
    who = " · ".join(v for v in (x.get("submitter"), x.get("state"), ", ".join(x.get("vendor_involvement") or [])) if v)
    out = f"- [{score if isinstance(score, int) and score >= 0 else '?'}] [{x.get('title') or x.get('url')}]({x.get('url')})"
    out += (f" — {who}" if who else "") + (f" (on {', '.join(x['days'])})" if len(x.get("days") or []) > 1 else "")
    if x.get("body"):
        out += f"\n  {' '.join(str(x['body']).split())}"
    if x.get("xpu_relevance_justification"):
        out += f"\n  Why: {' '.join(str(x['xpu_relevance_justification']).split())}"
    return out


@router.post("/insights/repos/{owner}/{name}/highlights/chat", status_code=201)
async def highlights_chat(owner: str, name: str, body: HighlightsChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """A chat about the XPU highlights shown (a day, or the dates), e.g. a selected part of them."""
    repo = _repo(owner, name)
    since, until = _date(body.since), _date(body.until)

    def keep(x: dict[str, Any]) -> bool:
        score = x.get("xpu_relevance_score")
        return not isinstance(score, int) or score < 0 or score >= body.min_score

    if body.day:
        day = _date(body.day)
        d = next((x for x in store.highlights_by_day(ctx, repo, since, until) if x["day"] == day), None)
        items = [x for x in (d or {}).get("items") or [] if keep(x) and not (body.only_new and x.get("seen_before"))]
        span = day
    else:
        items = [x for x in store.highlights(ctx, repo, since, until) if keep(x)]
        span = since if since == until else f"{since} – {until}"
    if not items:
        raise ApiError(409, "NO_HIGHLIGHTS", "no highlights shown to discuss")
    text = (f"XPU highlights of {repo} for {span} (score ≥ {body.min_score}; each: [XPU relevance score 0–100] the issue / PR, "
            "who opened it · its state · vendors involved, what it is, why it matters for XPU):\n\n" + "\n".join(_hl_line(x) for x in items))
    return await _chat_on(ctx, repo, [], body, mats=[{"label": span, "since": body.day or since, "until": body.day or until, "text": text}],
                          what="highlights")


class TrendChatIn(BaseModel):
    title: str = Field("", max_length=200)


@router.get("/insights/repos/{owner}/{name}/chats")
def trend_chats(owner: str, name: str, ctx: AppContext = Depends(require_auth)):
    """The repository's trend chats (an agent working on its insights data), newest activity first."""
    from ..chats import chat_snapshot_path, chat_title
    from ..models import Document, Message, Thread
    from .chat import listed

    repo = _repo(owner, name)
    out: list[dict[str, Any]] = []
    snaps = ctx.workspaces.primary.docs.snapshots_dir if ctx.workspaces else (ctx.documents.snapshots_dir if ctx.documents else None)
    with ctx.db.read() as s:
        for x in listed(ctx, repo):
            d = s.query(Document).filter_by(workspace_id=x["workspace_id"], path=x["path"]).one_or_none()
            p = chat_snapshot_path(snaps, x["workspace_id"], x["path"]) if snaps else None
            if d is None or d.hidden or p is None or not p.is_file():
                continue
            threads = s.query(Thread).filter_by(document_id=d.id).all()
            n = s.query(Message).filter(Message.thread_id.in_([t.id for t in threads])).count() if threads else 0
            times = [t.updated_at for t in threads if t.updated_at] + ([d.created_at] if d.created_at else [])
            last = max(times) if times else None
            out.append({"path": d.path, "document_id": d.id, "workspace_id": x["workspace_id"], "title": chat_title(p.read_text(encoding="utf-8")),
                        "messages": n, "updated_at": (last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)).isoformat() if last else None})
    out.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    return {"chats": out}


@router.post("/insights/repos/{owner}/{name}/chats", status_code=201)
def new_trend_chat(owner: str, name: str, body: TrendChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """A new, empty trend chat about the repository (its document lives in the primary wiki folder, out of Chats)."""
    from ..chats import new_chat
    from .chat import mark

    repo = _repo(owner, name)
    if ctx.workspaces is None:
        raise ApiError(409, "NO_WIKI", "no wiki folder is open")
    rt = ctx.workspaces.primary
    chat = new_chat(rt.docs, body.title.strip() or f"{repo} · chat")
    mark(ctx, repo, rt.id, chat["path"])
    return {**chat, "workspace_id": rt.id, "repo": repo}


class RunIn(BaseModel):
    repo: str = Field(max_length=200)
    kind: str = Field(pattern="^(daily|highlights)$")
    since: str
    until: str | None = None
    force: bool = False


@router.post("/insights/run", status_code=202)
async def run_now(body: RunIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Make the daily summaries (each day of the range) or the XPU highlights of a range now."""
    repo = body.repo.strip()
    if not st.REPO.match(repo):
        raise ApiError(400, "INVALID_REPO", "use owner/name")
    since, until = _date(body.since), _date(body.until or body.since)
    days = pl.days_of(since, until)
    if len(days) > 62:
        raise ApiError(400, "INVALID_DATE", "at most 62 days at a time")
    j = pl.new_job(body.kind, repo, since=since, until=until)
    pl.start(ctx, j, pl.run_days(ctx, j, repo, body.kind, since, until, body.force),
             durable=("insights.run", {"repo": repo, "kind": body.kind, "since": since, "until": until, "force": body.force}))
    return _public(j)


def _public(j: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in j.items() if k != "on_done"}


@router.get("/insights/jobs/{jid}")
def get_job(jid: str, ctx: AppContext = Depends(require_auth)):
    j = pl.job(jid)
    if j is None:
        raise ApiError(404, "JOB_NOT_FOUND", "no such job (the server restarted?)")
    return _public(j)


@router.get("/insights/status")
def status(ctx: AppContext = Depends(require_auth)):
    from ..kv import kv_get
    from .scheduler import RUNS_KEY

    from .scheduler import plan

    return {"jobs": [_public(j) for j in pl.jobs() if j["status"] == "running"], "runs": kv_get(ctx, RUNS_KEY, {}) or {}, "schedule": plan,
            "data_dir": str(st.data_dir(ctx)), "endpoint": st.llm_public(ctx)}

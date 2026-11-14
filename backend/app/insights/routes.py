"""/insights/* — Repo Insights."""

from __future__ import annotations

import datetime as dt
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
    return {"repo": repo, "config": st.repo_cfg(ctx, repo), "daily": daily[:400], "daily_count": len(daily),
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
    return {"path": str(p), "name": p.name, "text": p.read_text(encoding="utf-8", errors="replace")}


@router.get("/insights/repos/{owner}/{name}/highlights")
def get_highlights(owner: str, name: str, since: str = Query(...), until: str = Query(...), ctx: AppContext = Depends(require_auth)):
    repo = _repo(owner, name)
    return {"items": store.highlights(ctx, repo, _date(since), _date(until))}


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
    return pl.new_report(ctx, repo, since, until, {k: bool(v) for k, v in src.items()}, focus, label, body.preset, kind)


@router.get("/insights/reports/{owner}/{name}/{rid}")
def get_report(owner: str, name: str, rid: str, ctx: AppContext = Depends(require_auth)):
    return pl.report(ctx, _repo(owner, name), rid)


@router.delete("/insights/reports/{owner}/{name}/{rid}")
def delete_report(owner: str, name: str, rid: str, ctx: AppContext = Depends(require_auth_mutating)):
    pl.delete_report(ctx, _repo(owner, name), rid)
    return {"deleted": rid}


class ChatIn(BaseModel):
    workspace_id: str | None = Field(None, max_length=36)


@router.post("/insights/reports/{owner}/{name}/{rid}/chat", status_code=201)
async def report_chat(owner: str, name: str, rid: str, body: ChatIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Continue with the agent: a chat in a wiki folder with the report as its material."""
    from dataclasses import replace

    from ..annotations import ThreadCreateIn, ThreadService
    from ..chats import new_chat

    repo = _repo(owner, name)
    r = pl.report(ctx, repo, rid)
    if not r.get("text"):
        raise ApiError(409, "REPORT_NOT_READY", "the report is not ready")
    if ctx.workspaces is None:
        raise ApiError(409, "NO_WIKI", "no wiki folder is open")
    rt = ctx.workspaces.runtimes.get(body.workspace_id) if body.workspace_id else ctx.workspaces.primary
    if rt is None:
        raise ApiError(404, "WORKSPACE_NOT_FOUND", "that wiki folder is not open")
    chat = new_chat(rt.docs, f"{repo} · {r.get('label') or r['since'] + ' – ' + r['until']}")
    sctx = replace(ctx, documents=rt.docs, locks=rt.locks, watcher=rt.watcher, workspace_id=rt.id, workspace_root=rt.root)
    opened = rt.docs.open_document(chat["path"])
    brief = (f"Material: the Repo Insights report of {repo} from {r['since']} to {r['until']} (made from GitHub issues, pull "
             f"requests, RFCs and the git history):\n<<<REPORT\n{r['text']}\nREPORT>>>")
    q = f"Here is the report of {repo} for {r.get('label')}. Give me the three things I should look at first, with their links."
    t = await ThreadService(sctx).create_thread(chat["document_id"], ThreadCreateIn(version_sha256=opened["sha256"], question=q,
                                                                                   whole_document=True, chat_brief=brief))
    return {**chat, "workspace_id": rt.id, "thread_id": t["thread_id"], "job_id": t["job_id"]}


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

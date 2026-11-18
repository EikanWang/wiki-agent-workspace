"""The daily runs (each repository's own switch, off by default): at its time (UTC+8 by default), the previous UTC day's
summary of issues and PRs and — when on for the repository — its XPU highlights; days missed while the server was off
are caught up (up to a week).

Scheduled reports (each repository's week / month / quarter / year switches, off by default): once a period has
ended, its report — after the repository's time of day and, when its daily run is on, after the period's last day was
summarized (the report then reuses those summaries). Only the latest finished period is made, once; daily runs go
first; everything one at a time and only with enough GitHub quota."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from ..kv import kv_get, kv_put
from . import github as gh
from . import pipeline as pl
from . import settings as st

log = logging.getLogger("waw.insights")
RUNS_KEY = "insights_runs"  # {"owner/repo": "YYYY-MM-DD" (the last day done)}
REPORT_RUNS_KEY = "insights_report_runs"  # {"owner/repo|week": the period's value (e.g. 2026-11-09, 2026-10, 2026-Q3, 2025)}
_task: asyncio.Task | None = None


START_MIN = 0.5  # a scheduled run starts only with at least half of the GitHub quota left (core and graphql)
plan: dict[str, Any] = {"running": None, "due": [], "reports_due": [], "deferred": None, "checked": None}


def last_finished(period: str, day: dt.date) -> str:
    """The value (for pipeline.calendar_period) of the latest `period` that ended on or before `day`."""
    nxt = day + dt.timedelta(days=1)
    if period == "week":
        start = nxt - dt.timedelta(days=nxt.weekday())
        return (start - dt.timedelta(days=1)).isoformat()
    if period == "month":
        last = nxt.replace(day=1) - dt.timedelta(days=1)
        return f"{last.year}-{last.month:02d}"
    if period == "quarter":
        last = dt.date(nxt.year, 3 * ((nxt.month - 1) // 3) + 1, 1) - dt.timedelta(days=1)
        return f"{last.year}-Q{(last.month - 1) // 3 + 1}"
    if period == "year":
        return str(nxt.year - 1)
    raise ValueError(period)


def _reports_due(ctx: Any, cfg: dict[str, Any], local: dt.datetime, target: dt.date, runs: dict[str, str]) -> list[tuple[dict[str, Any], str, str]]:
    done = kv_get(ctx, REPORT_RUNS_KEY, {}) or {}
    out = []
    for r in sorted(cfg["repos"], key=lambda r: r["schedule"].get("time", "05:35")):
        if local.strftime("%H:%M") < r["schedule"].get("time", "05:35"):
            continue
        for p in st.REPORT_PERIODS:
            if not (r.get("reports") or {}).get(p):
                continue
            value = last_finished(p, target)
            if done.get(f"{r['repo']}|{p}") == value:
                continue
            until = pl.calendar_period(p, value)[1]
            if r["schedule"].get("enabled") and (runs.get(r["repo"]) or "") < until:
                continue  # its daily run first: the report reuses the day summaries
            out.append((r, p, value))
    return out


def _quota_ok() -> tuple[bool, str]:
    from .. import gh_limits

    st_ = gh_limits.LIMITER.state()
    for r in ("graphql", "core"):
        b = st_["buckets"].get(r)
        if b and b["limit"] and b["remaining"] / b["limit"] < START_MIN:
            return False, (f"waiting for GitHub: {b['remaining']} of {b['limit']} {r} requests left (a run starts with ≥ "
                           f"{int(START_MIN * 100)}%); the quota resets at {b['reset_at']}")
    if st_["blocked"]:
        return False, "waiting for GitHub's secondary rate limit to end"
    return True, ""


async def tick(ctx: Any, now: dt.datetime | None = None) -> list[str]:
    """Start what is due, at a pace GitHub allows: one repository at a time (in the order of their times), and only
    with enough quota left; each run is background work (≤ 40% of each hourly quota, evenly paced). Returns the
    repository started (or [])."""
    now = now or gh.now()
    cfg = st.load(ctx)
    local = now + dt.timedelta(hours=int(cfg.get("timezone_offset", 8)))
    runs = kv_get(ctx, RUNS_KEY, {}) or {}
    target = (now - dt.timedelta(days=1)).date()  # the last complete UTC day
    due = []
    for r in sorted(cfg["repos"], key=lambda r: r["schedule"].get("time", "05:35")):
        sched = r["schedule"]
        if not sched.get("enabled") or local.strftime("%H:%M") < sched.get("time", "05:35"):
            continue
        last = runs.get(r["repo"])
        if last and last >= target.isoformat():
            continue
        due.append(r)
    reports = _reports_due(ctx, cfg, local, target, runs)
    plan.update(due=[r["repo"] for r in due], reports_due=[f"{r['repo']} {p} {v}" for r, p, v in reports], checked=now.isoformat())
    live = [j for j in pl.jobs() if j.get("scheduled") and j["status"] == "running"]
    if live:
        plan.update(running=live[0]["repo"], deferred=None)
        return []
    plan["running"] = None
    if not due and not reports:
        plan["deferred"] = None
        return []
    ok, why = _quota_ok()
    if not ok:
        if plan.get("deferred") != why:
            log.info("insights schedule: %s (due: %s)", why, ", ".join(plan["due"]))
        plan["deferred"] = why
        return []
    plan["deferred"] = None
    if not due:
        return _start_report(ctx, *reports[0])
    r = due[0]
    last = runs.get(r["repo"])
    first = max(dt.date.fromisoformat(last) + dt.timedelta(days=1), target - dt.timedelta(days=6)) if last else target
    todo = [(first + dt.timedelta(days=i)).isoformat() for i in range((target - first).days + 1)]
    j = pl.new_job("daily", r["repo"], days=todo, scheduled=True)

    async def run(repo=r["repo"], todo=todo, hl=r["highlights"], j=j) -> dict[str, Any]:
        await pl.prefetch_days(ctx, repo, todo, j)  # catch-up days: one search, not one per day
        for d in todo:
            await pl.daily_summary(ctx, repo, d, j)
            if hl:
                await pl.highlights(ctx, repo, d, d, j)  # reads the day's local copies (no second search)
            runs2 = kv_get(ctx, RUNS_KEY, {}) or {}
            runs2[repo] = d
            kv_put(ctx, RUNS_KEY, runs2)
        return {"days": todo}
    pl.start(ctx, j, run())
    plan["running"] = r["repo"]
    log.info("insights schedule: %s, %s", r["repo"], ", ".join(todo))
    return [r["repo"]]


def _start_report(ctx: Any, r: dict[str, Any], period: str, value: str) -> list[str]:
    since, until, label, kind = pl.calendar_period(period, value)
    done = kv_get(ctx, REPORT_RUNS_KEY, {}) or {}
    done[f"{r['repo']}|{period}"] = value  # once: a failed report is not retried by itself (make it again in Reports)
    kv_put(ctx, REPORT_RUNS_KEY, done)
    src = {**r["sources"], "highlights": r["highlights"]}
    pl.new_report(ctx, r["repo"], since, until, {k: bool(v) for k, v in src.items()}, "", label, None, kind, scheduled=True)
    plan["running"] = r["repo"]
    log.info("insights schedule: %s report of %s (%s)", period, r["repo"], label)
    return [f"{r['repo']} {period}"]


async def _github_now() -> dt.datetime | None:
    """GitHub's time (checked at least hourly; `gh api rate_limit` is free), or None while GitHub is not reachable."""
    from .. import gh_limits

    lim = gh_limits.LIMITER
    if lim.clock() - lim.skew_at > 3600 and lim.refresher is not None:
        await asyncio.to_thread(lim.refresher)
    return gh.now() if lim.skew_at else None


async def _loop(ctx: Any) -> None:
    while True:
        try:
            now = await _github_now()
            if now is None:
                plan["deferred"] = "waiting for GitHub's time (this host's clock decides no day by itself)"
            else:
                await tick(ctx, now)
        except Exception:  # noqa: BLE001 - the loop keeps going
            log.exception("insights scheduler")
        await asyncio.sleep(60)


def start(ctx: Any) -> None:
    from .. import gh_queue

    gh_queue.register("insights.report", pl.resume)
    gh_queue.register("insights.run", pl.resume)
    gh_queue.resume_all(ctx)  # what a restart interrupted goes on by itself
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(ctx))


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None

"""The daily runs (each repository's own switch, off by default): at its time (UTC+8 by default), the previous UTC day's
summary of issues and PRs and — when on for the repository — its XPU highlights; days missed while the server was off
are caught up (up to a week)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from ..kv import kv_get, kv_put
from . import pipeline as pl
from . import settings as st

log = logging.getLogger("waw.insights")
RUNS_KEY = "insights_runs"  # {"owner/repo": "YYYY-MM-DD" (the last day done)}
_task: asyncio.Task | None = None


START_MIN = 0.5  # a scheduled run starts only with at least half of the GitHub quota left (core and graphql)
plan: dict[str, Any] = {"running": None, "due": [], "deferred": None, "checked": None}


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
    now = now or dt.datetime.now(dt.timezone.utc)
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
    plan.update(due=[r["repo"] for r in due], checked=now.isoformat())
    live = [j for j in pl.jobs() if j.get("scheduled") and j["status"] == "running"]
    if live:
        plan.update(running=live[0]["repo"], deferred=None)
        return []
    plan["running"] = None
    if not due:
        plan["deferred"] = None
        return []
    ok, why = _quota_ok()
    if not ok:
        if plan.get("deferred") != why:
            log.info("insights schedule: %s (due: %s)", why, ", ".join(plan["due"]))
        plan["deferred"] = why
        return []
    plan["deferred"] = None
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


async def _loop(ctx: Any) -> None:
    while True:
        try:
            await tick(ctx)
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

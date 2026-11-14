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


async def tick(ctx: Any, now: dt.datetime | None = None) -> list[str]:
    """Run what is due; returns the repositories started."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cfg = st.load(ctx)
    local = now + dt.timedelta(hours=int(cfg.get("timezone_offset", 8)))
    runs = kv_get(ctx, RUNS_KEY, {}) or {}
    started = []
    for r in cfg["repos"]:
        sched = r["schedule"]
        if not sched.get("enabled") or local.strftime("%H:%M") < sched.get("time", "05:35"):
            continue
        target = (now - dt.timedelta(days=1)).date()  # the last complete UTC day
        last = runs.get(r["repo"])
        if last and last >= target.isoformat():
            continue
        if any(j["repo"] == r["repo"] and j["kind"] == "daily" and j["status"] == "running" for j in pl.jobs()):
            continue
        first = max(dt.date.fromisoformat(last) + dt.timedelta(days=1), target - dt.timedelta(days=6)) if last else target
        todo = [(first + dt.timedelta(days=i)).isoformat() for i in range((target - first).days + 1)]
        j = pl.new_job("daily", r["repo"], days=todo)

        async def run(repo=r["repo"], todo=todo, hl=r["highlights"], j=j) -> dict[str, Any]:
            for d in todo:
                await pl.daily_summary(ctx, repo, d, j)
                if hl:
                    await pl.highlights(ctx, repo, d, d, j)
                runs2 = kv_get(ctx, RUNS_KEY, {}) or {}
                runs2[repo] = d
                kv_put(ctx, RUNS_KEY, runs2)
            return {"days": todo}
        pl.start(ctx, j, run())
        started.append(r["repo"])
    return started


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

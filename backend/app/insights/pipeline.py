"""The work: daily summaries (from GitHub, gaps filled), XPU highlights, and reports of any period built from them —
weekly building blocks for long periods, the commit history, RFCs — as jobs with progress."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..db import new_id
from ..errors import ApiError
from . import commits as cm
from . import github as gh
from . import llm, prompts, store
from . import settings as st

log = logging.getLogger("waw.insights")

_jobs: dict[str, dict[str, Any]] = {}
_day_locks: dict[str, asyncio.Lock] = {}
WEEK_BLOCKS_AFTER = 14  # longer periods are rolled up week by week


def today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def days_of(since: str, until: str) -> list[str]:
    a, b = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    return [(a + dt.timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


KINDS = {"day": "daily", "week": "weekly", "month": "monthly", "quarter": "quarterly", "year": "yearly", "custom": "dates"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def calendar_period(period: str, value: str | None, since: str | None = None, until: str | None = None) -> tuple[str, str, str, str]:
    """(since, until, label, kind) of a calendar period: a day (YYYY-MM-DD), the week (Mon–Sun) of a day, a month
    (YYYY-MM), a quarter (YYYY-Qn), a year (YYYY), or given dates. A period still going ends today ("so far")."""
    import re

    t = today()
    try:
        if period == "day":
            a = b = dt.date.fromisoformat(value or t.isoformat())
            label = a.isoformat()
        elif period == "week":
            d = dt.date.fromisoformat(value or t.isoformat())
            a = d - dt.timedelta(days=d.weekday())
            b = a + dt.timedelta(days=6)
            iy, iw, _ = a.isocalendar()
            label = f"Week {iw}, {iy} ({a.strftime('%b')} {a.day} – {b.strftime('%b')} {b.day})"
        elif period == "month":
            m = re.fullmatch(r"(\d{4})-(\d{2})", value or t.strftime("%Y-%m"))
            if not m or not 1 <= int(m[2]) <= 12:
                raise ValueError(value)
            a = dt.date(int(m[1]), int(m[2]), 1)
            b = (a.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            label = f"{_MONTHS[a.month - 1]} {a.year}"
        elif period == "quarter":
            m = re.fullmatch(r"(\d{4})-Q([1-4])", value or f"{t.year}-Q{(t.month - 1) // 3 + 1}")
            if not m:
                raise ValueError(value)
            q = int(m[2])
            a = dt.date(int(m[1]), 3 * q - 2, 1)
            b = (dt.date(int(m[1]) + (q == 4), 1 if q == 4 else 3 * q + 1, 1)) - dt.timedelta(days=1)
            label = f"Q{q} {a.year}"
        elif period == "year":
            y = int(value or t.year)
            a, b = dt.date(y, 1, 1), dt.date(y, 12, 31)
            label = str(y)
        elif period == "custom":
            a, b = dt.date.fromisoformat(since or ""), dt.date.fromisoformat(until or "")
            if a > b:
                raise ApiError(400, "INVALID_DATE", "the start is after the end")
            label = f"{a.isoformat()} – {b.isoformat()}"
        else:
            raise ApiError(400, "INVALID_PERIOD", "day, week, month, quarter, year or custom")
    except (ValueError, TypeError):
        raise ApiError(400, "INVALID_DATE", f"{period}: {value or since!r} is not a valid {period}")
    if a > t:
        raise ApiError(400, "INVALID_DATE", "that period has not started yet")
    if b > t:
        b = t
        label += " (so far)"
    return a.isoformat(), b.isoformat(), label, KINDS[period]


def _hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------- jobs

def new_job(kind: str, repo: str, jid: str | None = None, **info: Any) -> dict[str, Any]:
    j = {"id": jid or new_id(), "kind": kind, "repo": repo, "status": "running", "state": "RUNNING", "step": "starting", "log": [], "error": None,
         "started_at": time.time(), "finished_at": None, **info}
    _jobs[j["id"]] = j
    return j


def step(j: dict[str, Any] | None, text: str) -> None:
    if j is not None:
        j["step"] = text
        j["log"] = (j["log"] + [f"{time.strftime('%H:%M:%S')} {text}"])[-200:]


def job(jid: str) -> dict[str, Any] | None:
    return _jobs.get(jid)


def jobs(repo: str | None = None) -> list[dict[str, Any]]:
    return [j for j in _jobs.values() if not repo or j["repo"].lower() == repo.lower()]


def start(ctx: Any, j: dict[str, Any], coro, durable: tuple[str, dict[str, Any]] | None = None, resumed: bool = False) -> None:
    """Run a job in the background, at background priority for GitHub (it waits for the quota rather than spending
    what people need, and says so in its step). durable=(kind, params): kept in the GitHub work queue until it ends, so
    it resumes after a restart."""
    from .. import gh_limits, gh_queue

    if durable and not resumed:  # (a resumed one is in the queue already, with its progress)
        gh_queue.add(ctx, j["id"], durable[0], durable[1])

    def on_wait(info: dict[str, Any] | None) -> None:
        if info is None:
            j["state"], j["gh_wait"] = "RESUMING", None
            step(j, "GitHub allows requests again: going on")
            if durable:
                gh_queue.state(ctx, j["id"], "RESUMING")
            return
        j["state"], j["gh_wait"] = info["state"], {k: info[k] for k in ("resource", "why", "until")}
        step(j, f"waiting for GitHub: {info['why']}; goes on by itself at {time.strftime('%H:%M', time.gmtime(info['until'] + 8 * 3600))} (UTC+8)")
        if durable:
            gh_queue.state(ctx, j["id"], info["state"])

    async def run() -> None:
        with gh_limits.priority(gh_limits.BACKGROUND, wait=True), gh_limits.operation(f"insights:{j['kind']}", on_wait=on_wait):
            try:
                j["result"] = await coro
                j["status"], j["state"] = "done", "RUNNING"
                step(j, "done")
                if durable:
                    gh_queue.done(ctx, j["id"])
            except asyncio.CancelledError:  # the server stops: stays in the queue, resumes at the next start
                j["status"] = "interrupted"
                raise
            except Exception as exc:  # noqa: BLE001 - shown in the job
                j["status"], j["state"] = "failed", "FAILED"
                j["error"] = getattr(exc, "message", None) or f"{exc.__class__.__name__}: {exc}"
                step(j, f"failed: {j['error']}")
                log.warning("insights %s %s failed: %s", j["kind"], j["repo"], j["error"])
                if durable:
                    gh_queue.done(ctx, j["id"])
            finally:
                j["finished_at"] = time.time()
                if j.get("on_done"):
                    j.pop("on_done")(j)
    _tasks.add(t := asyncio.get_running_loop().create_task(run()))
    t.add_done_callback(_tasks.discard)


_tasks: set[asyncio.Task] = set()


async def run_days(ctx: Any, j: dict[str, Any], repo: str, kind: str, since: str, until: str, force: bool = False) -> dict[str, Any]:
    """`Make` / `Score now`: the daily summaries of each day of the range, or the XPU highlights of the range. The days
    done are recorded, so a resumed run goes on with the next one."""
    from .. import gh_queue

    if kind == "highlights":
        return await highlights(ctx, repo, since, until, j)
    done_days = set(next((o["progress"].get("done", []) for o in gh_queue.pending(ctx) if o["id"] == j["id"]), []))
    days = days_of(since, until)
    for d in days:
        if d in done_days:
            continue
        await daily_summary(ctx, repo, d, j, force=force)
        done_days.add(d)
        gh_queue.progress(ctx, j["id"], done=sorted(done_days))
    return {"days": days}


def resume(ctx: Any, op: dict[str, Any]) -> None:
    """gh_queue resumer for Insights work interrupted by a restart."""
    p = op["params"]
    if op["kind"] == "insights.report":
        try:
            meta = json.loads((_reports_dir(ctx, p["repo"]) / f"{op['id']}.json").read_text())
        except (OSError, ValueError):
            from .. import gh_queue

            gh_queue.done(ctx, op["id"])
            return
        if meta.get("status") == "done":
            from .. import gh_queue

            gh_queue.done(ctx, op["id"])
            return
        meta["status"] = "running"
        _meta_write(ctx, meta)
        j = new_job("report", p["repo"], jid=op["id"], since=meta["since"], until=meta["until"])
        step(j, f"resumed after a restart (resume {op['resumes']})")
        j["on_done"] = _report_done(ctx, meta)
        start(ctx, j, make_report(ctx, meta, j), durable=(op["kind"], p), resumed=True)
    elif op["kind"] == "insights.run":
        j = new_job(p["kind"], p["repo"], jid=op["id"], since=p["since"], until=p["until"])
        step(j, f"resumed after a restart (resume {op['resumes']})")
        start(ctx, j, run_days(ctx, j, p["repo"], p["kind"], p["since"], p["until"], p.get("force", False)), durable=(op["kind"], p), resumed=True)


def _report_done(ctx: Any, meta: dict[str, Any]):
    def done(job_: dict[str, Any]) -> None:
        if job_["status"] == "failed":
            meta.update(status="failed", error=job_["error"], finished_at=time.time())
            _meta_write(ctx, meta)
    return done


# ---------------------------------------------------------------------- daily summaries

async def daily_summary(ctx: Any, repo: str, day: str, j: dict[str, Any] | None = None, force: bool = False) -> str:
    """The day's summary of issues and PRs: the newest file of that day, else made now (a day not over yet is made each
    time and kept as .partial)."""
    lock = _day_locks.setdefault(f"{repo.lower()}|{day}", asyncio.Lock())
    async with lock:
        hit = store.daily(ctx, repo, day)
        if hit and not force:
            return store.body(hit["path"])
        step(j, f"issues and PRs of {day}: reading GitHub")
        items = await gh.items_of_day(ctx, repo, day)
        a, b = gh.window(day)
        if not items:
            text = f"No notable activity in issues and pull requests on {day}."
        else:
            step(j, f"issues and PRs of {day}: summarizing {len(items)} item(s)")
            text = await llm.summarize(ctx, "daily", [gh.text_of(it, a, b) for it in items], prompts.DAILY, prompts.COMBINE)
        partial = dt.date.fromisoformat(day) >= today()
        if partial:
            for old in store.index(ctx, repo):
                if old["daily"] and old["since"] == day and old["partial"]:
                    Path(old["path"]).unlink(missing_ok=True)
        store.write_summary(ctx, repo, day, day, text, partial=partial)
        return text


async def ensure_dailies(ctx: Any, repo: str, days: list[str], j: dict[str, Any] | None) -> dict[str, str]:
    """The summaries of these days; missing past days are fetched from GitHub in one go, then summarized."""
    have = {d: store.daily(ctx, repo, d) for d in days}
    missing = [d for d in days if not have[d] and dt.date.fromisoformat(d) <= today()]
    fetched = st.data_dir(ctx) / "items" / repo / "days"
    unfetched = [d for d in missing if not (fetched / f"{d}.json").is_file() and dt.date.fromisoformat(d) < today()]
    if len(unfetched) > 1:
        step(j, f"fetching issues and PRs of {unfetched[0]} … {unfetched[-1]} from GitHub ({len(unfetched)} days without a summary)")
        await gh.fetch_range(ctx, repo, unfetched[0], unfetched[-1])
    out: dict[str, str] = {}
    for i, d in enumerate(days):
        if dt.date.fromisoformat(d) > today():
            continue
        if have[d]:
            out[d] = store.body(have[d]["path"])
        else:
            step(j, f"day {i + 1}/{len(days)} ({d}): no summary yet — making it")
            out[d] = await daily_summary(ctx, repo, d, j)
    return out


# ---------------------------------------------------------------------- building blocks

def _block_dir(ctx: Any, repo: str) -> Path:
    p = st.data_dir(ctx) / "blocks" / repo
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _cached_block(ctx: Any, repo: str, name: str, make) -> str:
    p = _block_dir(ctx, repo) / f"{name}.md"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    text = await make()
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return text


def weeks(days: list[str]) -> list[list[str]]:
    """The days in calendar weeks (Mon–Sun; the first and last may be partial) — the same weeks in every report."""
    out: list[list[str]] = []
    for d in days:
        if out and dt.date.fromisoformat(d).weekday() != 0:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def months(days: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    for d in days:
        if out and out[-1][0][:7] == d[:7]:
            out[-1].append(d)
        else:
            out.append([d])
    return out


MONTH_BLOCKS_AFTER = 92  # longer periods: each month is summarized from its weeks first


async def issue_material(ctx: Any, repo: str, since: str, until: str, j: dict[str, Any] | None) -> tuple[str, int]:
    """The issues-and-PRs material of a period: up to two weeks the daily summaries themselves; up to a quarter one
    summary per calendar week (made from that week's daily summaries); longer, one per month (made from its weeks).
    Finished weeks and months are kept and reused by every later report."""
    days = days_of(since, until)
    dailies = await ensure_dailies(ctx, repo, days, j)
    if len(days) <= WEEK_BLOCKS_AFTER:
        return "\n\n".join(f"--- Daily summary of {d} ---\n{t}" for d, t in dailies.items()), len(dailies)
    groups = months(days) if len(days) > MONTH_BLOCKS_AFTER else [days]
    total_weeks = sum(len(weeks(g)) for g in groups)
    k = 0
    parts = []
    for gi, g in enumerate(groups):
        week_texts: list[tuple[list[str], str]] = []
        for w in weeks(g):
            k += 1
            texts = [f"--- Daily summary of {d} ---\n{dailies[d]}" for d in w if d in dailies]
            if not texts:
                continue

            async def make(w=w, texts=texts, k=k) -> str:
                step(j, f"week {k} of {total_weeks} ({w[0]} … {w[-1]}): condensing its {len(texts)} daily summaries into one "
                        f"week summary (the report is then written from the week summaries of the whole {since} … {until})")
                return await llm.summarize(ctx, "rollup", texts, prompts.rollup(len(texts), "daily", repo, w[0], w[-1], len(w)), prompts.COMBINE)
            done = dt.date.fromisoformat(w[-1]) < today()
            text = await (_cached_block(ctx, repo, f"week_{w[0]}_{w[-1]}_{_hash(*texts)}", make) if done else make())
            week_texts.append((w, text))
        if len(groups) == 1:
            parts += [f"--- Week {w[0]} … {w[-1]} ---\n{t}" for w, t in week_texts]
            continue
        if not week_texts:
            continue
        texts = [f"--- Week {w[0]} … {w[-1]} ---\n{t}" for w, t in week_texts]

        async def make_m(g=g, texts=texts, gi=gi) -> str:
            step(j, f"month {gi + 1} of {len(groups)} ({g[0][:7]}): condensing its {len(texts)} week summaries into one month summary")
            return await llm.summarize(ctx, "rollup", texts, prompts.rollup(len(texts), "weekly", repo, g[0], g[-1], len(g)), prompts.COMBINE)
        done = dt.date.fromisoformat(g[-1]) < today()
        text = await (_cached_block(ctx, repo, f"month_{g[0]}_{g[-1]}_{_hash(*texts)}", make_m) if done else make_m())
        parts.append(f"--- Month {g[0][:7]} ({g[0]} … {g[-1]}) ---\n{text}")
    return "\n\n".join(parts), len(dailies)


async def commit_material(ctx: Any, repo: str, since: str, until: str, j: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    step(j, "commits: updating the local history")
    data = await cm.log_of(ctx, repo, since, until)
    step(j, f"commits: {len(data['commits'])} in the period")
    if not data["commits"]:
        return "", data
    days = days_of(since, until)
    if len(days) <= WEEK_BLOCKS_AFTER or len(data["commits"]) <= 1500:
        return cm.text_of(repo, data), data
    parts = []
    ws = weeks(days)
    for i, w in enumerate(ws):
        rows = [c for c in data["commits"] if w[0] <= c[1] <= w[-1]]
        if not rows:
            continue
        sub = {**data, "commits": rows, "areas": []}

        async def make(w=w, sub=sub, i=i) -> str:
            step(j, f"commits, week {i + 1} of {len(ws)} ({w[0]} … {w[-1]}): summarizing its {len(sub['commits'])} commits")
            return await llm.complete(ctx, "commits", prompts.COMMITS.format(repo=repo, since=w[0], until=w[-1]) + cm.text_of(repo, sub))
        key = f"commits_{w[0]}_{w[-1]}_{_hash(*(c[0] for c in rows))}"
        text = await (_cached_block(ctx, repo, key, make) if dt.date.fromisoformat(w[-1]) < today() else make())
        parts.append(f"--- Commits of {w[0]} … {w[-1]} ({len(rows)}) ---\n{text}")
    areas = "\n".join(f"{n:6d}  {a}" for n, a in data["areas"])
    return f"Files changed per area over the period:\n{areas}\n\n" + "\n\n".join(parts), data


# ---------------------------------------------------------------------- reports

def _reports_dir(ctx: Any, repo: str) -> Path:
    p = st.data_dir(ctx) / "reports" / repo
    p.mkdir(parents=True, exist_ok=True)
    return p


def _meta_write(ctx: Any, meta: dict[str, Any]) -> None:
    p = _reports_dir(ctx, meta["repo"]) / f"{meta['id']}.json"
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False))
    os.replace(tmp, p)


def reports(ctx: Any, repo: str | None = None) -> list[dict[str, Any]]:
    root = st.data_dir(ctx) / "reports"
    out = []
    for p in (root.glob(f"{repo}/*.json") if repo else root.glob("*/*/*.json")):
        try:
            m = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        m.setdefault("kind", "daily" if m.get("since") == m.get("until") else "dates")  # reports made before categories
        live = _jobs.get(m.get("job_id") or "")
        if live and m.get("status") == "running":
            m = {**m, "step": live["step"], "status": live["status"], "state": live.get("state")}
        elif m.get("status") == "running" and not live:
            m = {**m, "status": "interrupted"}
        out.append(m)
    return sorted(out, key=lambda m: m.get("created_at") or "", reverse=True)


def report(ctx: Any, repo: str, rid: str) -> dict[str, Any]:
    p = _reports_dir(ctx, repo) / f"{rid}.json"
    if not p.is_file():
        raise ApiError(404, "REPORT_NOT_FOUND", "no such report")
    m = json.loads(p.read_text())
    md = p.with_suffix(".md")
    live = _jobs.get(m.get("job_id") or "")
    if live:
        m.update(step=live["step"], log=live["log"], status=live["status"] if m["status"] == "running" else m["status"],
                 state=live.get("state"), gh_wait=live.get("gh_wait"))
    return {**m, "text": md.read_text(encoding="utf-8") if md.is_file() else None}


def delete_report(ctx: Any, repo: str, rid: str) -> None:
    for ext in (".json", ".md"):
        (_reports_dir(ctx, repo) / f"{rid}{ext}").unlink(missing_ok=True)


async def make_report(ctx: Any, meta: dict[str, Any], j: dict[str, Any]) -> dict[str, Any]:
    repo, since, until = meta["repo"], meta["since"], meta["until"]
    src = meta["sources"]
    cfg = st.repo_cfg(ctx, repo)
    focus = meta.get("focus") or ""
    sections: list[str] = []
    facts = [f"Period: {since} to {until} ({len(days_of(since, until))} days)"]
    has: dict[str, bool] = {}
    if src.get("issues"):
        text, n = await issue_material(ctx, repo, since, until, j)
        facts.append(f"Days with a summary of issues and PRs: {n}")
        if text.strip():
            has["issues"] = True
            sections.append(f"### Issues and pull requests (summaries of the period)\n{text}")
    if src.get("commits"):
        try:
            text, data = await commit_material(ctx, repo, since, until, j)
            facts.append(f"Commits on the default branch: {len(data['commits'])} by {data['authors']} authors")
            if data.get("note"):
                facts.append(f"Note: {data['note']}")
            if text:
                has["commits"] = True
                sections.append(f"### Commits\n{text}")
        except ApiError as exc:
            facts.append(f"Commits: unavailable ({exc.message})")
    if src.get("rfcs"):
        step(j, "RFCs: reading GitHub")
        items = await gh.rfcs(ctx, cfg, since, until)
        a, b = f"{since}T00:00:00Z", f"{until}T23:59:59Z"
        facts.append(f"RFCs active in the period: {len(items)}")
        if items:
            has["rfcs"] = True
            sections.append("### RFCs\n" + "\n\n".join(gh.rfc_text(it, a, b) for it in items[:150]))
    if src.get("highlights"):
        th = st.load(ctx)["highlight"]["threshold"]
        hl = [x for x in store.highlights(ctx, repo, since, until) if x.get("xpu_relevance_score", -1) >= th]
        facts.append(f"XPU highlights scored ≥ {th}: {len(hl)}")
        if hl:
            has["highlights"] = True
            sections.append("### XPU highlights (score, item, why)\n" + "\n".join(
                f"- {x['xpu_relevance_score']} — [{x.get('title')}]({x['url']}) — {str(x.get('xpu_relevance_justification'))[:400]}"
                for x in hl[:120]))
    if not sections:
        text = "No activity found for this period."
    else:
        step(j, "writing the report")
        ins = prompts.report(repo, since, until, len(days_of(since, until)), focus, has)
        material = ["### Facts\n" + "\n".join(f"- {f}" for f in facts), *sections]
        text = await llm.summarize(ctx, "report", material, ins, combine=ins + "\n(These are partial reports of the same period: merge them into one.)\n")
    md = _reports_dir(ctx, repo) / f"{meta['id']}.md"
    md.write_text(f"Report of {repo} from {since} to {until}:\n\n{text.strip()}\n", encoding="utf-8")
    meta.update(status="done", finished_at=time.time(), facts=facts)
    _meta_write(ctx, meta)
    return {"report": meta["id"]}


def new_report(ctx: Any, repo: str, since: str, until: str, sources: dict[str, bool], focus: str, label: str, preset: str | None,
               kind: str = "dates") -> dict[str, Any]:
    j = new_job("report", repo, since=since, until=until)
    meta = {"id": j["id"], "job_id": j["id"], "repo": repo, "since": since, "until": until, "label": label, "kind": kind, "sources": sources,
            "focus": focus, "preset": preset, "status": "running", "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    _meta_write(ctx, meta)
    j["on_done"] = _report_done(ctx, meta)
    start(ctx, j, make_report(ctx, meta, j), durable=("insights.report", {"repo": repo}))
    return meta


# ---------------------------------------------------------------------- XPU highlights

def rules_match(it: dict[str, Any], diff: str, ccer: int) -> bool:
    """The keyword / Intel rules (an item that matches is a highlight whatever its score)."""
    import re

    title_kw = ["xpu", "xccl", "gpu_type", "ntel.*distributed", "ntel.*Triton", "mkl", "oneDNN", "mkldnn", "torchgen"]
    desc_kw = ["xpu", "xccl", "gpu_type", "ntel *distributed", "ntel *Triton", "oneDNN", "mkldnn"]
    has = lambda pats, text: any(re.search(p, text or "", re.IGNORECASE) for p in pats)  # noqa: E731
    vend = gh.vendor_involvement(it)
    if has(title_kw, it["title"]) or has(desc_kw, it["body"]) or any("xpu" in lb.lower() for lb in it["labels"]):
        return True
    if "Intel" in vend:
        return True
    users = [u.lower() for u in gh.INTEL_LOGINS]
    if any(u in c["body"].lower() for c in it["comments"] for u in users):
        return True
    if any((r.get("author") or "") in gh.INTEL_LOGINS for r in it["reviews"]):
        return True
    return "native_functions.yaml" in diff


async def highlights(ctx: Any, repo: str, since: str, until: str, j: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score every item active in the period for XPU relevance; keep those the rules select, those scored ≥ threshold
    and those the model could not score; one-sentence summaries; written as a highlight file."""
    cfg = st.load(ctx)
    th, ccer = cfg["highlight"]["threshold"], cfg["highlight"]["number_of_ccer"]
    a, b = f"{since}T00:00:00Z", f"{until}T23:59:59Z"
    step(j, f"highlights {since} … {until}: reading GitHub")
    items = [it for it in await gh.fetch_range(ctx, repo, since, until) if gh.active(it, a, b)]
    sem = asyncio.Semaphore(max(1, int(cfg["concurrency"])))
    done = 0

    async def one(it: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal done
        async with sem:
            diff = await asyncio.to_thread(gh.diff_of, ctx, repo, it["number"]) if it["kind"] == "pr" else ""
            prompt = f"{prompts.SCORE}\n\n<<<BEGIN>>>\nTitle: {it['title']}\nDescription: {it['body'][:20000]}\nTags: {', '.join(it['labels'])}\nDiff: {diff}\n<<<END>>>\n"
            try:
                sc = llm.parse_score(await llm.complete(ctx, "highlight", prompt, as_json=True, thinking=True))
            except ApiError as exc:
                sc = {"score": -1, "justification": exc.message}
            done += 1
            step(j, f"highlights: scored {done}/{len(items)}")
            if not (rules_match(it, diff, ccer) or sc["score"] >= th or sc["score"] < 0):
                return None
            body = it["body"]
            if body.strip():
                try:
                    body = await llm.complete(ctx, "highlight", f"{prompts.BODY}\n<<<BEGIN>>>\n{it['body'][:20000]}\n<<<END>>>")
                except ApiError:
                    body = it["body"][:500]
            return {"title": it["title"], "url": it["url"], "xpu_relevance_score": sc["score"], "xpu_relevance_justification": sc["justification"],
                    "body": body.strip() or it["title"], "submitter": it.get("author"), "state": (it.get("state") or "").lower(),
                    "vendor_involvement": sorted(gh.vendor_involvement(it))}
    kept = [x for x in await asyncio.gather(*(one(it) for it in items)) if x]
    kept.sort(key=lambda x: (x["xpu_relevance_score"] >= 0, x["xpu_relevance_score"]), reverse=True)
    p = store.write_highlights(ctx, repo, since, until, kept)
    return {"file": p.name, "scored": len(items), "kept": len(kept)}

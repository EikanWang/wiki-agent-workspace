"""PR Review priority: levels you define, hard rules, and the agent's judgement from your descriptions of each level.

A PR's level, in order:
1. set by hand (`priority_manual`);
2. otherwise the most urgent of the hard rules' level (most urgent matching rule) and the agent's level (the level
   whose descriptions the agent judged to fit; it may say none);
3. otherwise the default level.

The agent reads the PR's metadata, description, changed files and its diff. A small diff (≤ SMALL_DIFF characters) goes
into the decision as it is; a larger one is first summarized file by file (batches of ≤ BATCH_CHARS, a bigger file in
parts, at most MAX_FILES files; lock / vendored / generated / binary files only named), and the decision reads the
summaries. File summaries are cached by the hash of the file's patch, decisions by a fingerprint of the PR and of your
levels' descriptions, so only new or changed PRs (or files) cost agent turns. Turns run in a scratch folder of the state
directory, read-only (Codex sandbox read-only), in fresh sessions (origin "priority", hidden from session pickers).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ApiError
from .kv import kv_get, kv_put

log = logging.getLogger("waw.priority")

SMALL_DIFF = 12_000      # characters: a smaller diff goes into the decision as it is
BATCH_CHARS = 25_000     # characters of patch per summarization turn (a bigger file is split into parts)
MAX_FILES = 60           # files summarized per PR (the biggest changes); the rest are listed by name
BODY_CHARS = 3_000
CONCURRENCY = 2          # PRs classified at the same time
TURN_TIMEOUT = 900.0

DEFAULT_LEVELS: list[dict[str, Any]] = [
    {"id": "P0", "name": "Urgent", "color": "#cf222e", "descriptions": []},
    {"id": "P1", "name": "High", "color": "#d1600f", "descriptions": []},
    {"id": "P2", "name": "Normal", "color": "#0969da", "descriptions": []},
    {"id": "P3", "name": "Low", "color": "#6e7781", "descriptions": []},
]
DEFAULT_CATEGORIES: list[dict[str, str]] = [
    {"key": "todo", "title": "Not reviewed yet", "level": "top"},
    {"key": "news", "title": "New activity since your comments", "level": "high"},
    {"key": "quiet", "title": "Commented, nothing new", "level": "medium"},
]
DEFAULT_AGENT: dict[str, Any] = {"enabled": False, "summary": {"provider": None, "model": None, "variant": None},
                                 "decision": {"provider": None, "model": None, "variant": None}}

# hard-rule fields: kind decides the operators and the value's type
FIELDS: dict[str, str] = {
    "requested_by": "list", "requested_by_meta": "bool", "author": "list", "repo": "list", "label": "list",
    "title": "text", "size": "number", "age_days": "number", "idle_days": "number", "ci": "list",
    "draft": "bool", "review_decision": "list", "my_activity": "list",
}
OPS = {"list": ("in", "not_in"), "text": ("contains", "not_contains"), "number": (">=", "<="), "bool": ("is",)}
CI_VALUES = {"failing": ("FAILURE", "ERROR"), "passing": ("SUCCESS",), "running": ("PENDING", "EXPECTED"), "none": (None,)}

RESULTS_KEY = "pr_priority_results"
SUMMARIES_KEY = "pr_file_summaries"


# ---------------------------------------------------------------------- preferences

def normalize(prefs: dict[str, Any]) -> dict[str, Any]:
    """Fill in the priority settings; older settings (★ / un-★, Meta maintainers, priority people) become levels set
    by hand and hard rules the first time."""
    p = dict(prefs)
    if not p.get("priority_levels"):
        p["priority_levels"] = [dict(x, descriptions=list(x["descriptions"])) for x in DEFAULT_LEVELS]
        p.setdefault("priority_default", "P2")
        rules: list[dict[str, Any]] = []
        if p.get("priority_meta", True):
            rules.append({"id": "meta", "enabled": True, "level": "P1", "conditions": [{"field": "requested_by_meta", "op": "is", "value": True}]})
        if p.get("priority_requesters"):
            rules.append({"id": "people", "enabled": True, "level": "P1",
                          "conditions": [{"field": "requested_by", "op": "in", "value": list(p["priority_requesters"])}]})
        p.setdefault("priority_rules", rules)
        manual = dict(p.get("priority_manual") or {})
        for k in p.get("starred") or []:
            manual.setdefault(k.lower(), "P1")
        for k in p.get("unstarred") or []:
            manual.setdefault(k.lower(), p.get("priority_default") or "P2")
        p["priority_manual"] = manual
    ids = [lv["id"] for lv in p["priority_levels"]]
    if p.get("priority_default") not in ids:
        p["priority_default"] = ids[len(ids) // 2] if ids else None
    p.setdefault("priority_rules", [])
    p.setdefault("priority_manual", {})
    p["priority_agent"] = {**DEFAULT_AGENT, **(p.get("priority_agent") or {})}
    cats = p.get("categories") or []
    known = {c["key"] for c in cats if isinstance(c, dict)}
    p["categories"] = [c for c in cats if isinstance(c, dict) and c.get("key") in {d["key"] for d in DEFAULT_CATEGORIES}] + \
        [dict(d) for d in DEFAULT_CATEGORIES if d["key"] not in known]
    return p


def _err(msg: str) -> ApiError:
    return ApiError(400, "INVALID_PRIORITY", msg)


def clean_levels(levels: Any) -> list[dict[str, Any]]:
    if not isinstance(levels, list) or not 1 <= len(levels) <= 12:
        raise _err("give 1 to 12 levels")
    out, seen = [], set()
    for lv in levels:
        if not isinstance(lv, dict):
            raise _err("a level is {id, name, color, descriptions}")
        lid = str(lv.get("id") or "").strip()
        if not re.fullmatch(r"[\w.+-]{1,12}", lid) or lid.lower() in seen:
            raise _err(f"level id {lid!r}: 1-12 letters/digits, unique")
        seen.add(lid.lower())
        color = str(lv.get("color") or "#6e7781").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise _err(f"level {lid}: colour as #rrggbb")
        descs = [str(d).strip()[:1000] for d in (lv.get("descriptions") or []) if str(d).strip()]
        if len(descs) > 30:
            raise _err(f"level {lid}: at most 30 descriptions")
        out.append({"id": lid, "name": str(lv.get("name") or lid).strip()[:40], "color": color, "descriptions": descs})
    return out


def clean_rules(rules: Any, level_ids: list[str]) -> list[dict[str, Any]]:
    if not isinstance(rules, list) or len(rules) > 100:
        raise _err("at most 100 rules")
    out = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or r.get("level") not in level_ids:
            raise _err(f"rule {i + 1}: give one of the levels {', '.join(level_ids)}")
        conds = []
        for c in r.get("conditions") or []:
            f, op, v = (c or {}).get("field"), (c or {}).get("op"), (c or {}).get("value")
            kind = FIELDS.get(f)
            if kind is None or op not in OPS[kind]:
                raise _err(f"rule {i + 1}: unknown condition {f!r} {op!r}")
            if kind == "list":
                vals = v if isinstance(v, list) else str(v or "").split(",")
                v = [str(x).strip() for x in vals if str(x).strip()][:200]
                if not v:
                    raise _err(f"rule {i + 1}: {f} needs at least one value")
                if f == "ci" and any(x.lower() not in CI_VALUES for x in v):
                    raise _err(f"rule {i + 1}: ci is one of {', '.join(CI_VALUES)}")
                if f == "my_activity" and any(x not in ("todo", "news", "quiet") for x in v):
                    raise _err(f"rule {i + 1}: my_activity is todo, news or quiet")
            elif kind == "text":
                v = str(v or "").strip()[:200]
                if not v:
                    raise _err(f"rule {i + 1}: {f} needs a text")
            elif kind == "number":
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise _err(f"rule {i + 1}: {f} needs a number")
            else:
                v = bool(v)
            conds.append({"field": f, "op": op, "value": v})
        if not conds:
            raise _err(f"rule {i + 1}: give at least one condition")
        out.append({"id": str(r.get("id") or f"r{i + 1}")[:40], "enabled": bool(r.get("enabled", True)), "level": r["level"],
                    "name": str(r.get("name") or "").strip()[:80], "conditions": conds})
    return out


def clean_agent(agent: Any) -> dict[str, Any]:
    if not isinstance(agent, dict):
        raise _err("agent settings: {enabled, summary, decision}")

    def choice(c: Any) -> dict[str, Any]:
        c = c if isinstance(c, dict) else {}
        return {k: (str(c[k]).strip()[:300] or None) if c.get(k) else None for k in ("provider", "model", "variant")}
    return {"enabled": bool(agent.get("enabled")), "summary": choice(agent.get("summary")), "decision": choice(agent.get("decision"))}


def clean_categories(cats: Any) -> list[dict[str, str]]:
    keys = {d["key"] for d in DEFAULT_CATEGORIES}
    if not isinstance(cats, list) or sorted(c.get("key") for c in cats if isinstance(c, dict)) != sorted(keys):
        raise _err("categories: each of todo, news, quiet once, in your order")
    return [{"key": c["key"], "title": str(c.get("title") or "").strip()[:60] or next(d["title"] for d in DEFAULT_CATEGORIES if d["key"] == c["key"]),
             "level": str(c.get("level") or "").strip()[:20]} for c in cats]


# ---------------------------------------------------------------------- deciding a PR's level

def _days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def _value(p: dict[str, Any], field: str) -> Any:
    if field == "size":
        return (p.get("additions") or 0) + (p.get("deletions") or 0)
    if field == "age_days":
        return _days(p.get("created_at"))
    if field == "idle_days":
        return _days(p.get("updated_at"))
    if field == "label":
        return [lb["name"] for lb in p.get("labels") or []]
    if field == "draft":
        return bool(p.get("draft"))
    if field == "requested_by_meta":
        return bool(p.get("requested_by_meta"))
    return p.get({"review_decision": "review_decision", "my_activity": "my_activity"}.get(field, field))


def _match(p: dict[str, Any], c: dict[str, Any]) -> bool:
    f, op, want = c["field"], c["op"], c["value"]
    kind = FIELDS[f]
    have = _value(p, f)
    if kind == "bool":
        return bool(have) == bool(want)
    if kind == "number":
        return have is not None and (have >= want if op == ">=" else have <= want)
    if kind == "text":
        hit = str(want).lower() in str(have or "").lower()
        return hit if op == "contains" else not hit
    wanted = [str(x).lower() for x in want]
    if f == "ci":
        states = {s for w in wanted for s in CI_VALUES.get(w, ())}
        hit = have in states
    elif f == "repo":
        hit = str(have or "").lower() in wanted
    elif f == "label":
        hit = any(x.lower() in wanted for x in have)
    elif f == "my_activity":
        hit = (have or "todo") in wanted
    else:
        hit = str(have or "").lower() in wanted
    return hit if op == "in" else not hit


def describe(c: dict[str, Any]) -> str:
    v = c["value"]
    v = ", ".join(v) if isinstance(v, list) else ("yes" if v is True else "no" if v is False else f"{v:g}" if isinstance(v, float) else v)
    return f"{c['field'].replace('_', ' ')} {c['op'].replace('_', ' ')} {v}"


def pr_key(p: dict[str, Any]) -> str:
    return f"{p['repo']}#{p['number']}".lower()


def hard_level(p: dict[str, Any], prefs: dict[str, Any]) -> tuple[str | None, str | None]:
    """The most urgent level of the enabled hard rules that match (and why), or (None, None)."""
    order = [lv["id"] for lv in prefs["priority_levels"]]
    best: tuple[int, str, str] | None = None
    for r in prefs.get("priority_rules") or []:
        if not r.get("enabled", True) or r["level"] not in order:
            continue
        if all(_match(p, c) for c in r["conditions"]):
            rank = order.index(r["level"])
            if best is None or rank < best[0]:
                best = (rank, r["level"], r.get("name") or " and ".join(describe(c) for c in r["conditions"]))
    return (best[1], best[2]) if best else (None, None)


def final_level(p: dict[str, Any], prefs: dict[str, Any], agent: dict[str, Any] | None) -> dict[str, Any]:
    """{level, rank, source, reasons} — by hand, else the more urgent of hard rules and agent, else the default."""
    levels = prefs["priority_levels"]
    order = [lv["id"] for lv in levels]
    default = prefs.get("priority_default") or order[-1]
    manual = (prefs.get("priority_manual") or {}).get(pr_key(p))
    if not (prefs.get("priority_agent") or {}).get("enabled"):
        agent = None  # the agent is off: its earlier decisions do not count
    reasons: list[str] = []
    rule_level, rule_why = hard_level(p, prefs)
    agent_level = agent.get("level") if agent and agent.get("level") in order else None
    if rule_level:
        reasons.append(f"hard rule: {rule_why} → {rule_level}")
    if agent_level:
        reasons.append(f"agent: {agent.get('reason') or ''}".strip() + (f" (matches “{agent['matched']}”)" if agent.get("matched") else "") + f" → {agent_level}")
    elif agent and agent.get("error"):
        reasons.append(f"agent failed: {agent['error']}")
    elif agent and agent.get("level") is None and agent.get("at"):
        reasons.append("agent: none of your descriptions fits" + (f" — {agent['reason']}" if agent.get("reason") else ""))
    if manual in order:
        level, source = manual, "manual"
        reasons.insert(0, "set by hand")
    else:
        cands = [x for x in ((rule_level, "rule"), (agent_level, "agent")) if x[0]]
        if cands:
            level, source = min(cands, key=lambda x: order.index(x[0]))
        else:
            level, source = default, "default"
            reasons.append("default level")
    return {"level": level, "rank": order.index(level), "source": source, "reasons": reasons,
            "priority": order.index(level) < order.index(default)}


# ---------------------------------------------------------------------- the agent: what it reads

SKIP_FILE = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum|uv\.lock|Pipfile\.lock)$"
                       r"|(^|/)(vendor|third_party|node_modules|dist|build)/|\.(min\.js|min\.css|map|pb\.go|lock)$|_pb2\.py$|\.generated\.")


def plan_files(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(files to summarize — the biggest changes, at most MAX_FILES —, files only named)."""
    named, cand = [], []
    for f in files:
        if not f.get("unified") or f.get("binary_or_large") or SKIP_FILE.search(f["path"]):
            named.append({**f, "note": "binary or too large" if not f.get("unified") else "lock / vendored / generated"})
        else:
            cand.append(f)
    cand.sort(key=lambda f: -(f.get("additions", 0) + f.get("deletions", 0)))
    named += [{**f, "note": f"not summarized (more than {MAX_FILES} files)"} for f in cand[MAX_FILES:]]
    return cand[:MAX_FILES], named


def split_patch(text: str, limit: int = BATCH_CHARS) -> list[str]:
    """A file's patch in parts of ≤ limit characters, at hunk boundaries when possible."""
    if len(text) <= limit:
        return [text]
    hunks = re.split(r"(?m)^(?=@@ )", text)
    parts, cur = [], ""
    for h in hunks:
        while len(h) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(h[:limit])
            h = h[limit:]
        if len(cur) + len(h) > limit and cur:
            parts.append(cur)
            cur = ""
        cur += h
    if cur:
        parts.append(cur)
    return parts


def file_hash(f: dict[str, Any]) -> str:
    return hashlib.sha256(f"{f['path']}\0{f.get('unified') or ''}".encode()).hexdigest()


def batches(items: list[tuple[str, str, str]], limit: int = BATCH_CHARS) -> list[list[tuple[str, str, str]]]:
    """[(id, label, text)] grouped into turns of ≤ limit characters."""
    out: list[list[tuple[str, str, str]]] = []
    cur: list[tuple[str, str, str]] = []
    size = 0
    for it in items:
        if cur and size + len(it[2]) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it[2])
    if cur:
        out.append(cur)
    return out


def summary_prompt(batch: list[tuple[str, str, str]], pr: dict[str, Any]) -> str:
    parts = ["WAW-PRIORITY-SUMMARY",
             f"Summarize each file change below from pull request {pr['repo']}#{pr['number']} \u201c{pr['title']}\u201d for a reviewer "
             "who decides how urgent the review is. For each item write 2-4 short lines: what changed, why it matters, any "
             "risk (correctness, crashes, security, performance, API or behaviour changes). Do not run tools, do not read or "
             "change any file: use only the text here.",
             'Reply with ONLY a JSON object: {"files": [{"id": "<item id>", "summary": "<2-4 lines>"}]} covering every item.', ""]
    for iid, label, text in batch:
        parts += [f"### FILE {iid} {label}", "```diff", text.rstrip("\n"), "```", ""]
    return "\n".join(parts)


def levels_text(prefs: dict[str, Any]) -> str:
    lines = []
    for lv in prefs["priority_levels"]:
        lines.append(f"- {lv['id']} ({lv['name']}):" + ("" if lv["descriptions"] else " (no description)"))
        lines += [f"    * {d}" for d in lv["descriptions"]]
    return "\n".join(lines)


def decision_prompt(pr: dict[str, Any], item: dict[str, Any], prefs: dict[str, Any], files: list[dict[str, Any]],
                    diff: str | None, summaries: list[dict[str, Any]] | None) -> str:
    order = [lv["id"] for lv in prefs["priority_levels"]]
    meta = [f"Repository: {pr['repo']}", f"Pull request: #{pr['number']} {pr['title']}", f"Author: {pr.get('author')}",
            f"Review requested by: {item.get('requested_by') or 'unknown'}", f"Draft: {'yes' if pr.get('draft') else 'no'}",
            f"Labels: {', '.join(lb['name'] for lb in pr.get('labels') or []) or 'none'}",
            f"Size: +{pr.get('additions', 0)} −{pr.get('deletions', 0)} in {pr.get('changed_files', len(files))} files",
            f"CI: {pr.get('ci') or 'none'}", f"Review decision: {pr.get('review_decision') or 'none'}",
            f"Created: {pr.get('created_at')}, last updated: {pr.get('updated_at')}"]
    flist = "\n".join(f"- {f['path']} ({f.get('status')}, +{f.get('additions', 0)} −{f.get('deletions', 0)})" for f in files)
    body = (pr.get("body") or "").strip()
    parts = ["WAW-PRIORITY-DECISION",
             "You decide how urgent it is for the user to review this GitHub pull request. The user describes each priority "
             f"level below, most urgent first ({' > '.join(order)}). Choose the MOST URGENT level one of whose descriptions "
             "fits this pull request; if none fits, choose none. Judge only from the information here; do not run tools, "
             "do not read or change any file.",
             "Levels and their descriptions:", levels_text(prefs), "", "About the pull request:", *meta, "",
             "Description:" if body else "Description: (none)", *( [body[:BODY_CHARS] + (" …" if len(body) > BODY_CHARS else "")] if body else []), "",
             "Changed files:", flist or "(none)", ""]
    if diff is not None:
        parts += ["The diff:", "```diff", diff.rstrip("\n"), "```"]
    else:
        parts += ["Summaries of the changes, file by file:"]
        parts += [f"- {s['path']}: {s['summary']}" if s.get("summary") else f"- {s['path']}: ({s.get('note') or 'not summarized'})" for s in summaries or []]
    parts += ["", 'Reply with ONLY a JSON object: {"level": "<one of ' + ", ".join(order) + '> or null", '
              '"reason": "<one sentence>", "matched": "<the description that fits, quoted, or empty>"}']
    return "\n".join(parts)


def parse_json(text: str) -> Any:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise ValueError("the agent's answer has no JSON object")
    return json.loads(m.group(1))


# ---------------------------------------------------------------------- the agent: running it

_queue: list[str] = []                    # PR keys waiting
_items: dict[str, dict[str, Any]] = {}    # key → the queue item (repo, number, …) to classify
_running: set[str] = set()
_forced: set[str] = set()
_workers: list[asyncio.Task] = []
_state: dict[str, Any] = {"last_run": None, "classified": 0, "files_summarizing": 0, "errors": 0}


def fingerprint(item: dict[str, Any], prefs: dict[str, Any]) -> str:
    desc = json.dumps([[lv["id"], lv["name"], lv["descriptions"]] for lv in prefs["priority_levels"]], ensure_ascii=False)
    pr = json.dumps([item.get("updated_at"), item.get("title"), item.get("draft"), sorted(lb["name"] for lb in item.get("labels") or []),
                     item.get("additions"), item.get("deletions")], ensure_ascii=False)
    return hashlib.sha256((pr + "\0" + desc + "\0" + json.dumps(prefs["priority_agent"]["decision"])).encode()).hexdigest()[:20]


def results(ctx: Any) -> dict[str, dict[str, Any]]:
    return kv_get(ctx, RESULTS_KEY, {})


def _save_result(ctx: Any, key: str, res: dict[str, Any]) -> None:
    allres = results(ctx)
    allres[key] = res
    if len(allres) > 800:  # drop the oldest
        for k in sorted(allres, key=lambda k: allres[k].get("at") or "")[: len(allres) - 800]:
            allres.pop(k, None)
    kv_put(ctx, RESULTS_KEY, allres)


def needs_agent(item: dict[str, Any], prefs: dict[str, Any], res: dict[str, Any] | None) -> bool:
    agent = prefs["priority_agent"]
    if not agent.get("enabled") or not any(lv["descriptions"] for lv in prefs["priority_levels"]):
        return False
    key = pr_key(item)
    if (prefs.get("priority_manual") or {}).get(key):
        return False
    rule_level, _ = hard_level(item, prefs)
    if rule_level and rule_level == prefs["priority_levels"][0]["id"]:
        return False  # already the most urgent: the agent cannot raise it
    if key in _forced:
        return True
    return not res or res.get("fingerprint") != fingerprint(item, prefs)


def schedule(ctx: Any, items: list[dict[str, Any]], prefs: dict[str, Any]) -> int:
    """Queue the PRs that need the agent; start the workers. Returns how many are queued or running."""
    allres = results(ctx)
    for it in items:
        key = pr_key(it)
        if key in _running or key in _queue:
            continue
        if needs_agent(it, prefs, allres.get(key)):
            _items[key] = it
            _queue.append(key)
    alive = [w for w in _workers if not w.done()]
    _workers[:] = alive
    while _queue and len(_workers) < CONCURRENCY:
        _workers.append(asyncio.get_running_loop().create_task(_worker(ctx)))
    return len(_queue) + len(_running)


def force(keys: list[str]) -> None:
    _forced.update(keys)


def status() -> dict[str, Any]:
    return {**_state, "queued": len(_queue), "running": sorted(_running)}


async def _worker(ctx: Any) -> None:
    while _queue:
        key = _queue.pop(0)
        item = _items.pop(key, None)
        if item is None:
            continue
        _running.add(key)
        try:
            await classify(ctx, item)
        except Exception as exc:  # noqa: BLE001 - recorded, retried on the next refresh
            log.warning("priority of %s failed: %s", key, exc)
            _state["errors"] += 1
            _save_result(ctx, key, {"level": None, "error": str(getattr(exc, "message", exc))[:300], "fingerprint": None,
                                    "at": _now()})
        finally:
            _running.discard(key)
            _forced.discard(key)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_turn(ctx: Any, choice: dict[str, Any], prompt: str, title: str) -> str:
    """One read-only agent turn in a fresh session (hidden from session pickers); its final text."""
    from .models import AgentSession, Job, JobEvent
    from .sessions import ModelChoiceIn, SessionService

    if ctx.jobs is None or ctx.providers is None:
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "agents are not available")
    sess = await SessionService(ctx).session_for_model(None, ModelChoiceIn(**{k: choice.get(k) for k in ("provider", "model", "variant")}), title)
    with ctx.db.write() as s:
        row = s.get(AgentSession, sess.id)
        if row is not None:
            row.origin = "priority"
    work = Path(ctx.settings.state_dir) / "priority-work"
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    done: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_complete(job_id: str) -> None:
        with ctx.db.read() as s:
            job = s.get(Job, job_id)
            ev = s.query(JobEvent).filter_by(job_id=job_id, type="completed").order_by(JobEvent.seq.desc()).first()
            text = (json.loads(ev.payload_json).get("text") or "") if ev else ""
            st, msg = (job.status, job.error_message) if job else ("failed", "the job is gone")
        if not done.done():
            if st == "completed":
                done.set_result(text)
            else:
                done.set_exception(ApiError(502, "AGENT_FAILED", msg or f"the agent's turn ended: {st}"))

    ctx.jobs.enqueue_turn(session_id=sess.id, thread_id=None, prompt=prompt, context={"_cwd": str(work), "_read_only": True},
                          agent_message_id=None, kind="priority", on_complete=on_complete)
    return await asyncio.wait_for(done, TURN_TIMEOUT)


async def _fetch_pr(ctx: Any, item: dict[str, Any]) -> dict[str, Any]:
    from . import github_hub as gh

    owner, repo = item["repo"].split("/", 1)
    key = pr_key(item)
    hit = gh._pr_cache.get(key)
    if hit and time.time() - hit[0] < 600 and hit[1]["pr"].get("updated_at") == item.get("updated_at"):
        return hit[1]
    graphql = gh._hooks(ctx)[0]
    files_fn = ctx.extra.get("gh_pr_files", gh._gh_files)
    data = await asyncio.to_thread(gh.fetch_pr, owner, repo, item["number"], graphql, files_fn)
    gh._pr_cache[key] = (time.time(), data)
    return data


async def summarize_files(ctx: Any, pr: dict[str, Any], files: list[dict[str, Any]], choice: dict[str, Any]) -> dict[str, str]:
    """file hash → summary, from the cache or new summarization turns (≤ BATCH_CHARS each; a big file in parts)."""
    cache: dict[str, str] = kv_get(ctx, SUMMARIES_KEY, {})
    out = {h: cache[h] for f in files if (h := file_hash(f)) in cache}
    todo = [f for f in files if file_hash(f) not in out]
    items: list[tuple[str, str, str]] = []
    parts_of: dict[str, list[str]] = {}
    for i, f in enumerate(todo):
        parts = split_patch(f["unified"])
        for j, text in enumerate(parts):
            iid = f"f{i}" + (f"p{j + 1}" if len(parts) > 1 else "")
            label = f"{f['path']} ({f.get('status')}, +{f.get('additions', 0)} −{f.get('deletions', 0)})" + (f" — part {j + 1} of {len(parts)}" if len(parts) > 1 else "")
            items.append((iid, label, text))
            parts_of.setdefault(file_hash(f), []).append(iid)
    got: dict[str, str] = {}
    _state["files_summarizing"] += len(todo)
    try:
        for batch in batches(items):
            text = await run_turn(ctx, choice, summary_prompt(batch, pr), f"PR priority: summaries of {pr['repo']}#{pr['number']}")
            data = parse_json(text)
            for x in data.get("files") or []:
                if isinstance(x, dict) and x.get("id"):
                    got[str(x["id"])] = str(x.get("summary") or "").strip()[:1500]
    finally:
        _state["files_summarizing"] -= len(todo)
    for h, ids in parts_of.items():
        texts = [got.get(i, "") for i in ids]
        s = texts[0] if len(ids) == 1 else " ".join(f"(part {n + 1}) {t}" for n, t in enumerate(texts) if t)
        if s:
            out[h] = s
            cache[h] = s
    if len(cache) > 4000:
        for k in list(cache)[: len(cache) - 4000]:
            cache.pop(k, None)
    kv_put(ctx, SUMMARIES_KEY, cache)
    return out


async def classify(ctx: Any, item: dict[str, Any]) -> dict[str, Any]:
    from .github_hub import load_prefs

    prefs = load_prefs(ctx)
    agent = prefs["priority_agent"]
    data = await _fetch_pr(ctx, item)
    pr, files = data["pr"], data["files"]
    total = sum(len(f.get("unified") or "") for f in files)
    shown: list[dict[str, Any]] = []
    if total <= SMALL_DIFF:
        diff, summaries = "".join(f.get("unified") or "" for f in files), None
        shown = [{"path": f["path"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0), "summary": None,
                  "note": "small change: read in full" if f.get("unified") else "binary or too large"} for f in files]
    else:
        todo, named = plan_files(files)
        sums = await summarize_files(ctx, pr, todo, agent["summary"])
        summaries = [{"path": f["path"], "summary": sums.get(file_hash(f)), "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)} for f in todo] + \
            [{"path": f["path"], "summary": None, "note": f["note"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)} for f in named]
        diff, shown = None, summaries
    text = await run_turn(ctx, agent["decision"], decision_prompt(pr, item, prefs, files, diff, summaries),
                          f"PR priority: {pr['repo']}#{pr['number']}")
    order = [lv["id"] for lv in prefs["priority_levels"]]
    ans = parse_json(text)
    level = ans.get("level")
    if isinstance(level, str) and level.strip().lower() in ("", "none", "null"):
        level = None
    if level is not None:
        level = next((x for x in order if x.lower() == str(level).strip().lower()), None)
        if level is None:
            raise ValueError(f"the agent chose an unknown level {ans.get('level')!r}")
    res = {"level": level, "reason": str(ans.get("reason") or "").strip()[:500], "matched": str(ans.get("matched") or "").strip()[:300],
           "fingerprint": fingerprint(item, prefs), "at": _now(), "model": agent["decision"].get("model"),
           "mode": "diff" if diff is not None else "summaries", "files": shown[:200]}
    _save_result(ctx, pr_key(item), res)
    _state["classified"] += 1
    _state["last_run"] = res["at"]
    return res

"""Review learning: the agent's PR reviews get better from what you do with them.

The loop:

1. **Capture** (`review_cases`): what became of each agent suggestion when you publish a review — applied as it
   was, edited (the agent's original is kept on the draft), discarded (with an optional reason) — plus the comments
   you wrote yourself on a PR the agent reviewed, and those other reviewers wrote where the agent said nothing
   (`missed`). Teaching a lesson from a comment records a `taught` case.
2. **Distil**: a read-only model turn (the app's agent, or the Repo Insights model endpoint) reads the new cases and
   the existing lessons and proposes changes: new lessons, revisions, confirmations, contradictions.
3. **Approve** (`review_lessons`): proposed lessons wait for you; only `active` ones are used. Contradicted ones are
   flagged for you, never switched off by themselves. Rejected ones: the same text is never
   proposed again; other wordings of the idea rely on the prompt, which lists the rejected lessons.
4. **Inject**: an agent review's prompt gets the active lessons of the repository (and of every repository) whose
   path globs match the PR, a few real examples from the repository, and the agent's track record there (applied /
   discarded per severity), with an instruction to check each comment against them before suggesting it.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .errors import ApiError
from .github_hub import require_owner, require_owner_mutating
from .kv import kv_get, kv_put

if TYPE_CHECKING:
    from .deps import AppContext

log = logging.getLogger("waw.review_learning")
router = APIRouter()

SETTINGS_KEY = "review_learning"
STATE_KEY = "review_learning_state"
REASONS = {"wrong": "Wrong", "nitpick": "Nitpick", "minor": "Not important", "discussed": "Already discussed",
           "scope": "Out of scope", "other": "Other"}
SEVERITIES = ("blocker", "major", "minor", "nit")
STATUSES = ("proposed", "active", "off", "rejected")
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,  # lessons, examples and the track record go into agent reviews
    "auto": True,  # distil after each published review
    "model": {"mode": "agent", "provider": None, "model": None, "variant": None},  # agent | api (Repo Insights endpoint)
    "max_lessons": 40, "max_chars": 6000, "examples": 6,
    "min_cases": 10,  # the track record is shown from this many agent suggestions (last 90 days) on
    "reviewers": True,  # learn from comments other reviewers wrote where the agent said nothing
}
NEAR_LINES = 3  # a comment this close to an agent suggestion is about the same point
MAX_REVIEWER_CASES = 20  # per PR
BATCH = 80  # cases per distiller run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def _iso(d: datetime | None) -> str | None:
    d = _aware(d)
    return d.isoformat() if d else None


# ---------------------------------------------------------------------- settings

def load_settings(ctx: AppContext) -> dict[str, Any]:
    s = {**DEFAULT_SETTINGS, **(kv_get(ctx, SETTINGS_KEY, {}) or {})}
    s["model"] = {**DEFAULT_SETTINGS["model"], **(s.get("model") or {})}
    return s


def clean_settings(patch: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    out = dict(cur)
    for k in ("enabled", "auto", "reviewers"):
        if k in patch:
            out[k] = bool(patch[k])
    for k, lo, hi in (("max_lessons", 1, 100), ("max_chars", 500, 20000), ("examples", 0, 12), ("min_cases", 1, 1000)):
        if k in patch:
            try:
                out[k] = max(lo, min(hi, int(patch[k])))
            except (TypeError, ValueError):
                raise ApiError(400, "INVALID_SETTING", f"{k} must be a number")
    if "model" in patch:
        m = patch["model"] if isinstance(patch["model"], dict) else {}
        mode = m.get("mode") or "agent"
        if mode not in ("agent", "api"):
            raise ApiError(400, "INVALID_SETTING", "model.mode must be agent or api")
        out["model"] = {"mode": mode, **{k: (str(m[k]).strip()[:300] or None) if m.get(k) else None for k in ("provider", "model", "variant")}}
    return out


# ---------------------------------------------------------------------- capture

def safely(fn: Any, *args: Any, default: Any = None) -> Any:
    """Run a review-learning step for a core request (publish a review, load comments, delete a draft): a failure is
    logged and `default` returned, so learning never breaks the request it rides on."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("review learning: %s failed (the request goes on)", getattr(fn, "__name__", fn))
        return default


def _case(**kw: Any) -> Any:
    from .db import new_id
    from .models import ReviewCase

    return ReviewCase(id=new_id(), created_at=_now(), **kw)


def _near(path: str, line: int | None, others: list[tuple[str, int | None]]) -> bool:
    """Same file and within NEAR_LINES lines (comments on the whole file: only other whole-file comments)."""
    for p, n in others:
        if p != path:
            continue
        if (line and n and abs(line - n) <= NEAR_LINES) or (not line and not n):
            return True
    return False


def _agent_reviewed(ctx: AppContext, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    from .github_hub import _review_key

    return [r for r in kv_get(ctx, _review_key(owner, repo, number), []) if r.get("state") == "done"]


def agent_spots(ctx: AppContext, full: str, number: int) -> list[tuple[str, int | None]]:
    """Where the agent said something on a PR: its suggestions still there (decided or not) and those already
    recorded as cases (published, discarded or deleted earlier)."""
    from .models import GithubPrDraft, ReviewCase

    with ctx.db.read() as s:
        spots = [(p, n or None) for p, n in s.query(GithubPrDraft.path, GithubPrDraft.line).filter_by(repo=full, number=number, origin="agent")]
        spots += [(p, n) for p, n in s.query(ReviewCase.path, ReviewCase.line).filter_by(repo=full, number=number, source="agent")]
    return spots


def record_published(ctx: AppContext, owner: str, repo: str, number: int, drafts: list[dict[str, Any]],
                     spots: list[tuple[str, int | None]] | None = None) -> dict[str, int]:
    """After a review was published: a case for every agent suggestion you decided on (applied / edited /
    discarded), and for your own comments on a PR the agent reviewed (points it missed). `drafts`: the decided
    drafts; `spots`: every place the agent spoke on the PR, taken before the published drafts were deleted
    (agent_spots). Returns {agent, missed}: the cases recorded."""
    full = f"{owner}/{repo}".lower()
    agent = [d for d in drafts if d.get("origin") == "agent" and d.get("decision") in ("accepted", "discarded")]
    mine = [d for d in drafts if d.get("origin") != "agent" and d.get("decision") == "accepted"]
    reviewed = bool(agent) or bool(_agent_reviewed(ctx, owner, repo, number))
    spots = list(spots) if spots is not None else agent_spots(ctx, full, number)
    spots += [(d["path"], d.get("line") or None) for d in drafts if d.get("origin") == "agent"]
    rows = []
    for d in agent:
        orig = d.get("original_body") or d["body"]
        if d["decision"] == "discarded":
            outcome, final = "discarded", None
        else:
            outcome, final = ("applied" if d["body"].strip() == orig.strip() else "edited"), d["body"]
        rows.append(_case(repo=full, number=number, path=d["path"], line=d.get("line") or None, line_text=(d.get("line_text") or "")[:2000],
                          source="agent", outcome=outcome, agent_body=orig, final_body=final,
                          reason=d.get("discard_reason") if outcome == "discarded" else None,
                          severity=d.get("severity"), category=d.get("category")))
    if reviewed:
        for d in mine:
            if _near(d["path"], d.get("line") or None, spots):
                continue  # your own wording of a point the agent made
            rows.append(_case(repo=full, number=number, path=d["path"], line=d.get("line") or None, line_text=(d.get("line_text") or "")[:2000],
                              source="user", outcome="missed", final_body=d["body"]))
    if rows:
        with ctx.db.write() as s:
            s.add_all(rows)
    n_agent = sum(1 for r in rows if r.source == "agent")
    return {"agent": n_agent, "missed": len(rows) - n_agent}


def record_deleted(ctx: AppContext, d: dict[str, Any]) -> None:
    """Deleting an agent suggestion is discarding it (`d`: the draft's dict with repo and number)."""
    if d.get("origin") != "agent":
        return
    with ctx.db.write() as s:
        s.add(_case(repo=d["repo"], number=d["number"], path=d["path"], line=d.get("line") or None, line_text=(d.get("line_text") or "")[:2000],
                    source="agent", outcome="discarded", agent_body=d.get("original_body") or d["body"], reason=d.get("discard_reason"),
                    severity=d.get("severity"), category=d.get("category")))


def _is_bot(login: str | None) -> bool:
    lo = (login or "").lower()
    return not lo or lo.endswith("[bot]") or lo.endswith("-bot") or lo in ("github-actions", "copilot", "dependabot", "pytorchbot", "facebook-github-bot")


def record_reviewer_comments(ctx: AppContext, owner: str, repo: str, number: int, threads: list[dict[str, Any]], me: str | None) -> int:
    """Comments other reviewers started after the agent reviewed the PR, where the agent said nothing: points it
    missed (each GitHub comment once; at most MAX_REVIEWER_CASES per PR; bots and you are left out)."""
    if not load_settings(ctx).get("reviewers"):
        return 0
    runs = _agent_reviewed(ctx, owner, repo, number)
    if not runs or not threads:
        return 0
    from .models import ReviewCase

    full = f"{owner}/{repo}".lower()
    since = min(r.get("started_at") or "" for r in runs)
    spots = agent_spots(ctx, full, number)
    found = []
    for t in threads:
        c = (t.get("comments") or [None])[0]
        if not c:
            continue
        ref = str(c.get("id") or "")
        who = c.get("author")
        if not ref or _is_bot(who) or (me and (who or "").lower() == me.lower()):
            continue
        if (c.get("created_at") or "") <= since:
            continue  # was there when the agent reviewed (it read it)
        line = t.get("line") or t.get("original_line")
        if _near(t.get("path") or "", line, spots):
            continue
        found.append((ref, t, c, line))
    if not found:
        return 0
    n = 0
    # the ids already recorded are read in the same write transaction (which holds the write lock), so two
    # requests at once cannot both record a comment
    with ctx.db.write() as s:
        seen = {g for (g,) in s.query(ReviewCase.gh_ref).filter_by(repo=full, number=number, source="reviewer")}
        budget = MAX_REVIEWER_CASES - len(seen)
        for ref, t, c, line in found:
            if ref in seen or budget <= 0:
                continue
            s.add(_case(repo=full, number=number, path=t.get("path") or "", line=line, line_text=(t.get("diff_hunk") or "")[-1500:],
                        source="reviewer", outcome="missed", final_body=(c.get("body") or "")[:4000], author=c.get("author"), gh_ref=ref))
            seen.add(ref)
            budget -= 1
            n += 1
    return n


# ---------------------------------------------------------------------- lessons in the review prompt

def _paths(v: str | None) -> list[str]:
    try:
        x = json.loads(v or "[]")
        return [str(p) for p in x] if isinstance(x, list) else []
    except ValueError:
        return []


_FILE_HEAD = re.compile(r"^### (.+) \((?:added|removed|modified|renamed|copied|changed|unchanged), \+\d+ −\d+\)$", re.M)


def pr_paths(copy_text: str) -> list[str]:
    """The changed files of a PR copy: the `### path (status, +a −d)` headings of pr_markdown's "Changed files"
    section (not the description before it, not the review discussion appended after it)."""
    text = copy_text.partition("\n\x00DISCUSSION\x00")[0]
    _, sep, files = text.partition("\n## Changed files\n")
    return _FILE_HEAD.findall(files) if sep else []


def _applies(lesson: Any, paths: list[str]) -> bool:
    globs = _paths(lesson.paths)
    return not globs or any(fnmatch.fnmatch(p, g) for p in paths for g in globs)


def select_lessons(ctx: AppContext, repo: str, paths: list[str]) -> list[Any]:
    from .models import ReviewLesson

    st = load_settings(ctx)
    full = repo.lower()
    with ctx.db.read() as s:
        rows = s.query(ReviewLesson).filter(ReviewLesson.status == "active", ReviewLesson.scope.in_([full, "*"])).all()
    rows = [r for r in rows if _applies(r, paths)]
    # this repository's first, then those cases confirmed most, then the newest
    rows.sort(key=lambda r: (r.scope != full, -(r.confirmed_count or 0), -(_aware(r.updated_at) or _now()).timestamp()))
    out, size = [], 0
    for r in rows:
        if len(out) >= st["max_lessons"] or size + len(r.text) > st["max_chars"]:
            break
        out.append(r)
        size += len(r.text)
    return out


def stats(ctx: AppContext, repo: str | None = None, days: int = 90) -> dict[str, dict[str, Any]]:
    """Per repository over the last `days`: agent suggestions applied / edited / discarded (also per severity), and
    the points it missed (your comments, other reviewers')."""
    from .models import ReviewCase

    since = _now() - timedelta(days=days)
    out: dict[str, dict[str, Any]] = {}
    with ctx.db.read() as s:
        q = s.query(ReviewCase.repo, ReviewCase.source, ReviewCase.outcome, ReviewCase.severity).filter(ReviewCase.created_at >= since)
        if repo:
            q = q.filter(ReviewCase.repo == repo.lower())
        for r, src, outcome, sev in q:
            x = out.setdefault(r, {"suggested": 0, "applied": 0, "edited": 0, "discarded": 0, "missed": 0, "taught": 0, "by_severity": {}})
            if src == "agent":
                x["suggested"] += 1
                x[outcome] += 1
                b = x["by_severity"].setdefault(sev or "unrated", {"suggested": 0, "kept": 0})
                b["suggested"] += 1
                b["kept"] += outcome in ("applied", "edited")
            elif outcome in ("missed", "taught"):
                x[outcome] += 1
    return out


def track_record(ctx: AppContext, repo: str) -> str | None:
    st = load_settings(ctx)
    x = stats(ctx, repo).get(repo.lower())
    if not x or x["suggested"] < st["min_cases"]:
        return None
    n = x["suggested"]
    pct = lambda k: round(100 * x[k] / n)  # noqa: E731
    lines = [f"Your track record on {repo} (last 90 days): of {n} suggested comments the user applied {pct('applied')}% as written, "
             f"applied {pct('edited')}% after editing them and discarded {pct('discarded')}%; the user and other reviewers made "
             f"{x['missed']} points you missed."]  # numbers only: no text of other reviewers
    sev = [f"{k} {v['kept']}/{v['suggested']} kept" for k, v in sorted(x["by_severity"].items(), key=lambda kv: (SEVERITIES + ("unrated",)).index(kv[0]) if kv[0] in SEVERITIES + ("unrated",) else 9)]
    if sev:
        lines.append("By severity: " + ", ".join(sev) + ". Calibrate: skip kinds of comments the user rarely keeps unless they really matter here.")
    return "\n".join(lines)


def _short(t: str | None, n: int) -> str:
    t = (t or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def examples(ctx: AppContext, repo: str, k: int) -> list[str]:
    """A few recent cases of this repository: discarded (with the reason), edited, missed, applied."""
    from .models import ReviewCase

    if k <= 0:
        return []
    # only what the user did: comments by other reviewers go to the distiller (lessons you accept), never verbatim
    # into a prompt — anyone can comment on a public repository
    want = [("agent", "discarded", 2), ("agent", "edited", 1), ("user", "missed", 2), ("agent", "applied", 1)]
    out: list[str] = []
    with ctx.db.read() as s:
        for src, outcome, n in want:
            q = s.query(ReviewCase).filter(ReviewCase.repo == repo.lower(), ReviewCase.outcome == outcome)
            q = q.filter(ReviewCase.source == src)
            for c in q.order_by(ReviewCase.created_at.desc()).limit(n):
                where = f"{c.path}{f' line {c.line}' if c.line else ''}"
                code = f" — code: `{_short(c.line_text, 160)}`" if c.line_text else ""
                if outcome == "discarded":
                    out.append(f"- DISCARDED ({REASONS.get(c.reason or '', 'no reason given')}) on {where}{code}: \"{_short(c.agent_body, 300)}\"")
                elif outcome == "edited":
                    out.append(f"- EDITED on {where}: you wrote \"{_short(c.agent_body, 250)}\" — the user published \"{_short(c.final_body, 250)}\"")
                elif outcome == "missed":
                    out.append(f"- MISSED on {where}{code}: the user commented \"{_short(c.final_body, 300)}\"")
                else:
                    out.append(f"- APPLIED on {where}: \"{_short(c.agent_body, 300)}\"")
    return out[:k]


def review_context(ctx: AppContext, repo: str, paths: list[str]) -> tuple[str | None, list[dict[str, str]]]:
    """The review-learning part of an agent review's prompt, and the lessons it uses ({id, text})."""
    from .models import ReviewLesson

    st = load_settings(ctx)
    if not st["enabled"]:
        return None, []
    lessons = select_lessons(ctx, repo, paths)
    ex = examples(ctx, repo, st["examples"])
    record = track_record(ctx, repo)
    if not lessons and not ex and not record:
        return None, []
    parts = ["What you learned from this user's earlier reviews (from the suggestions they applied, edited and discarded, "
             "and from the points they made that you missed):"]
    if lessons:
        parts.append("LESSONS — follow them:")
        parts += [f"- {r.text}" + ("" if r.scope == repo.lower() else " (every repository)") for r in lessons]
    if ex:
        parts.append(f"Examples from {repo}:")
        parts += ex
    if record:
        parts.append(record)
    parts.append("Before you write the comment block, check each comment against these lessons and examples, and against the "
                 "code (the local checkout when there is one): drop comments they rule out and those you cannot substantiate.")
    if lessons:
        with ctx.db.write() as s:
            for r in lessons:
                row = s.get(ReviewLesson, r.id)
                if row is not None:
                    row.used_count = (row.used_count or 0) + 1
    return "\n".join(parts), [{"id": r.id, "text": r.text} for r in lessons]


# ---------------------------------------------------------------------- distil

_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()


def _case_dict(c: Any) -> dict[str, Any]:
    return {"id": c.id, "repo": c.repo, "number": c.number, "path": c.path, "line": c.line, "line_text": c.line_text,
            "source": c.source, "outcome": c.outcome, "agent_body": c.agent_body, "final_body": c.final_body, "reason": c.reason,
            "reason_label": REASONS.get(c.reason or ""), "severity": c.severity, "category": c.category, "author": c.author,
            "distilled": bool(c.distilled), "created_at": _iso(c.created_at)}


def lesson_dict(r: Any, replaced: str | None = None) -> dict[str, Any]:
    ev = _paths(r.evidence)
    return {"id": r.id, "scope": r.scope, "paths": _paths(r.paths), "text": r.text, "status": r.status, "source": r.source,
            "rationale": r.rationale or "", "evidence": len(ev), "replaces": r.replaces, "replaces_text": replaced,
            "used": r.used_count or 0, "confirmed": r.confirmed_count or 0, "contradicted": r.contradicted_count or 0,
            "flagged": r.flagged, "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at)}


def distill_prompt(repo: str, cases: list[dict[str, Any]], lessons: list[dict[str, Any]]) -> str:
    def case_line(c: dict[str, Any]) -> str:
        at = f" line {c['line']}" if c["line"] else ""
        where = f"{c['path']}{at} (PR #{c['number']})"
        code = f"\n  code: {_short(c['line_text'], 300)}" if c["line_text"] else ""
        sev = f" [{c['severity']}{'/' + c['category'] if c['category'] else ''}]" if c["severity"] else ""
        if c["outcome"] == "discarded":
            body = f"agent suggested{sev}: {_short(c['agent_body'], 600)}\n  user DISCARDED it; reason: {c['reason_label'] or 'not given'}"
        elif c["outcome"] == "edited":
            body = f"agent suggested{sev}: {_short(c['agent_body'], 500)}\n  user EDITED it and published: {_short(c['final_body'], 500)}"
        elif c["outcome"] == "applied":
            body = f"agent suggested{sev}: {_short(c['agent_body'], 500)}\n  user APPLIED it as written"
        elif c["outcome"] == "missed":
            who = f"reviewer {c['author']}" if c["source"] == "reviewer" else "the user"
            body = f"the agent said nothing here; {who} commented: {_short(c['final_body'], 600)}"
        else:
            body = f"the user taught: {_short(c['final_body'], 600)}"
        return f"[{c['id']}] {where}{code}\n  {body}"

    def lesson_line(lsn: dict[str, Any]) -> str:
        scope = "this repository" if lsn["scope"] != "*" else "every repository"
        paths = f" files {', '.join(lsn['paths'])}" if lsn["paths"] else ""
        return f"[{lsn['id']}] ({lsn['status']}; {scope}{paths}) {lsn['text']}"

    active = [x for x in lessons if x["status"] in ("active", "proposed", "off")]
    rejected = [x for x in lessons if x["status"] == "rejected"]
    return "\n\n".join([
        "WAW-REVIEW-LEARNING",
        f"You maintain the lessons an AI code reviewer follows when it reviews pull requests of {repo} for this user. "
        "Below are new CASES — what the user did with the reviewer's suggested comments (applied, edited, discarded "
        "with a reason) and points the reviewer missed (comments by the user or other reviewers where it said nothing) "
        "— and the EXISTING LESSONS. Distil what the cases show about the user's standards into short, specific, "
        "actionable lessons for the reviewer (what to look for, what to leave out, how to phrase and how strongly). "
        "Generalize only from what the cases support; one case is enough for a lesson only when it is clear-cut "
        "(an explicit reason, a precise edit). Prefer revising or confirming an existing lesson over adding a near "
        "duplicate. Never propose again what was rejected. Judge only from the information here; do not run tools.",
        "EXISTING LESSONS:\n" + ("\n".join(lesson_line(x) for x in active) or "(none)"),
        "REJECTED by the user (do not propose these or the same idea again):\n" + ("\n".join(f"- {x['text']}" for x in rejected) or "(none)"),
        "CASES:\n" + "\n".join(case_line(c) for c in cases),
        'Reply with ONLY a JSON object: {"changes": [ ... ]}, each change one of\n'
        '{"op": "add", "scope": "repo" | "all", "paths": [<optional file globs, e.g. "aten/src/ATen/native/xpu/*">], '
        '"text": "<the lesson, one or two sentences, addressed to the reviewer>", "why": "<which cases show it>", "cases": ["<case id>", ...]}\n'
        '{"op": "revise", "id": "<lesson id>", "text": "<the better lesson>", "why": "...", "cases": [...]}\n'
        '{"op": "confirm", "id": "<lesson id>", "cases": [...]}\n'
        '{"op": "contradict", "id": "<lesson id>", "why": "<what the cases show instead>", "cases": [...]}\n'
        'Use "scope": "all" only for habits that clearly do not depend on this repository. An empty list is a fine answer.',
    ])


def parse_json(text: str) -> Any:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", t, re.S)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except ValueError:
                return None
    return None


async def ask_model(ctx: AppContext, prompt: str, title: str) -> str:
    """One read-only turn of the model chosen for review learning: the app's agent or the Repo Insights endpoint."""
    m = load_settings(ctx)["model"]
    if m["mode"] == "api":
        from .insights.llm import complete

        return await complete(ctx, "review_learning", prompt, as_json=True)
    from .review_priority import run_turn

    return await run_turn(ctx, m, prompt, title)


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", t.lower()).strip()


def apply_changes(ctx: AppContext, repo: str, changes: Any, case_ids: list[str]) -> dict[str, int]:
    from .db import new_id
    from .models import ReviewLesson

    done = {"added": 0, "revised": 0, "confirmed": 0, "contradicted": 0}
    if not isinstance(changes, list):
        return done
    valid = set(case_ids)
    full = repo.lower()
    with ctx.db.write() as s:
        lessons = {r.id: r for r in s.query(ReviewLesson).filter(ReviewLesson.scope.in_([full, "*"]))}
        by_text = {_norm(r.text): r for r in lessons.values()}
        for ch in changes[:30]:
            if not isinstance(ch, dict):
                continue
            op = ch.get("op")
            cases = [str(c) for c in (ch.get("cases") or []) if str(c) in valid]
            text = str(ch.get("text") or "").strip()[:1000]
            why = str(ch.get("why") or "").strip()[:1000]
            target = lessons.get(str(ch.get("id") or ""))
            if op == "add" and text:
                same = by_text.get(_norm(text))
                if same is not None:  # already there: more evidence for it (a rejected one stays rejected)
                    if same.status in ("active", "proposed"):
                        same.confirmed_count = (same.confirmed_count or 0) + max(1, len(cases))
                        same.evidence = json.dumps(_paths(same.evidence) + cases)
                        done["confirmed"] += 1
                    continue
                paths = [str(p).strip()[:200] for p in (ch.get("paths") or []) if str(p).strip()][:10] if isinstance(ch.get("paths"), list) else []
                row = ReviewLesson(id=new_id(), scope="*" if ch.get("scope") == "all" else full, paths=json.dumps(paths), text=text,
                                   status="proposed", source="distilled", rationale=why, evidence=json.dumps(cases),
                                   created_at=_now(), updated_at=_now())
                s.add(row)
                lessons[row.id], by_text[_norm(text)] = row, row
                done["added"] += 1
            elif op == "revise" and target is not None and text and target.status in ("active", "off"):
                if _norm(text) in by_text:
                    continue
                row = ReviewLesson(id=new_id(), scope=target.scope, paths=target.paths, text=text, status="proposed", source="distilled",
                                   rationale=why, evidence=json.dumps(cases), replaces=target.id, created_at=_now(), updated_at=_now())
                s.add(row)
                by_text[_norm(text)] = row
                done["revised"] += 1
            elif op == "confirm" and target is not None:
                target.confirmed_count = (target.confirmed_count or 0) + max(1, len(cases))
                target.evidence = json.dumps(_paths(target.evidence) + cases)
                done["confirmed"] += 1
            elif op == "contradict" and target is not None:
                target.contradicted_count = (target.contradicted_count or 0) + max(1, len(cases))
                if target.status == "active":
                    target.flagged = why or "recent reviews disagree with it"
                done["contradicted"] += 1
    return done


async def distill(ctx: AppContext) -> dict[str, Any]:
    """Read the cases not distilled yet (per repository) and propose lesson changes; one run at a time. Called while
    a run is going, it asks that run for another pass, so cases recorded meanwhile are not left waiting."""
    global _again
    if _lock.locked():
        _again = True
        return load_state(ctx)
    async with _lock:
        _failed.clear()
        run: dict[str, Any] = {"totals": {"cases": 0, "added": 0, "revised": 0, "confirmed": 0, "contradicted": 0}, "errors": {}}
        await _distill_once(ctx, run)
        while _again and pending_cases(ctx, exclude_failed=True):
            await _distill_once(ctx, run)
        _again = False
    return load_state(ctx)


async def _distill_once(ctx: AppContext, run: dict[str, Any]) -> None:
    """One pass over the cases not distilled yet, each repository on its own: a repository whose model answer is
    unusable keeps its cases (and is reported) while the others go on; it is left out of the follow-up passes of
    this run, so a broken answer cannot loop."""
    from .models import ReviewCase, ReviewLesson

    global _again
    _again = False
    totals, errors = run["totals"], run["errors"]  # over all the passes of this run
    with ctx.db.read() as s:
        q = s.query(ReviewCase).filter(ReviewCase.distilled.is_(False))
        if _failed:
            q = q.filter(ReviewCase.repo.notin_(list(_failed)))
        # the oldest per repository first, BATCH cases at most per repository (one repository cannot starve the rest)
        repos: dict[str, list[dict[str, Any]]] = {}
        for c in q.order_by(ReviewCase.created_at):
            cs = repos.setdefault(c.repo, [])
            if len(cs) < BATCH:
                cs.append(_case_dict(c))
    for repo, cs in repos.items():
        try:
            with ctx.db.read() as s:
                ls = [lesson_dict(r) for r in s.query(ReviewLesson).filter(ReviewLesson.scope.in_([repo, "*"]))]
            data = parse_json(await ask_model(ctx, distill_prompt(repo, cs, ls), f"Review learning: {repo}"))
            if data is None:
                raise ApiError(502, "BAD_ANSWER", "the model did not answer with JSON")
            got = apply_changes(ctx, repo, data.get("changes") if isinstance(data, dict) else data, [c["id"] for c in cs])
            with ctx.db.write() as s:
                s.query(ReviewCase).filter(ReviewCase.id.in_([c["id"] for c in cs])).update({"distilled": True}, synchronize_session=False)
            totals["cases"] += len(cs)
            for k, v in got.items():
                totals[k] += v
        except Exception as exc:  # noqa: BLE001 — shown in the settings; this repository's cases stay for the next run
            msg = exc.message if isinstance(exc, ApiError) else str(exc)[:300]
            log.warning("review learning: distilling %s failed: %s", repo, msg)
            errors[repo] = msg
            _failed.add(repo)
    state = {"error": "; ".join(f"{r}: {m}" for r, m in errors.items()) or None, "errors": errors,
             "last_run": _iso(_now()), "last": totals}
    kv_put(ctx, STATE_KEY, state)


_again = False  # cases came in during a run: another pass when it ends
_failed: set[str] = set()  # repositories whose answer failed in the current run (cleared when a run starts)


def load_state(ctx: AppContext) -> dict[str, Any]:
    """The last run, and whether one is running now — from this process (a stored flag would outlive a restart)."""
    st = {"error": None, "errors": {}, "last_run": None, "last": None, **(kv_get(ctx, STATE_KEY, {}) or {})}
    st["running"] = _lock.locked()
    return st


def pending_cases(ctx: AppContext, exclude_failed: bool = False) -> int:
    from .models import ReviewCase

    with ctx.db.read() as s:
        q = s.query(ReviewCase).filter(ReviewCase.distilled.is_(False))
        if exclude_failed and _failed:
            q = q.filter(ReviewCase.repo.notin_(list(_failed)))
        return q.count()


def distill_soon(ctx: AppContext) -> None:
    """After a published review (when distilling is automatic): in the background."""
    global _again
    st = load_settings(ctx)
    if not st["auto"] or not pending_cases(ctx):
        return
    if _lock.locked():  # a run is going: it takes another pass for these cases when it ends
        _again = True
        return
    try:
        t = asyncio.get_running_loop().create_task(distill(ctx))
    except RuntimeError:
        return
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def idle() -> None:
    """Wait for background distilling (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ---------------------------------------------------------------------- routes

class SettingsIn(BaseModel):
    enabled: bool | None = None
    auto: bool | None = None
    reviewers: bool | None = None
    model: dict[str, Any] | None = None
    max_lessons: int | None = None
    max_chars: int | None = None
    examples: int | None = None
    min_cases: int | None = None


class LessonIn(BaseModel):
    scope: str = "*"
    text: str
    paths: list[str] = []


class LessonEditIn(BaseModel):
    text: str | None = None
    scope: str | None = None
    paths: list[str] | None = None
    status: str | None = None
    clear_flag: bool = False


class TeachIn(BaseModel):
    repo: str
    scope: str = "repo"  # repo | all
    text: str
    paths: list[str] = []
    number: int | None = None
    path: str = ""
    line: int | None = None
    line_text: str = ""
    body: str = ""  # the comment it was taught from
    author: str | None = None


class TeachDraftIn(BaseModel):
    repo: str
    body: str
    path: str = ""
    line_text: str = ""
    author: str | None = None


def _scope(v: str) -> str:
    v = (v or "*").strip().lower()
    if v != "*" and not re.fullmatch(r"[\w.-]+/[\w.-]+", v):
        raise ApiError(400, "INVALID_SCOPE", "scope is owner/name or *")
    return v


def _clean_paths(ps: list[str]) -> list[str]:
    return [p.strip()[:200] for p in ps if p and p.strip()][:10]


def overview(ctx: AppContext) -> dict[str, Any]:
    from .models import ReviewLesson

    with ctx.db.read() as s:
        rows = s.query(ReviewLesson).order_by(ReviewLesson.created_at.desc()).all()
    texts = {r.id: r.text for r in rows}
    lessons = [lesson_dict(r, texts.get(r.replaces or "")) for r in rows]
    return {"settings": load_settings(ctx), "state": load_state(ctx), "pending_cases": pending_cases(ctx), "lessons": lessons,
            "proposed": sum(1 for x in lessons if x["status"] == "proposed"), "flagged": sum(1 for x in lessons if x["flagged"] and x["status"] == "active"),
            "stats": stats(ctx), "reasons": REASONS}


@router.get("/review-learning")
def get_overview(auth: tuple[AppContext, str] = Depends(require_owner)):
    return overview(auth[0])


@router.get("/review-learning/summary")
def get_summary(auth: tuple[AppContext, str] = Depends(require_owner)):
    from .models import ReviewLesson

    with auth[0].db.read() as s:
        n = s.query(ReviewLesson).filter_by(status="proposed").count()
        f = s.query(ReviewLesson).filter(ReviewLesson.status == "active", ReviewLesson.flagged.isnot(None)).count()
    # running: a distill is going (the UI asks again until it ends, then shows the lessons it proposed)
    return {"proposed": n, "flagged": f, "running": _lock.locked() or bool(_tasks)}


@router.put("/review-learning/settings")
def put_settings(body: SettingsIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx = auth[0]
    s = clean_settings(body.model_dump(exclude_none=True), load_settings(ctx))
    kv_put(ctx, SETTINGS_KEY, s)
    return s


@router.post("/review-learning/distill")
async def post_distill(auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx = auth[0]
    if not pending_cases(ctx):
        return {**load_state(ctx), "nothing": True}
    t = asyncio.get_running_loop().create_task(distill(ctx))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    await asyncio.sleep(0)
    return {**load_state(ctx), "running": True}


@router.post("/review-learning/lessons", status_code=201)
def add_lesson(body: LessonIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    from .db import new_id
    from .models import ReviewLesson

    text = body.text.strip()
    if not text:
        raise ApiError(400, "INVALID_LESSON", "a lesson needs text")
    with auth[0].db.write() as s:
        r = ReviewLesson(id=new_id(), scope=_scope(body.scope), paths=json.dumps(_clean_paths(body.paths)), text=text[:1000], status="active",
                         source="manual", created_at=_now(), updated_at=_now())
        s.add(r)
        s.flush()
        return lesson_dict(r)


@router.patch("/review-learning/lessons/{lesson_id}")
def edit_lesson(lesson_id: str, body: LessonEditIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    from .models import ReviewLesson

    with auth[0].db.write() as s:
        r = s.get(ReviewLesson, lesson_id)
        if r is None:
            raise ApiError(404, "LESSON_NOT_FOUND", "lesson not found")
        if body.text is not None:
            if not body.text.strip():
                raise ApiError(400, "INVALID_LESSON", "a lesson needs text")
            r.text = body.text.strip()[:1000]
        if body.scope is not None:
            r.scope = _scope(body.scope)
        if body.paths is not None:
            r.paths = json.dumps(_clean_paths(body.paths))
        if body.clear_flag:
            r.flagged = None
        if body.status is not None:
            if body.status not in STATUSES:
                raise ApiError(400, "INVALID_STATUS", "status must be proposed, active, off or rejected")
            if body.status == "active" and r.status == "proposed" and r.replaces:  # an accepted revision replaces its lesson
                old = s.get(ReviewLesson, r.replaces)
                if old is not None:
                    old.status = "off"
                    r.used_count, r.confirmed_count = old.used_count, old.confirmed_count
                    r.evidence = json.dumps(_paths(old.evidence) + _paths(r.evidence))
            r.status = body.status
            if body.status != "active":
                r.flagged = None
        r.updated_at = _now()
        s.flush()
        return lesson_dict(r)


@router.delete("/review-learning/lessons/{lesson_id}")
def delete_lesson(lesson_id: str, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    from .models import ReviewLesson

    with auth[0].db.write() as s:
        r = s.get(ReviewLesson, lesson_id)
        if r is None:
            raise ApiError(404, "LESSON_NOT_FOUND", "lesson not found")
        s.delete(r)
    return {"deleted": lesson_id}


@router.get("/review-learning/lessons/{lesson_id}/evidence")
def lesson_evidence(lesson_id: str, auth: tuple[AppContext, str] = Depends(require_owner)):
    from .models import ReviewCase, ReviewLesson

    with auth[0].db.read() as s:
        r = s.get(ReviewLesson, lesson_id)
        if r is None:
            raise ApiError(404, "LESSON_NOT_FOUND", "lesson not found")
        ids = _paths(r.evidence)
        cases = s.query(ReviewCase).filter(ReviewCase.id.in_(ids)).order_by(ReviewCase.created_at.desc()).all() if ids else []
        return {"cases": [_case_dict(c) for c in cases]}


@router.post("/review-learning/teach", status_code=201)
def teach(body: TeachIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """A lesson you write from a comment ("Remember for future reviews"): active at once, with its case as evidence."""
    from .db import new_id
    from .models import ReviewLesson

    repo = _scope(body.repo)
    if repo == "*":
        raise ApiError(400, "INVALID_SCOPE", "repo is owner/name")
    text = body.text.strip()
    if not text:
        raise ApiError(400, "INVALID_LESSON", "a lesson needs text")
    with auth[0].db.write() as s:
        c = _case(repo=repo, number=body.number or 0, path=body.path, line=body.line, line_text=body.line_text[:2000], source="taught",
                  outcome="taught", final_body=(body.body or text)[:4000], author=body.author, distilled=True)
        s.add(c)
        r = ReviewLesson(id=new_id(), scope="*" if body.scope == "all" else repo, paths=json.dumps(_clean_paths(body.paths)), text=text[:1000],
                         status="active", source="taught", evidence=json.dumps([c.id]), created_at=_now(), updated_at=_now())
        s.add(r)
        s.flush()
        return lesson_dict(r)


@router.post("/review-learning/teach/draft")
async def teach_draft(body: TeachDraftIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """The model's wording of a lesson from one comment (you edit it before saving)."""
    who = f" by {body.author}" if body.author else ""
    prompt = "\n\n".join([
        "WAW-REVIEW-TEACH",
        f"An AI code reviewer reviews pull requests of {body.repo} for a user. The user wants it to learn from this review comment{who} "
        f"and write comments like it where they fit. Write ONE short, general, actionable lesson for the reviewer (one or two "
        f"sentences, addressed to the reviewer: what to look for and how to comment), not about this PR only.",
        f"File: {body.path or '(the PR)'}" + (f"\nCode:\n{body.line_text[:1500]}" if body.line_text else ""),
        f"Comment:\n{body.body[:4000]}",
        'Reply with ONLY a JSON object: {"text": "<the lesson>"}',
    ])
    data = parse_json(await ask_model(auth[0], prompt, f"Review learning: {body.repo}"))
    text = str((data or {}).get("text") or "").strip() if isinstance(data, dict) else ""
    if not text:
        raise ApiError(502, "BAD_ANSWER", "the model did not suggest a lesson")
    return {"text": text[:1000]}

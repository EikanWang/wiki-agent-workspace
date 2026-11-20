"""The summary / highlight / report files: indexed by their first line (so the history copied over and the files the
app writes are read alike)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import settings as st

HEADER = re.compile(r"^Summary of (?P<repo>[\w.-]+/[\w.-]+) from (?P<a>\d{4}-\d{2}-\d{2})[ T_]?[\d:-]* to (?P<b>\d{4}-\d{2}-\d{2})")
HL_HEADER = re.compile(r"Highlights of (?P<repo>[\w.-]+/[\w.-]+) from (?P<a>\d{4}-\d{2}-\d{2})\S* to (?P<b>\d{4}-\d{2}-\d{2})(?:[_ T](?P<bt>\d{2}:\d{2}))?")
_index: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _scan(folder: Path, kind: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(folder.glob("*.md" if kind == "summary" else "*.json")):
        try:
            with p.open(encoding="utf-8", errors="replace") as fh:
                head = fh.read(400 if kind == "summary" else 300)
                m = (HEADER if kind == "summary" else HL_HEADER).search(head)
                if not m:
                    continue
                fresh = [new_key(n) for n in new_names(head + fh.read())] if kind == "summary" else []
        except OSError:
            continue
        day = m["b"]
        if kind == "highlight" and (m.groupdict().get("bt") or "23:59") < "06:00":  # a window ending early in the morning reports the day before
            day = (dt.date.fromisoformat(m["b"]) - dt.timedelta(days=1)).isoformat()
        out.append({"path": str(p), "name": p.name, "repo": m["repo"], "since": m["a"], "until": m["b"], "day": day, "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime, "daily": m["a"] == m["b"], "partial": ".partial." in p.name, "new": len(fresh), "new_names": fresh})
    return out


def index(ctx: Any, repo: str, kind: str = "summary") -> list[dict[str, Any]]:
    folder = st.data_dir(ctx) / kind / repo
    if not folder.is_dir():
        return []
    key = f"{folder}"
    stamp = folder.stat().st_mtime
    hit = _index.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    rows = _scan(folder, kind)
    _index[key] = (stamp, rows)
    return rows


def body(path: str) -> str:
    """A summary's text without its first line."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    first, _, rest = text.partition("\n")
    return rest.strip() if HEADER.search(first) else text.strip()


def daily(ctx: Any, repo: str, day: str) -> dict[str, Any] | None:
    """The newest complete daily summary of the day."""
    rows = [r for r in index(ctx, repo) if r["daily"] and r["since"] == day and not r["partial"]]
    return max(rows, key=lambda r: r["mtime"]) if rows else None


NEW_NAME = re.compile(r"🆕\s*\*\*([^*\n]{2,120})\*\*")


def new_key(name: str) -> str:
    """A new thing's name as its read mark keeps it (as the page shows it: no backticks; spaces collapsed, lower case)."""
    return " ".join(name.replace("`", "").split()).strip(" :").lower()


_NOTE = re.compile(r"\s*\((?:new|now|added|introduced|experimental|beta|prototype|rfc|proposal)\b[^)]*\)\s*$")


def match_key(name: str) -> str:
    """A name as "seen before?" compares it: case, backticks, punctuation, "()" and a trailing note such as
    "(new feature)" do not count ("`RVV` fused-Adam()" is "rvv fused adam")."""
    s = _NOTE.sub("", name.lower()).replace("()", " ")
    return " ".join(re.sub(r"[^\w]+|_", " ", s).split())


def new_names(text: str) -> list[str]:
    """The names a summary or report marks as new ("🆕 **name**"), in order, without duplicates."""
    seen: dict[str, str] = {}
    for m in NEW_NAME.finditer(text):
        name = " ".join(m.group(1).split()).strip(" :")
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return list(seen.values())


def flagged_new(ctx: Any, repo: str, before: str, days: int = 30, limit: int = 200) -> list[str]:
    """The names flagged "🆕" in the daily summaries of the `days` days before `before`, newest first, without duplicates."""
    lo = (dt.date.fromisoformat(before) - dt.timedelta(days=days)).isoformat()
    rows = sorted((r for r in index(ctx, repo) if r["daily"] and not r["partial"] and lo <= r["since"] < before),
                  key=lambda r: (r["since"], r["mtime"]), reverse=True)
    seen: dict[str, str] = {}
    for r in rows:
        try:
            text = Path(r["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in new_names(text):
            if name.lower() not in seen:
                seen[name.lower()] = name
                if len(seen) >= limit:
                    return list(seen.values())
    return list(seen.values())


def unmark(text: str, keys: set[str]) -> tuple[str, list[str]]:
    """Takes the "🆕" off the names whose `match_key` is in `keys` (seen before, so not new); in a "🆕 What's new" section, a bullet whose
    every new thing was seen before is dropped (with its sub-bullets that mark nothing new). Returns the text and the
    names unmarked."""
    keys = keys - {""}
    if not keys:
        return text, []
    out, dropped, in_new, cut = [], {}, False, None
    for line in text.split("\n"):
        indent = len(line) - len(line.lstrip())
        if cut is not None and line.strip() and indent > cut and "🆕" not in line:
            continue
        cut = None
        if line.lstrip().startswith("#"):
            in_new = "🆕" in line
            out.append(line)
            continue
        marks = list(NEW_NAME.finditer(line))
        seen = [m for m in marks if match_key(m.group(1)) in keys]
        if not seen:
            out.append(line)
            continue
        for m in seen:
            dropped.setdefault(new_key(m.group(1)), " ".join(m.group(1).split()).strip(" :"))
        if in_new and len(seen) == len(marks) and line.lstrip()[:1] in ("-", "*"):
            cut = indent
            continue
        for m in reversed(seen):
            line = line[:m.start()] + f"**{m.group(1)}**" + line[m.end():]
        out.append(line)
    return "\n".join(out), list(dropped.values())


def _read_file(ctx: Any, repo: str) -> Path:
    return st.data_dir(ctx) / "read" / f"{repo}.json"


def read_new(ctx: Any, repo: str) -> dict[str, list[str]]:
    """The new things marked read, per file ("f:<summary file name>") or report ("r:<id>"): lower-case names."""
    try:
        d = json.loads(_read_file(ctx, repo).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_read(ctx: Any, repo: str, key: str, names: list[str], read: bool) -> list[str]:
    """Marks these new things read (or unread again) in one file or report; returns what is read there now."""
    d = read_new(ctx, repo)
    have = dict.fromkeys(d.get(key) or [])
    for n in map(new_key, names):
        if read:
            have[n] = None
        else:
            have.pop(n, None)
    d[key] = list(have)
    if not d[key]:
        del d[key]
    p = _read_file(ctx, repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    os.replace(tmp, p)
    return d.get(key, [])


def unread(names: list[str], read: list[str] | None) -> int:
    done = set(read or [])
    return sum(1 for n in names if new_key(n) not in done)


def previous_day_known(ctx: Any, repo: str, day: str) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    """The day before `day`: its date, its daily summary (if any) and the names it flagged new."""
    prev = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
    row = daily(ctx, repo, prev)
    if row is None:
        return prev, None, []
    try:
        return prev, row, new_names(Path(row["path"]).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return prev, row, []


def write_check(ctx: Any, repo: str, day: str, chk: dict[str, Any]) -> None:
    """The new-things check of a day's summary (what the model decided for each mark)."""
    folder = st.data_dir(ctx) / "check" / repo
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{day}.json"
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(chk, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def write_summary(ctx: Any, repo: str, since: str, until: str, text: str, partial: bool = False) -> Path:
    owner, name = repo.split("/", 1)
    folder = st.data_dir(ctx) / "summary" / repo
    folder.mkdir(parents=True, exist_ok=True)
    days = (dt.date.fromisoformat(until) - dt.date.fromisoformat(since)).days
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    tail = f"{until}_23-59-59" if days == 0 else stamp
    p = folder / f"summary_{owner}_{name}_{days}D_{tail}{'.partial' if partial else ''}.md"
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(f"Summary of {repo} from {since} 00-00-00 to {until} 23-59-59:\n\n{text.strip()}\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def _hl_items(r: dict[str, Any]) -> list[dict[str, Any]]:
    """A highlight file's items; a score left at -1 but present in the justification is taken from there."""
    from .llm import parse_score

    try:
        data = json.loads(Path(r["path"]).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for it in data[1:] if data and isinstance(data[0], dict) and "File Information" in data[0] else data:
        if not isinstance(it, dict) or not it.get("url"):
            continue
        score = it.get("xpu_relevance_score", -1)
        just = it.get("xpu_relevance_justification")
        if not isinstance(score, int) or score < 0:
            parsed = parse_score(just if isinstance(just, str) else json.dumps(just))
            score, just = parsed["score"], parsed["justification"]
        out.append({**it, "xpu_relevance_score": score, "xpu_relevance_justification": just, "file": r["name"]})
    return out


def _score(x: dict[str, Any]) -> int:
    return x.get("xpu_relevance_score", -1)


def highlights_by_day(ctx: Any, repo: str, since: str, until: str) -> list[dict[str, Any]]:
    """Per day (newest first): the highlight runs reporting that day and their items — one per URL within the day (the
    newest run wins) — each marked `seen_before` when an earlier day of the period already had it (the runs' windows
    overlap, e.g. 48 hours)."""
    days: dict[str, list[dict[str, Any]]] = {}
    for r in index(ctx, repo, "highlight"):
        if since <= r["day"] <= until:
            days.setdefault(r["day"], []).append(r)
    out = []
    for day in sorted(days):
        runs = sorted(days[day], key=lambda r: r["mtime"], reverse=True)
        seen: dict[str, dict[str, Any]] = {}
        for r in runs:
            for it in _hl_items(r):
                seen.setdefault(it["url"], it)
        out.append({"day": day, "files": [{"name": r["name"], "since": r["since"], "until": r["until"]} for r in runs],
                    "items": sorted(seen.values(), key=_score, reverse=True)})
    earlier: set[str] = set()
    for d in out:  # oldest first
        for it in d["items"]:
            it["seen_before"] = it["url"] in earlier
        earlier |= {it["url"] for it in d["items"]}
    return out[::-1]


def highlights(ctx: Any, repo: str, since: str, until: str) -> list[dict[str, Any]]:
    """Flat: every highlighted item of the period once (by URL; the newest run's copy), with the days it was on."""
    seen: dict[str, dict[str, Any]] = {}
    for d in highlights_by_day(ctx, repo, since, until):  # newest day first
        for it in d["items"]:
            if it["url"] in seen:
                seen[it["url"]]["days"].append(d["day"])
            else:
                seen[it["url"]] = {**{k: v for k, v in it.items() if k != "seen_before"}, "days": [d["day"]]}
    return sorted(seen.values(), key=_score, reverse=True)


def write_highlights(ctx: Any, repo: str, since: str, until: str, items: list[dict[str, Any]]) -> Path:
    owner, name = repo.split("/", 1)
    folder = st.data_dir(ctx) / "highlight" / repo
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"highlight_{owner}_{name}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.json"
    head = {"File Information": f"Highlights of {repo} from {since}_00:00:00 to {until}_23:59:59"}
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps([head, *items], indent=4, ensure_ascii=False))
    os.replace(tmp, p)
    return p

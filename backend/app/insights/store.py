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
        except OSError:
            continue
        m = (HEADER if kind == "summary" else HL_HEADER).search(head)
        if not m:
            continue
        day = m["b"]
        if kind == "highlight" and (m.groupdict().get("bt") or "23:59") < "06:00":  # a window ending early in the morning reports the day before
            day = (dt.date.fromisoformat(m["b"]) - dt.timedelta(days=1)).isoformat()
        out.append({"path": str(p), "name": p.name, "repo": m["repo"], "since": m["a"], "until": m["b"], "day": day, "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime, "daily": m["a"] == m["b"], "partial": ".partial." in p.name})
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

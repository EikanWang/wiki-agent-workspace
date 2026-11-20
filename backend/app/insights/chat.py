"""Trend chats: a conversation with the agent about one repository's Repo Insights data.

A trend chat is a folder chat (`.waw-chat/…`, a hidden-from-Chats document of the primary wiki folder) marked with
the repository it is about. Its agent works in the insights data folder (`data_dir`), reads the daily summaries,
reports, highlights and fetched items there, and answers from them (e.g. "summarize Oct 1 – Nov 15").
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..kv import kv_get, kv_put
from . import pipeline as pl
from . import settings as st
from . import store


def _key(workspace_id: str, norm: str) -> str:
    return f"chat_trend:{workspace_id}:{norm}"


def _list_key(repo: str) -> str:
    return f"insights_chats:{repo}"


def trend_of(ctx: Any, workspace_id: str, norm: str) -> str | None:
    """The repository a chat is about when it is a trend chat."""
    v = kv_get(ctx, _key(workspace_id, norm), None)
    return v if isinstance(v, str) and st.REPO.match(v) else None


def mark(ctx: Any, repo: str, workspace_id: str, norm: str) -> None:
    kv_put(ctx, _key(workspace_id, norm), repo)
    rows = [x for x in kv_get(ctx, _list_key(repo), []) or [] if isinstance(x, dict)]
    rows.append({"workspace_id": workspace_id, "path": norm})
    kv_put(ctx, _list_key(repo), rows)


def listed(ctx: Any, repo: str) -> list[dict[str, str]]:
    return [x for x in kv_get(ctx, _list_key(repo), []) or [] if isinstance(x, dict) and x.get("path") and x.get("workspace_id")]


def _days(rows: list[dict[str, Any]]) -> str:
    days = sorted({r["since"] for r in rows if r["daily"] and not r["partial"]})
    if not days:
        return "none yet"
    a, b = dt.date.fromisoformat(days[0]), dt.date.fromisoformat(days[-1])
    have = set(days)
    gaps = [(a + dt.timedelta(days=i)).isoformat() for i in range((b - a).days + 1) if (a + dt.timedelta(days=i)).isoformat() not in have]
    out = f"{days[0]} … {days[-1]} ({len(days)} days)"
    if gaps:
        out += f"; missing: {', '.join(gaps[:40])}{' …' if len(gaps) > 40 else ''}"
    return out


def catalog(ctx: Any, repo: str) -> str:
    """What exists for `repo` (so the agent knows where to look without listing folders first)."""
    rows = store.index(ctx, repo)
    longer = sorted((r for r in rows if not r["daily"]), key=lambda r: (r["since"], r["until"]))
    lines = [f"- Daily summaries: {_days(rows)}."]
    if longer:
        lines.append("- Longer summaries (summary/<repo>/, building blocks of reports): "
                     + "; ".join(f"{r['since']}…{r['until']} `{r['name']}`" for r in longer[-30:]))
    reps = [r for r in pl.reports(ctx, repo) if r.get("status") == "done"]
    if reps:
        lines.append("- Reports (reports/<repo>/<id>.md): " + "; ".join(
            f"{r.get('label') or ''} {r['since']}…{r['until']} `{r['id']}.md`".strip() for r in reps[:40]))
    return "\n".join(lines)


def build_trend_prompt(ctx: Any, *, repo: str, question: str, prior: list[dict[str, str]] | None, brief: str | None = None,
                       quote: str | None = None, main: list[dict[str, str]] | None = None) -> str:
    """The prompt of a trend chat's turn (`quote` + `main`: a side question about a part of an answer)."""
    from .github import now

    root = st.data_dir(ctx)
    today = now().date().isoformat()
    others = sorted({r["repo"] for r in st.load(ctx)["repos"]} - {repo})
    parts = [
        f"You are chatting with the user about the GitHub repository {repo}, using the Repo Insights data folder that is your "
        f"working directory ({root}). It holds what this app collected and summarized from GitHub (issues, pull requests, "
        "RFCs, commits) day by day. Paths below are relative to it; <repo> is the owner/name path of a repository.",
        "Layout:\n"
        "- summary/<repo>/summary_<owner>_<name>_0D_<YYYY-MM-DD>_23-59-59.md — the daily summary of one UTC day (first line: "
        "`Summary of <repo> from <day> 00-00-00 to <day> 23-59-59:`). `.partial.` in the name: the day was not over when made. "
        "Files with another `<N>D` cover N+1 days (week / month blocks).\n"
        "- reports/<repo>/<id>.md (+ <id>.json: label, since, until, kind) — reports the user made for a period "
        "(weekly, monthly, custom), built from the daily summaries.\n"
        "- highlight/<repo>/*.json — XPU / Intel highlights (scored items).\n"
        "- items/<repo>/<number>.json — the fetched issue / PR (title, body, author, comments, reviews); "
        "items/<repo>/days/<YYYY-MM-DD>.json — the numbers active on that day.\n"
        "- repos/<repo>.git — a git clone (history: `git -C repos/<repo>.git log …`).",
        f"What exists for {repo} now:\n{catalog(ctx, repo)}",
        f"Today is {today} (UTC; days are UTC days; the user reads times in UTC+{int(st.load(ctx).get('timezone_offset', 8))})."
        + (f" Other repositories in this folder: {', '.join(others)}." if others else ""),
        "How to work: answer from these files — find the ones covering the period asked (by the dates in the names), read them, "
        "and say which days or reports you used and which days are missing. For a long period, prefer existing reports and "
        "longer summaries where they fit and fill the rest from daily summaries; read in batches and condense as you go. "
        "Keep the GitHub links (issues, PRs, people) the summaries have. The `gh` CLI may be used for something the files "
        "do not have; say when you did. Do not modify or delete files in this folder unless the user explicitly asks.",
    ]
    if brief:
        parts.append(brief)
    if main:
        parts.append("The main conversation (context):")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in main[-30:])
    if quote is not None:
        parts += ["The user selected this part of an answer and asks a side question about it:",
                  "\n".join(f"> {line}" for line in quote[:20000].splitlines() or [""])]
    if prior:
        parts.append("The conversation so far:" if quote is None else "The side discussion so far:")
        parts.extend(f"[{m['role']}] {m['content'][:6000]}" for m in prior[-40:])
    parts += ["User message:", question, "Instructions: answer the user in Markdown."]
    return "\n\n".join(parts)

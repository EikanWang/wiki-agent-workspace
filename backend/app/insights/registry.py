"""The term registry of a repository (what its daily summaries were about, with what each thing is) and the model check
of what a report or daily summary marks as new against it: repeat, duplicate, extension or new."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from typing import Any

from ..errors import ApiError
from . import llm, prompts, store

TOK = re.compile(r"[a-z0-9]+")
NEAR = 8          # registry entries shown with each candidate
BATCH = 20        # candidates per model call
REPEAT_MIN = 60   # the confidence a repeat needs to lose its mark


def entries_of(raw: Any) -> list[dict[str, Any]]:
    """The described terms of a day as the model gave them (a bare name is a term without a description)."""
    out = []
    for t in raw if isinstance(raw, list) else []:
        if isinstance(t, str):
            t = {"name": t}
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"].strip():
            continue
        name = " ".join(t["name"].split())[:120]
        aliases = [" ".join(a.split())[:120] for a in t.get("aliases") or [] if isinstance(a, str) and a.strip()][:12]
        out.append({"name": name, "aliases": [a for a in aliases if a != name],
                    **{k: " ".join(str(t.get(k) or "").split())[:400] for k in ("kind", "area", "what")},
                    "refs": [r for r in t.get("refs") or [] if isinstance(r, int)][:3]})
    return out[:400]


def _specific(k: str) -> bool:
    return len(k.split()) >= 2 or len(k) >= 10


def build(days: list[tuple[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """One entry per thing, from the days' terms (oldest day first). A term joins an entry only by its main name, by its
    name being one of the entry's other spellings, or by one of its other spellings being the entry's main name —
    never other spelling to other spelling, and other spellings only when specific (two words or ten characters). The
    other spellings of a term that joined by a spelling are not taken up for matching (no chains of spellings)."""
    reg: list[dict[str, Any]] = []
    main: dict[str, int] = {}
    alias: dict[str, int] = {}
    for day, terms in days:
        for t in terms:
            k = store.match_key(t["name"])
            if not k:
                continue
            aks = [a for a in (store.match_key(x) for x in t.get("aliases") or []) if a and a != k and _specific(a)]
            i = main.get(k)
            direct = i is not None
            if i is None:
                i = alias.get(k) if _specific(k) else None
            if i is None:
                i = next((main[a] for a in aks if a in main), None)
            if i is None:
                i, direct = len(reg), True
                main[k] = i
                reg.append({"name": t["name"], "aliases": [], "kind": "", "area": "", "what": "", "first": day, "last": day, "days": 0})
            e = reg[i]
            if e["last"] != day or not e["days"]:
                e["days"] += 1
            e["last"] = day
            for f in ("kind", "area", "what"):
                e[f] = t.get(f) or e[f]
            for n in [t["name"], *(t.get("aliases") or [])]:
                if n != e["name"] and n not in e["aliases"] and len(e["aliases"]) < 20:
                    e["aliases"].append(n)
            for a in aks if direct else []:
                alias.setdefault(a, i)
    return reg


def _toks(s: str) -> list[str]:
    return TOK.findall(s.lower().replace("_", " "))


def nearest(reg: list[dict[str, Any]], queries: list[list[str]], k: int = NEAR) -> list[list[int]]:
    """The `k` entries closest to each query (BM25 over the names, the other spellings and what each is)."""
    if not reg:
        return [[] for _ in queries]
    post: dict[str, list[tuple[int, int]]] = {}
    lens = []
    for i, e in enumerate(reg):
        d = Counter(_toks(" ".join([e["name"], *e["aliases"]])) * 3 + _toks(e["what"]))
        lens.append(sum(d.values()))
        for t, c in d.items():
            post.setdefault(t, []).append((i, c))
    n, avg = len(reg), (sum(lens) / len(reg)) or 1
    out = []
    for q in queries:
        scores: dict[int, float] = {}
        for t, w in Counter(q).items():
            p = post.get(t)
            if not p:
                continue
            idf = math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5))
            for i, c in p:
                scores[i] = scores.get(i, 0) + idf * w * c * 2.2 / (c + 1.2 * (0.25 + 0.75 * lens[i] / avg))
        out.append(sorted(scores, key=lambda i: -scores[i])[:k])
    return out


_LINK = re.compile(r"\(\[[^]]*\]\([^)]*\)\)|\[([^]]*)\]\([^)]*\)")


def candidates(text: str) -> list[dict[str, Any]]:
    """What the text marks as new, once each, with the line it is marked in."""
    out, seen = [], set()
    for line in text.split("\n"):
        for m in store.NEW_NAME.finditer(line):
            k = store.match_key(m.group(1))
            if k and k not in seen:
                seen.add(k)
                ctx = re.sub(r"^\s*(?:#+|[-*+]|\d+\.)\s+", "", _LINK.sub(r"\1", line).replace("🆕 ", "").replace("🆕", "")).strip()
                out.append({"name": " ".join(m.group(1).split()).strip(" :"), "key": k, "context": ctx[:500]})
    return out


def _entry_line(j: int, e: dict[str, Any]) -> str:
    al = "; ".join(e["aliases"])[:200]
    desc = ", ".join(x for x in (e["kind"], e["area"]) if x)
    return (f"  [{j}] {e['name']}" + (f" (also: {al})" if al else "") + (f" — {desc}" if desc else "")
            + (f": {e['what'][:300]}" if e["what"] else "") + f" (seen {e['first']} … {e['last']}, {e['days']} day(s))")


def _as_json(raw: str) -> Any:
    return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])


async def check(ctx: Any, repo: str, text: str, reg: list[dict[str, Any]], what: str, baseline: str, concurrency: int = 4,
                ) -> tuple[str, dict[str, Any]]:
    """The model's verdict on each new thing of `text`; repeats (confidence ≥ REPEAT_MIN) and duplicates lose their
    mark, extensions are marked "➕ **name** *(extends …)*" (not counted as new). Returns the text and the check."""
    cands = candidates(text)
    if not cands or not reg:
        return text, {}
    near = nearest(reg, [_toks(c["name"]) * 2 + _toks(c["context"]) for c in cands])
    head = prompts.check(repo, what, baseline)
    names = "All candidates of this text: " + "; ".join(f"{i}. {c['name']}" for i, c in enumerate(cands, 1)) + "\n\n"
    sem = asyncio.Semaphore(max(1, concurrency))

    async def batch(b: int) -> None:
        blocks = [f"Candidate {i}: {c['name']}\n  Sentence: {c['context']}\n  Closest entries:\n"
                  + ("\n".join(_entry_line(j, reg[j]) for j in near[i - 1]) or "  (none)")
                  for i, c in enumerate(cands[b:b + BATCH], b + 1)]
        async with sem:
            for _ in range(2):
                try:
                    res = _as_json(await llm.complete(ctx, "check", head + names + "\n\n".join(blocks), as_json=True))["results"]
                    break
                except (ApiError, ValueError, KeyError, TypeError):
                    res = None
        for r in res if isinstance(res, list) else []:
            if isinstance(r, dict) and isinstance(r.get("id"), int) and b < r["id"] <= min(b + BATCH, len(cands)):
                cands[r["id"] - 1]["result"] = r
    await asyncio.gather(*(batch(b) for b in range(0, len(cands), BATCH)))

    drop: set[str] = set()
    ext: dict[str, str] = {}
    items = []
    for i, c in enumerate(cands, 1):
        r = c.get("result") or {}
        verdict = r.get("verdict") if r.get("verdict") in ("repeat", "extension", "duplicate", "new") else None
        try:
            conf = int(r.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0
        m = r.get("match")
        entry = reg[m]["name"] if isinstance(m, int) and 0 <= m < len(reg) and m in near[i - 1] else None
        same = r.get("same_as")
        if verdict == "duplicate" and not (isinstance(same, int) and 0 < same < i):
            verdict = "new"
        if verdict in ("repeat", "extension") and not entry:
            verdict = "new"
        if verdict == "repeat" and conf < REPEAT_MIN:
            verdict = "new"
        if verdict in ("repeat", "duplicate"):
            drop.add(c["key"])
        elif verdict == "extension":
            ext[c["key"]] = entry  # type: ignore[assignment]
        items.append({"name": c["name"], "verdict": verdict or "unchecked", "confidence": conf, "match": entry,
                      "same_as": cands[same - 1]["name"] if verdict == "duplicate" else None, "why": str(r.get("why") or "")[:300]})
    text, _ = store.unmark(text, drop)
    text = extend(text, ext)
    counts = Counter(x["verdict"] for x in items)
    return text, {"entries": len(reg), "counts": dict(counts), "items": items}


EXT_NAME = re.compile(r"➕\s*\*\*([^*\n]{2,120})\*\*")


def extend(text: str, ext: dict[str, str]) -> str:
    """Marks the new things in `ext` (match key → the entry each extends) as extensions: "➕ **name**", the first one
    followed by "*(extends Entry)*"."""
    if not ext:
        return text
    done: set[str] = set()

    def one(m: re.Match[str]) -> str:
        k = store.match_key(m.group(1))
        if k not in ext:
            return m.group(0)
        out = f"➕ **{m.group(1)}**"
        if k not in done:
            done.add(k)
            out += f" *(extends {ext[k]})*"
        return out
    return store.NEW_NAME.sub(one, text)


def summary(chk: dict[str, Any]) -> str:
    """The check as a line of the report's facts."""
    c = chk.get("counts") or {}
    return (f"New-things check (model, against {chk.get('entries', 0)} registry entries): {c.get('repeat', 0)} repeat, "
            f"{c.get('duplicate', 0)} duplicate (marks removed), {c.get('extension', 0)} extension (marked ➕, not counted as new), "
            f"{c.get('new', 0)} new" + (f", {c['unchecked']} unchecked" if c.get("unchecked") else ""))

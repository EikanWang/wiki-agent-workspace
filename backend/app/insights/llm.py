"""Model requests: an OpenAI-compatible chat endpoint (default: DeepSeek) or the app's agents (OpenCode / Codex),
chosen per task. Long material is split into parts of ≤ max_chars (agent_max_chars for a task on the agent) and each
part answered (the caller combines). The parts' answers are kept on disk until the summary is whole (a retry asks only
for what is missing), and a part the provider's safety filter refuses is halved until the texts it refuses are found.
Tests replace the whole thing with ctx.extra["insights_llm"] = callable(task, prompt, json) -> str."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..errors import ApiError
from . import settings as st

log = logging.getLogger("waw.insights")


async def complete(ctx: Any, task: str, prompt: str, *, as_json: bool = False, thinking: bool = False, repo: str | None = None) -> str:
    """One answer, by whoever does the task (for `repo`: its own choice when it has one)."""
    fake = ctx.extra.get("insights_llm")
    if fake is not None:
        out = fake(task, prompt, as_json)
        return await out if asyncio.iscoroutine(out) else out
    cfg = st.task_cfg(ctx, task, repo)
    if cfg.get("mode") == "agent":
        from ..review_priority import run_turn

        return await run_turn(ctx, cfg, prompt + ("\n\nReply with ONLY a JSON object." if as_json else ""), f"Insights: {task}")
    sec = st.secrets(ctx).get("llm") or {}
    key = sec.get("api_key")
    if not key:
        raise ApiError(409, "NO_LLM_KEY", "set the model API key in Repo Insights ⚙ (or choose the agent for this task)")
    body: dict[str, Any] = {"model": sec.get("model") or "deepseek-v4-flash", "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.0 if as_json else 1.0}
    if as_json:
        body["response_format"] = {"type": "json_object"}
    if thinking:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = "high"
    url = (sec.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    last = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=30)) as c:
                r = await c.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
            if r.status_code in (401, 403):
                raise ApiError(502, "LLM_AUTH", f"the model endpoint refused the API key ({r.status_code})")
            if r.status_code >= 400:
                last = f"{r.status_code}: {r.text[:300]}"
                raise httpx.HTTPError(last)
            return (r.json()["choices"][0]["message"].get("content") or "").strip()
        except ApiError:
            raise
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last = str(exc)[:300]
            log.warning("model request failed (attempt %d/3): %s", attempt + 1, last)
            await asyncio.sleep(2 ** attempt)
    raise ApiError(502, "LLM_FAILED", f"the model request failed: {last}")


def _groups(texts: list[str], instruction: str, max_chars: int, sep: str = "\n") -> list[list[tuple[int, str]]]:
    """The material in parts of ≤ max_chars (with the instruction), each text with its index; a single oversized text
    is cut, nothing is dropped."""
    room = max(10_000, max_chars - len(instruction))
    parts: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    size = 0
    for i, t in enumerate(texts):
        if len(t) > room:
            if cur:
                parts.append(cur)
                cur, size = [], 0
            parts.append([(i, t[:room] + "\n…(cut)")])
            continue
        if cur and size + len(t) + len(sep) > room:
            parts.append(cur)
            cur, size = [], 0
        cur.append((i, t))
        size += len(t) + len(sep)
    if cur:
        parts.append(cur)
    return parts


def pack(texts: list[str], instruction: str, max_chars: int, sep: str = "\n") -> list[str]:
    """Material in parts of ≤ max_chars (with the instruction); a single oversized text is cut, nothing is dropped."""
    return [sep.join(t for _, t in g) for g in _groups(texts, instruction, max_chars, sep)]


def request_chars(ctx: Any, task: str, repo: str | None = None) -> int:
    """The characters one request of the task may have: the agent's limit when the task runs on the agent."""
    s = st.load(ctx)
    agent = st.task_cfg(ctx, task, repo, s).get("mode") == "agent"
    return int(s["agent_max_chars"]) if agent else int(s["max_chars"])


BLOCKED = re.compile(r"blocked the response|safeguards? flagged|content[ _]?(?:filter|policy|management)|Content Exists Risk", re.I)
BLOCK_MAX = 24      # refused requests one summary may spend on finding the texts a provider refuses
KEPT_DAYS = 7       # how long the answers of a summary that never became whole stay on disk


def blocked(exc: ApiError) -> bool:
    """The provider refused the content (its safety filter) rather than failed: the same request is refused again."""
    return bool(BLOCKED.search(exc.message or ""))


async def _kept(ctx: Any, task: str, prompt: str, used: list[Path], repo: str | None = None) -> str:
    """`complete`, with the answer kept on disk (by the request) until the summary it belongs to is whole: a summary
    that failed at a later part does not pay for the earlier ones again when it is made anew."""
    p = st.data_dir(ctx) / "parts" / (hashlib.sha256(f"{task}\0{prompt}".encode()).hexdigest()[:32] + ".md")
    again = p in used  # the same request twice in one summary is asked twice
    used.append(p)
    if not again:
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            pass
    out = await complete(ctx, task, prompt, repo=repo)
    if out.strip():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, p)
    return out


def _drop_old(ctx: Any) -> None:
    old = time.time() - KEPT_DAYS * 86400
    for p in (st.data_dir(ctx) / "parts").glob("*.md"):
        try:
            if p.stat().st_mtime < old:
                p.unlink()
        except OSError:
            pass


async def summarize(ctx: Any, task: str, texts: list[str], instruction: str, combine: str | None = None,
                    skipped: list[int] | None = None, repo: str | None = None) -> str:
    """One answer for the material: each part summarized, then (more than one part) combined. With `skipped` (a list
    to fill), a part the provider refuses is halved until the texts it refuses are found: their indexes are added to
    `skipped` and the summary is made without them (at most BLOCK_MAX refused requests; then what is still refused
    is skipped together)."""
    max_chars = request_chars(ctx, task, repo)
    groups = _groups(texts, instruction, max_chars)
    if not groups:
        return ""
    _drop_old(ctx)
    used: list[Path] = []
    refused = 0

    async def part(g: list[tuple[int, str]]) -> list[str]:
        nonlocal refused
        try:
            return [await _kept(ctx, task, f"{instruction}\n\n" + "\n".join(t for _, t in g), used, repo)]
        except ApiError as exc:
            if skipped is None or not blocked(exc):
                raise
            refused += 1
            if len(g) == 1 or refused > BLOCK_MAX:
                log.warning("%s: the provider refused %d text(s): %s", task, len(g), exc.message[:200])
                skipped.extend(i for i, _ in g)
                return []
            return await part(g[:len(g) // 2]) + await part(g[len(g) // 2:])

    outs: list[str] = []
    for g in groups:
        outs += await part(g)
    while len(outs) > 1 and combine:
        packed = pack(outs, combine, max_chars, sep="\n\n")
        if len(packed) >= len(outs):  # cannot shrink any more
            break
        try:
            outs = [await _kept(ctx, task, f"{combine}\n\n{g}", used, repo) for g in packed]
        except ApiError as exc:
            if skipped is None or not blocked(exc):
                raise
            break  # refused as a whole: the parts' summaries stand as they are
    for p in used:
        p.unlink(missing_ok=True)
    return "\n\n".join(outs)


def parse_score(text: str, depth: int = 0) -> dict[str, Any]:
    """{"score", "justification"} from the model's answer; a score buried in text or nested JSON is found too
    (score -1 only when there really is none)."""
    raw = text.strip()
    data: Any = None
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = None
    if isinstance(data, dict) and isinstance(data.get("score"), (int, float)) and data["score"] >= 0:
        j = data.get("justification")
        return {"score": int(data["score"]), "justification": j if isinstance(j, str) else json.dumps(j)}
    if isinstance(data, dict) and isinstance(data.get("justification"), (str, dict)) and depth < 3:
        inner = data["justification"]
        got = parse_score(inner if isinstance(inner, str) else json.dumps(inner), depth + 1)
        if got["score"] >= 0:
            return got
    sm = re.search(r'"?score"?\s*[:=]\s*(\d{1,3})', raw)
    if sm:
        jm = re.search(r'"justification"\s*:\s*"?(.*)', raw, re.S)
        just = (jm.group(1) if jm else raw).strip().rstrip('}').rstrip().rstrip('"')
        return {"score": min(100, int(sm.group(1))), "justification": just[:4000]}
    return {"score": -1, "justification": raw[:4000] or "no answer"}

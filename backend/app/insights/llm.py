"""Model requests: an OpenAI-compatible chat endpoint (default: DeepSeek) or the app's agents (OpenCode / Codex),
chosen per task. Long material is split into parts of ≤ max_chars and each part answered (the caller combines).
Tests replace the whole thing with ctx.extra["insights_llm"] = callable(task, prompt, json) -> str."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from ..errors import ApiError
from . import settings as st

log = logging.getLogger("waw.insights")


async def complete(ctx: Any, task: str, prompt: str, *, as_json: bool = False, thinking: bool = False) -> str:
    fake = ctx.extra.get("insights_llm")
    if fake is not None:
        out = fake(task, prompt, as_json)
        return await out if asyncio.iscoroutine(out) else out
    cfg = st.load(ctx)["llm"].get(task) or {"mode": "api"}
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


def pack(texts: list[str], instruction: str, max_chars: int, sep: str = "\n") -> list[str]:
    """Material in parts of ≤ max_chars (with the instruction); a single oversized text is cut, nothing is dropped."""
    room = max(10_000, max_chars - len(instruction))
    parts: list[str] = []
    cur: list[str] = []
    size = 0
    for t in texts:
        if len(t) > room:
            if cur:
                parts.append(sep.join(cur))
                cur, size = [], 0
            parts.append(t[:room] + "\n…(cut)")
            continue
        if cur and size + len(t) + len(sep) > room:
            parts.append(sep.join(cur))
            cur, size = [], 0
        cur.append(t)
        size += len(t) + len(sep)
    if cur:
        parts.append(sep.join(cur))
    return parts


async def summarize(ctx: Any, task: str, texts: list[str], instruction: str, combine: str | None = None) -> str:
    """One answer for the material: each part summarized, then (more than one part) combined."""
    max_chars = int(st.load(ctx)["max_chars"])
    parts = pack(texts, instruction, max_chars)
    if not parts:
        return ""
    outs = [await complete(ctx, task, f"{instruction}\n\n{p}") for p in parts]
    while len(outs) > 1 and combine:
        groups = pack(outs, combine, max_chars, sep="\n\n")
        if len(groups) >= len(outs):  # cannot shrink any more
            break
        outs = [await complete(ctx, task, f"{combine}\n\n{g}") for g in groups]
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

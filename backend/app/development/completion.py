"""AI inline completion for the editor: one short request to an OpenAI-compatible chat endpoint.

Configured under [development.completion] (base_url, model, api_key_env). The agent CLIs (opencode / Codex) are
conversation tools, too slow for keystroke-level suggestions, so completion uses a direct model endpoint. The key is
read from the named environment variable of the server process; it never reaches the browser or the editor.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import OrderedDict
from typing import Any

import httpx

from ..config import DevCompletion

_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
CACHE_SIZE = 256
CACHE_TTL = 120

SYSTEM = ("You are a code completion engine inside an IDE. Given the code before and after the cursor, reply with ONLY "
          "the text to insert at the cursor: no explanations, no markdown fences, no repetition of the code before or "
          "after the cursor. Prefer completing the current line or statement; at most a few lines. If nothing sensible "
          "fits, reply with an empty message.")


def enabled(cfg: DevCompletion) -> bool:
    return bool(cfg.base_url and cfg.model)


def info(cfg: DevCompletion) -> dict[str, Any]:
    key_ok = (not cfg.api_key_env) or bool(os.environ.get(cfg.api_key_env))
    return {"enabled": enabled(cfg) and key_ok, "model": cfg.model or None,
            "note": None if enabled(cfg) and key_ok else (
                f"the environment variable {cfg.api_key_env} is not set for the server" if enabled(cfg) else
                "AI inline completion is off: set [development.completion] base_url and model in the config file")}


def _clean(text: str, prefix: str, suffix: str) -> str:
    t = text.strip("\n")
    m = re.match(r"^```[\w+-]*\n(.*?)\n?```\s*$", t, re.S)
    if m:
        t = m.group(1)
    t = t.replace("<CURSOR>", "")
    # models sometimes repeat the current line's start: drop the overlap with the prefix
    last = prefix.rsplit("\n", 1)[-1]
    if last and t.startswith(last):
        t = t[len(last):]
    # ... or run into the code that follows
    nxt = suffix.lstrip("\n").split("\n", 1)[0].strip()
    if nxt and len(nxt) > 3:
        i = t.find(nxt)
        if i > 0:
            t = t[:i].rstrip()
    return t


async def complete(cfg: DevCompletion, *, language: str, path: str, prefix: str, suffix: str) -> dict[str, Any]:
    if not enabled(cfg):
        return {"text": "", "disabled": True}
    prefix, suffix = prefix[-6000:], suffix[:2000]
    key = hashlib.sha256(f"{cfg.model}\0{language}\0{path}\0{prefix}\0{suffix}".encode()).hexdigest()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return {"text": hit[1], "cached": True}
    headers = {"Content-Type": "application/json"}
    if cfg.api_key_env:
        k = os.environ.get(cfg.api_key_env)
        if not k:
            return {"text": "", "disabled": True, "note": f"{cfg.api_key_env} is not set"}
        headers["Authorization"] = f"Bearer {k}"
    body = {"model": cfg.model, "temperature": 0, "max_tokens": cfg.max_tokens, "stream": False,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"File: {path} ({language})\n\n{prefix}<CURSOR>{suffix}"}]}
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as c:
            r = await c.post(cfg.base_url.rstrip("/") + "/chat/completions", json=body, headers=headers)
    except httpx.HTTPError as exc:
        return {"text": "", "error": f"{exc.__class__.__name__}"}
    if r.status_code != 200:
        return {"text": "", "error": f"HTTP {r.status_code}"}
    try:
        msg = r.json()["choices"][0]["message"]
        text = msg.get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError):
        return {"text": "", "error": "unexpected response"}
    text = _clean(text, prefix, suffix)
    _cache[key] = (time.time(), text)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return {"text": text}

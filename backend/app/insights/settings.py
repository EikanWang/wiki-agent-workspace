"""Repo Insights settings (kv `insights_settings`, shared by every browser) and its secrets (API key, mail lists) in
<state_dir>/insights-secrets.json (mode 0600; never sent to the browser)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..errors import ApiError
from ..kv import kv_get, kv_put
from .prompts import XPU_FOCUS

KEY = "insights_settings"
TASKS = ("daily", "rollup", "commits", "report", "highlight")

DEFAULT_RFC = {"repos": [], "labels": ["RFC"], "title_patterns": ["[RFC]", "RFC:"]}
DEFAULT_REPOS: list[dict[str, Any]] = [
    {"repo": "pytorch/pytorch", "highlights": True, "time": "05:35", "rfc": {**DEFAULT_RFC, "repos": ["pytorch/rfcs"]}},
    {"repo": "sgl-project/sglang", "time": "02:35"},
    {"repo": "vllm-project/vllm", "time": "03:05"},
    {"repo": "huggingface/transformers", "time": "03:35"},
    {"repo": "flashinfer-ai/flashinfer", "time": "04:05"},
    {"repo": "NVIDIA/cutlass", "time": "04:35"},
    {"repo": "triton-lang/triton", "time": "05:05"},
]
REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def _repo_defaults(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": r["repo"],
        "sources": {"issues": True, "commits": True, "rfcs": True, **(r.get("sources") or {})},
        "highlights": bool(r.get("highlights", False)),
        "schedule": {"enabled": False, "time": r.get("time") or "05:35", **(r.get("schedule") or {})},
        "rfc": {**DEFAULT_RFC, **(r.get("rfc") or {})},
    }


def defaults(state_dir: Path) -> dict[str, Any]:
    return {
        "data_dir": str(Path(state_dir) / "insights"),
        "timezone_offset": 8,  # schedule times are in UTC+8 (like the jobs they replace)
        "repos": [_repo_defaults(r) for r in DEFAULT_REPOS],
        # how each task reaches a model: "api" (an OpenAI-compatible endpoint, default) or "agent" (OpenCode / Codex)
        "llm": {t: {"mode": "api", "provider": None, "model": None, "variant": None} for t in TASKS},
        "highlight": {"threshold": 70, "number_of_ccer": 5},
        "max_chars": 1_500_000,   # characters per model request (the text is split in parts beyond it)
        "concurrency": 4,         # model requests at the same time (scoring)
        "presets": [{"id": "xpu", "name": "XPU (Intel GPU)", "text": XPU_FOCUS}],
    }


def load(ctx: Any) -> dict[str, Any]:
    d = defaults(ctx.settings.state_dir)
    s = kv_get(ctx, KEY, {}) or {}
    out = {**d, **{k: v for k, v in s.items() if k in d}}
    out["llm"] = {t: {**d["llm"][t], **((s.get("llm") or {}).get(t) or {})} for t in TASKS}
    out["highlight"] = {**d["highlight"], **(s.get("highlight") or {})}
    out["repos"] = [_repo_defaults(r) for r in (s.get("repos") if s.get("repos") is not None else d["repos"])]
    presets = s.get("presets") if s.get("presets") is not None else d["presets"]
    out["presets"] = [dict(p) for p in presets]
    return out


def save(ctx: Any, patch: dict[str, Any]) -> dict[str, Any]:
    cur = load(ctx)
    if patch.get("data_dir"):
        p = Path(str(patch["data_dir"])).expanduser()
        if not p.is_absolute():
            raise ApiError(400, "INVALID_PATH", "give an absolute folder")
        try:
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ApiError(400, "NOT_WRITABLE", f"cannot use {p}: {exc}")
        cur["data_dir"] = str(p)
    if "repos" in patch:
        repos, seen = [], set()
        for r in patch["repos"] or []:
            name = str((r or {}).get("repo") or "").strip()
            if not REPO.match(name):
                raise ApiError(400, "INVALID_REPO", f"{name!r}: use owner/name")
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            t = str(((r.get("schedule") or {}).get("time")) or "05:35")
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ApiError(400, "INVALID_TIME", f"{name}: time as HH:MM")
            rfc = r.get("rfc") or {}
            for x in rfc.get("repos") or []:
                if not REPO.match(str(x)):
                    raise ApiError(400, "INVALID_REPO", f"RFC repository {x!r}: use owner/name")
            repos.append(_repo_defaults({**r, "repo": name, "schedule": {"enabled": bool((r.get("schedule") or {}).get("enabled")), "time": t},
                                         "rfc": {"repos": [str(x) for x in rfc.get("repos") or []][:10],
                                                 "labels": [str(x)[:60] for x in rfc.get("labels") or []][:20],
                                                 "title_patterns": [str(x)[:60] for x in rfc.get("title_patterns") or []][:20]}}))
        cur["repos"] = repos
    if "llm" in patch:
        for t, v in (patch["llm"] or {}).items():
            if t in TASKS and isinstance(v, dict):
                mode = v.get("mode", "api")
                if mode not in ("api", "agent"):
                    raise ApiError(400, "INVALID_MODE", "mode is api or agent")
                cur["llm"][t] = {"mode": mode, **{k: (str(v[k])[:300] if v.get(k) else None) for k in ("provider", "model", "variant")}}
    if "highlight" in patch:
        h = patch["highlight"] or {}
        cur["highlight"] = {"threshold": max(0, min(100, int(h.get("threshold", cur["highlight"]["threshold"])))),
                            "number_of_ccer": max(0, int(h.get("number_of_ccer", cur["highlight"]["number_of_ccer"])))}
    if "presets" in patch:
        cur["presets"] = [{"id": str(p.get("id") or f"p{i}")[:40], "name": str(p.get("name") or "")[:80], "text": str(p.get("text") or "")[:20000]}
                          for i, p in enumerate(patch["presets"] or [])][:50]
    for k in ("max_chars", "concurrency", "timezone_offset"):
        if k in patch:
            cur[k] = int(patch[k])
    kv_put(ctx, KEY, cur)
    return load(ctx)


def repo_cfg(ctx: Any, repo: str) -> dict[str, Any]:
    for r in load(ctx)["repos"]:
        if r["repo"].lower() == repo.lower():
            return r
    return _repo_defaults({"repo": repo})


def data_dir(ctx: Any) -> Path:
    p = Path(load(ctx)["data_dir"]).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------- secrets

def _secrets_path(ctx: Any) -> Path:
    return Path(ctx.settings.state_dir) / "insights-secrets.json"


def secrets(ctx: Any) -> dict[str, Any]:
    try:
        return json.loads(_secrets_path(ctx).read_text())
    except (OSError, ValueError):
        return {}


def save_secrets(ctx: Any, patch: dict[str, Any]) -> None:
    cur = secrets(ctx)
    llm = dict(cur.get("llm") or {})
    for k in ("base_url", "model", "api_key"):
        if patch.get(k) is not None:
            llm[k] = str(patch[k]).strip()
    cur["llm"] = llm
    p = _secrets_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cur, fh)
    os.replace(p.with_suffix(".tmp"), p)


def llm_public(ctx: Any) -> dict[str, Any]:
    s = secrets(ctx).get("llm") or {}
    return {"base_url": s.get("base_url") or "https://api.deepseek.com", "model": s.get("model") or "deepseek-v4-flash",
            "has_key": bool(s.get("api_key"))}

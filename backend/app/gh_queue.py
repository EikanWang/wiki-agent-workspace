"""Durable queue of long-running GitHub-dependent work (the app's kv table): what was started, with its parameters and
progress, so it resumes by itself after a restart (or after a rate-limit wait that outlived the process).

Each kind registers a resumer: `register("insights.report", fn)`; `fn(ctx, op)` restarts the work from `op["params"]`
and `op["progress"]` (the work skips what it already finished, so a resumed operation does nothing twice). An operation
is resumed at most MAX_RESUMES times (then dropped as failed), so a broken one cannot loop forever.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .kv import kv_get, kv_put

log = logging.getLogger("waw.github.queue")
KEY = "github_pending_ops"
MAX_RESUMES = 5
_resumers: dict[str, Callable[[Any, dict[str, Any]], Any]] = {}


def register(kind: str, fn: Callable[[Any, dict[str, Any]], Any]) -> None:
    _resumers[kind] = fn


def _all(ctx: Any) -> dict[str, dict[str, Any]]:
    return kv_get(ctx, KEY, {}) or {}


def add(ctx: Any, op_id: str, kind: str, params: dict[str, Any]) -> None:
    ops = _all(ctx)
    ops[op_id] = {"id": op_id, "kind": kind, "params": params, "progress": {}, "created_at": time.time(), "resumes": 0, "state": "RUNNING"}
    kv_put(ctx, KEY, ops)


def progress(ctx: Any, op_id: str, **fields: Any) -> None:
    ops = _all(ctx)
    if op_id in ops:
        ops[op_id]["progress"].update(fields)
        kv_put(ctx, KEY, ops)


def state(ctx: Any, op_id: str, value: str) -> None:
    ops = _all(ctx)
    if op_id in ops and ops[op_id].get("state") != value:
        ops[op_id]["state"] = value
        kv_put(ctx, KEY, ops)


def done(ctx: Any, op_id: str) -> None:
    ops = _all(ctx)
    if ops.pop(op_id, None) is not None:
        kv_put(ctx, KEY, ops)


def pending(ctx: Any) -> list[dict[str, Any]]:
    return sorted(_all(ctx).values(), key=lambda o: o["created_at"])


def resume_all(ctx: Any) -> list[str]:
    """At start-up: restart what was pending (call from the running event loop)."""
    started = []
    ops = _all(ctx)
    for op in sorted(ops.values(), key=lambda o: o["created_at"]):
        fn = _resumers.get(op["kind"])
        if fn is None:
            continue
        if op["resumes"] >= MAX_RESUMES:
            log.warning("dropping %s %s: resumed %d times without finishing", op["kind"], op["id"], op["resumes"])
            ops.pop(op["id"], None)
            kv_put(ctx, KEY, ops)
            continue
        op["resumes"] += 1
        op["state"] = "RESUMING"
        kv_put(ctx, KEY, ops)
        try:
            fn(ctx, op)
            started.append(op["id"])
            log.info("resumed %s %s (resume %d)", op["kind"], op["id"], op["resumes"])
        except Exception:  # noqa: BLE001 - the others still resume
            log.exception("could not resume %s %s", op["kind"], op["id"])
    return started

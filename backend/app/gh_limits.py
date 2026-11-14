"""Central GitHub API rate-limit management. Every GitHub API call of the app goes through `run_gh` (the server's
`gh` login) — REST, GraphQL and `gh pr …` — so the quota is watched, shared and respected in one place.

What it does
- Reads GitHub's own numbers: `gh api -i` gives the X-RateLimit-{Limit,Remaining,Used,Reset,Resource} and
  Retry-After headers of every response (each page of a paginated call); calls without headers (`gh pr …`, logs)
  are counted as estimates and checked against `gh api rate_limit` (free) when the state is old.
- Keeps a bucket per (identity, resource) — core, graphql, search, … — with the limit GitHub states (never assumes
  5000), reset times converted to this host's clock with the skew taken from GitHub's Date header.
- Shares the buckets with the other processes of this user (a small locked JSON file), so several servers / jobs on
  the same gh login coordinate.
- Throttles before GitHub has to: below `warn` (20%) background requests are spread out until the reset; below
  `suspend` (5%) background requests wait for the reset; the last `reserve` (2%, at least 20) is kept for critical
  operations (your mutations); at 0 everything affected waits for the reset (a person waiting gets a clear 429 with
  the reset time instead of a hang longer than `max_interactive_wait`).
- Retries rate-limited answers (primary: until the reset + margin; secondary: Retry-After, else ≥ 60 s with bounded
  exponential backoff), with jitter, at most `max_retries` times. A rate-limited answer means GitHub did not do the
  call, so mutations are retried too; a mutation whose outcome is unknown (time-out, 5xx, network) is never repeated
  blindly: its `reconcile` callback checks GitHub first, else it fails with a clear message.
- Deduplicates identical reads in flight, records metrics (per operation, retries, waits, blocked time, cache hits)
  and the state shown to people: RUNNING, THROTTLED, WAITING_FOR_RATE_LIMIT, RESUMING, FAILED.

Priorities come from the caller's context (`with priority(BACKGROUND, wait=False): …`), so the callers stay simple;
`asyncio.to_thread` carries the context into the worker thread.
"""

from __future__ import annotations

import contextlib
import contextvars
import email.utils
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import ApiError

log = logging.getLogger("waw.github.rate")

CRITICAL, INTERACTIVE, BACKGROUND = 0, 1, 2
PRIO_NAME = {CRITICAL: "critical", INTERACTIVE: "interactive", BACKGROUND: "background"}
RUNNING, THROTTLED, WAITING, RESUMING, FAILED = "RUNNING", "THROTTLED", "WAITING_FOR_RATE_LIMIT", "RESUMING", "FAILED"

_prio: contextvars.ContextVar[tuple[int, bool]] = contextvars.ContextVar("gh_priority", default=(INTERACTIVE, True))
_op: contextvars.ContextVar[str] = contextvars.ContextVar("gh_op", default="other")
_on_wait: contextvars.ContextVar[Callable[[dict[str, Any] | None], None] | None] = contextvars.ContextVar("gh_on_wait", default=None)


@contextlib.contextmanager
def priority(level: int, wait: bool = True) -> Iterator[None]:
    """Calls made inside run at this priority; wait=False: raise GH_RATE_LIMITED instead of waiting (speculative
    work such as prefetching, which a later visit does anyway)."""
    tok = _prio.set((level, wait))
    try:
        yield
    finally:
        _prio.reset(tok)


@contextlib.contextmanager
def operation(name: str, on_wait: Callable[[dict[str, Any] | None], None] | None = None) -> Iterator[None]:
    """Name the calls inside (for the metrics); on_wait(info) is told when they wait for the quota (info None: going on)."""
    t1 = _op.set(name)
    t2 = _on_wait.set(on_wait) if on_wait is not None else None
    try:
        yield
    finally:
        _op.reset(t1)
        if t2 is not None:
            _on_wait.reset(t2)


def in_priority(level: int, wait: bool, op: str, fn: Callable[..., Any], *args: Any) -> Any:
    """fn(*args) at a priority, under an operation name (for asyncio.to_thread)."""
    with priority(level, wait), operation(op):
        return fn(*args)


@dataclass
class Bucket:
    limit: int = 0
    remaining: int = 0
    used: int = 0
    reset: float = 0.0       # epoch seconds, on this host's clock
    observed: float = 0.0    # when GitHub last told us (local epoch)
    source: str = "estimate"  # headers | rate_limit | estimate


@dataclass
class Config:
    warn: float = 0.20
    suspend: float = 0.05
    reserve: float = 0.02
    reserve_min: int = 20
    max_interactive_wait: float = 90.0
    max_background_wait: float = 3900.0
    max_retries: int = 4
    secondary_min: float = 60.0
    backoff_cap: float = 900.0
    margin: float = 3.0
    jitter: float = 0.2
    mutation_gap: float = 1.0
    refresh_after: float = 120.0   # re-read `gh api rate_limit` when nothing newer is known
    max_gap: float = 30.0          # longest spacing of throttled background requests


class RateLimited(ApiError):
    pass


@dataclass
class GhResult:
    rc: int
    out: str
    err: str
    status: int | None = None
    headers: list[dict[str, str]] = field(default_factory=list)  # one per response (page)

    @property
    def last(self) -> dict[str, str]:
        return self.headers[-1] if self.headers else {}


_STATUS = re.compile(r"^HTTP/\d(?:\.\d)? (\d{3})")


def split_included(out: str) -> tuple[list[tuple[int, dict[str, str]]], str]:
    """`gh api -i` output: [(status, headers)] of each response and the body text (pages joined)."""
    lines = out.split("\n")
    heads: list[tuple[int, dict[str, str]]] = []
    body: list[str] = []
    i = 0
    while i < len(lines):
        m = _STATUS.match(lines[i])
        if not m:
            body.append(lines[i])
            i += 1
            continue
        h: dict[str, str] = {}
        i += 1
        while i < len(lines) and lines[i].strip():
            k, _, v = lines[i].partition(":")
            h[k.strip().lower()] = v.strip()
            i += 1
        i += 1  # the blank line
        heads.append((int(m[1]), h))
    return heads, "\n".join(body).strip("\n") + ("\n" if body and body[-1:] != [""] else "")


def error_text(res: "GhResult") -> str:
    """What a failed answer says: gh's stderr and GitHub's error message / GraphQL error types — never the data
    (a PR titled "rate limit" is not a rate limit)."""
    parts = [res.err or ""]
    body = (res.out or "").strip() if isinstance(res.out, str) else ""
    if body.startswith("{"):
        try:
            j = json.loads(body)
        except ValueError:
            j = None
        if isinstance(j, dict):
            parts.append(str(j.get("message") or ""))
            parts += [f"{e.get('type') or ''} {e.get('message') or ''}" for e in j.get("errors") or [] if isinstance(e, dict)]
    elif len(body) < 500:
        parts.append(body)
    return "\n".join(parts)


def classify(status: int | None, text: str, headers: dict[str, str]) -> str:
    """primary | secondary | auth | transient | other — what a failed answer means (text: error_text)."""
    low = text.lower()
    if "secondary rate limit" in low or "abuse detection" in low or (status in (403, 429) and "retry-after" in headers and "remaining" not in low):
        return "secondary"
    if "rate limit" in low or "rate_limited" in low or (status in (403, 429) and headers.get("x-ratelimit-remaining") == "0"):
        return "primary"
    if status == 429:
        return "secondary"
    if status == 401 or "bad credentials" in low or "gh auth login" in low or "not logged" in low or "http 401" in low:
        return "auth"
    if (status and status >= 500) or any(s in low for s in ("http 502", "http 503", "http 504", "invalid character '<'", "timed out", "timeout",
                                                           "could not resolve host", "connection reset", "connection refused", "unexpected eof")):
        return "transient"
    return "other"


class Limiter:
    def __init__(self, config: Config | None = None, shared: Path | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = config or Config()
        self.shared = shared
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.RLock()
        self.buckets: dict[str, Bucket] = {}            # "identity|resource"
        self.blocked: dict[str, tuple[float, str]] = {}  # secondary limits: key -> (until, why)
        self.skew = 0.0                                  # GitHub's clock - ours
        self.identity = "gh"
        self.last_bg: dict[str, float] = {}
        self.last_mutation = 0.0
        self.checked = 0.0
        self.waiting: dict[int, dict[str, Any]] = {}     # thread id -> what it waits for
        self.failed: dict[str, Any] | None = None
        self.resuming_until = 0.0
        self.metrics: dict[str, Any] = {"requests": 0, "by_op": {}, "by_resource": {}, "throttled": 0, "waited": 0, "retries": 0,
                                        "blocked_seconds": 0.0, "rate_limited": 0, "errors": {}, "deduped": 0, "cache_hits": 0, "cache_misses": 0}
        self.inflight: dict[str, tuple[threading.Event, list[Any]]] = {}
        self.refresher: Callable[[], dict[str, Any] | None] | None = None

    # ------------------------------------------------------------------ state
    def key(self, resource: str) -> str:
        return f"{self.identity}|{resource}"

    def set_identity(self, login: str) -> None:
        if login:
            self.identity = login

    def observe(self, headers: dict[str, str], resource: str | None = None) -> str | None:
        """GitHub's numbers from one response (authoritative). Returns the resource they were for."""
        h = {k.lower(): v for k, v in headers.items()}
        now = self.clock()
        if h.get("date"):
            with contextlib.suppress(TypeError, ValueError):
                gh_now = email.utils.parsedate_to_datetime(h["date"]).timestamp()
                if abs(gh_now - now) < 86400:
                    self.skew = gh_now - now
        res = h.get("x-ratelimit-resource") or resource
        if not res or "x-ratelimit-remaining" not in h:
            return res
        try:
            b = Bucket(limit=int(h.get("x-ratelimit-limit", 0)), remaining=int(h["x-ratelimit-remaining"]), used=int(h.get("x-ratelimit-used", 0)),
                       reset=float(h.get("x-ratelimit-reset", 0)) - self.skew, observed=now, source="headers")
        except ValueError:
            return res
        with self.lock:
            old = self.buckets.get(self.key(res))
            # answers of one window can arrive out of order: keep the lower count within a window
            if old and old.source == "headers" and abs(old.reset - b.reset) < 5 and old.remaining < b.remaining and now - old.observed < 5:
                b.remaining = old.remaining
            self.buckets[self.key(res)] = b
            self._save()
        return res

    def observe_rate_limit(self, data: dict[str, Any]) -> None:
        """`gh api rate_limit` output."""
        now = self.clock()
        with self.lock:
            for res, v in (data.get("resources") or {}).items():
                if isinstance(v, dict) and "remaining" in v:
                    old = self.buckets.get(self.key(res))
                    if old and old.source == "headers" and now - old.observed < 5:
                        continue
                    self.buckets[self.key(res)] = Bucket(int(v.get("limit", 0)), int(v["remaining"]), int(v.get("used", 0)),
                                                         float(v.get("reset", 0)) - self.skew, now, "rate_limit")
            self.checked = now
            self._save()

    def _load_shared(self) -> None:
        if not self.shared:
            return
        try:
            data = json.loads(self.shared.read_text())
        except (OSError, ValueError):
            return
        for k, v in (data.get("buckets") or {}).items():
            with contextlib.suppress(TypeError):
                b = Bucket(**v)
                cur = self.buckets.get(k)
                if cur is None or b.observed > cur.observed or (abs(b.reset - cur.reset) < 5 and b.remaining < cur.remaining):
                    self.buckets[k] = b
        for k, v in (data.get("blocked") or {}).items():
            if v[0] > self.blocked.get(k, (0, ""))[0]:
                self.blocked[k] = (float(v[0]), str(v[1]))

    def _save(self) -> None:
        if not self.shared:
            return
        try:
            import fcntl

            self.shared.parent.mkdir(parents=True, exist_ok=True)
            with open(self.shared.with_suffix(".lock"), "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                self._load_shared()
                now = self.clock()
                data = {"buckets": {k: asdict(b) for k, b in self.buckets.items()},
                        "blocked": {k: list(v) for k, v in self.blocked.items() if v[0] > now}}
                tmp = self.shared.with_name(f".{self.shared.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data))
                os.replace(tmp, self.shared)
        except OSError as exc:  # sharing is best effort; this process still limits itself
            log.debug("rate state not shared: %s", exc)

    # ------------------------------------------------------------------ decisions
    def _bucket(self, resource: str) -> Bucket | None:
        b = self.buckets.get(self.key(resource))
        if b and b.reset and self.clock() >= b.reset + 1:  # a new window: full until GitHub says otherwise
            b = Bucket(b.limit, b.limit, 0, b.reset + 3600, b.observed, "estimate")
            self.buckets[self.key(resource)] = b
        return b

    def decide(self, resource: str, level: int, mutation: bool) -> tuple[float, str, str]:
        """(seconds to wait, state, why) for one request now."""
        c, now = self.cfg, self.clock()
        until, why = self.blocked.get(self.key(resource), (0.0, ""))
        if until > now:
            return until - now, WAITING, why or f"GitHub's secondary rate limit ({resource})"
        b = self._bucket(resource)
        wait, state, reason = 0.0, RUNNING, ""
        if b and b.limit > 0:
            to_reset = max(0.0, b.reset - now) + c.margin
            reserve = max(c.reserve_min, b.limit * c.reserve)
            frac = b.remaining / b.limit
            if b.remaining <= 0:
                wait, state, reason = to_reset, WAITING, f"the {resource} quota is used up ({b.limit}/h)"
            elif level == BACKGROUND and frac < c.suspend:
                wait, state, reason = to_reset, WAITING, f"background work paused: {b.remaining} of {b.limit} {resource} requests left"
            elif level == INTERACTIVE and b.remaining <= reserve:
                wait, state, reason = to_reset, WAITING, f"the last {int(reserve)} {resource} requests are kept for your own actions"
            elif level == BACKGROUND and frac < c.warn:
                spare = max(1.0, b.remaining - b.limit * c.suspend)
                gap = min(c.max_gap, max(0.5, (to_reset - c.margin) / spare))
                wait = max(0.0, self.last_bg.get(resource, 0) + gap - now)
                state, reason = (THROTTLED, f"{b.remaining} of {b.limit} {resource} requests left: background requests spread out") if wait else (THROTTLED, "")
        if mutation:
            wait = max(wait, self.last_mutation + c.mutation_gap - now)
        return wait, state, reason

    def max_wait(self, level: int, may_wait: bool) -> float:
        if not may_wait:
            return 0.0
        return self.cfg.max_background_wait if level == BACKGROUND else self.cfg.max_interactive_wait

    def acquire(self, resource: str, mutation: bool = False, cost: int = 1) -> None:
        """Wait until a request to `resource` is allowed for the caller's priority (or raise GH_RATE_LIMITED)."""
        level, may_wait = _prio.get()
        if mutation:
            level = CRITICAL
        waited = 0.0
        told = False
        while True:
            with self.lock:
                self._load_shared()
                stale = self.clock() - max(self.checked, (self._bucket(resource) or Bucket()).observed) > self.cfg.refresh_after
            if stale and self.refresher is not None:
                self.checked = self.clock()
                with contextlib.suppress(Exception):
                    data = self.refresher()
                    if data:
                        self.observe_rate_limit(data)
            with self.lock:
                wait, state, why = self.decide(resource, level, mutation)
                if wait <= 0.05:
                    b = self._bucket(resource)
                    if b:
                        b.remaining -= cost
                        if b.source != "headers":
                            b.source = "estimate"
                    if level == BACKGROUND:
                        self.last_bg[resource] = self.clock()
                    if mutation:
                        self.last_mutation = self.clock()
                    if waited:
                        self.metrics["blocked_seconds"] += waited
                        self.resuming_until = self.clock() + 5
                    self.waiting.pop(threading.get_ident(), None)
                    break
                if waited + wait > self.max_wait(level, may_wait) + 0.5:
                    self.waiting.pop(threading.get_ident(), None)
                    self.metrics["rate_limited"] += 1
                    at = self.clock() + wait
                    raise RateLimited(429, "GH_RATE_LIMITED", f"GitHub API limit: {why}; it can go on at {_hhmm(at)} (UTC+8)",
                                      {"resource": resource, "retry_at": at, "retry_after": round(wait), "state": state})
                info = {"op": _op.get(), "resource": resource, "priority": PRIO_NAME[level], "state": state, "why": why,
                        "until": self.clock() + wait, "since": self.clock()}
                self.waiting[threading.get_ident()] = info
                self.metrics["throttled" if state == THROTTLED else "waited"] += 1
            if state == WAITING:
                log.warning("github %s: %s; waiting %.0f s (%s, %s)", resource, why, wait, info["op"], info["priority"])
            cb = _on_wait.get()
            if cb is not None and state == WAITING:
                told = True
                with contextlib.suppress(Exception):
                    cb(info)
            step = min(wait, 30.0)
            self.sleep(step)
            waited += step
        if told:
            cb = _on_wait.get()
            if cb is not None:
                with contextlib.suppress(Exception):
                    cb(None)

    def block(self, resource: str, seconds: float, why: str) -> None:
        with self.lock:
            self.blocked[self.key(resource)] = (self.clock() + seconds, why)
            self._save()

    def backoff(self, kind: str, attempt: int, headers: dict[str, str], resource: str) -> tuple[float, bool]:
        """(seconds, known): known when GitHub said how long (Retry-After, a reset time)."""
        c = self.cfg
        ra = headers.get("retry-after")
        known = bool(ra)
        if ra:
            try:
                d = float(ra)
            except ValueError:
                try:
                    d = email.utils.parsedate_to_datetime(ra).timestamp() - self.skew - self.clock()
                except (TypeError, ValueError):
                    d = c.secondary_min
        elif kind == "primary":
            b = self._bucket(resource)
            reset = float(headers["x-ratelimit-reset"]) - self.skew if headers.get("x-ratelimit-reset") else (b.reset if b else 0)
            known = (reset > self.clock() and b is not None and b.source == "headers") or bool(headers.get("x-ratelimit-reset"))
            d = reset - self.clock() + c.margin if reset > self.clock() else c.secondary_min * (2 ** attempt)
        elif kind == "secondary":
            d = c.secondary_min * (2 ** attempt)
        else:  # transient
            d = 1.0 * (2 ** attempt)
        d = min(max(d, 1.0), c.backoff_cap if kind != "primary" else 3700.0)
        return d * (1 + random.uniform(0, c.jitter)), known

    # ------------------------------------------------------------------ the call
    def call(self, fn: Callable[[], GhResult], resource: str, *, mutation: bool = False, idempotent: bool | None = None,
             reconcile: Callable[[], Any] | None = None, dedupe: str | None = None, endpoint: str = "") -> GhResult:
        idempotent = (not mutation) if idempotent is None else idempotent
        if dedupe and idempotent:
            with self.lock:
                hit = self.inflight.get(dedupe)
                if hit is None:
                    self.inflight[dedupe] = (threading.Event(), [])
            if hit is not None:
                self.metrics["deduped"] += 1
                hit[0].wait(timeout=600)
                if hit[1] and isinstance(hit[1][0], GhResult):
                    return hit[1][0]
                if hit[1]:
                    raise hit[1][0]
        try:
            res = self._call(fn, resource, mutation, idempotent, reconcile, endpoint)
            if dedupe and idempotent:
                self.inflight[dedupe][1].append(res)
            return res
        except BaseException as exc:
            if dedupe and idempotent and dedupe in self.inflight:
                self.inflight[dedupe][1].append(exc)
            raise
        finally:
            if dedupe and idempotent:
                with self.lock:
                    ev = self.inflight.pop(dedupe, None)
                if ev:
                    ev[0].set()

    def _call(self, fn, resource, mutation, idempotent, reconcile, endpoint) -> GhResult:
        op = _op.get()
        if op == "other" and endpoint:
            op = endpoint
        level = CRITICAL if mutation else _prio.get()[0]
        for attempt in range(self.cfg.max_retries + 1):
            self.acquire(resource, mutation=mutation)
            t0 = self.clock()
            res = fn()
            dt = self.clock() - t0
            seen = None
            for h in res.headers:
                seen = self.observe(h, resource) or seen
            res_name = seen or resource
            m = self.metrics
            m["requests"] += max(1, len(res.headers))
            m["by_op"][op] = m["by_op"].get(op, 0) + max(1, len(res.headers))
            m["by_resource"][res_name] = m["by_resource"].get(res_name, 0) + max(1, len(res.headers))
            b = self.buckets.get(self.key(res_name))
            log.info("github call op=%s endpoint=%s identity=%s resource=%s priority=%s status=%s rc=%s latency_ms=%d remaining=%s reset=%s",
                     op, endpoint[:120], self.identity, res_name, PRIO_NAME[level], res.status, res.rc, dt * 1000,
                     b.remaining if b else "?", _hhmm(b.reset) if b else "?")
            if res.rc == 0:
                if self.failed and self.failed.get("resource") == res_name:
                    self.failed = None
                return res
            if _graphql_partial(res):  # data with some errors (e.g. SSO-hidden nodes): an answer, for the caller
                return res
            kind = classify(res.status, error_text(res), res.last)
            m["errors"][kind] = m["errors"].get(kind, 0) + 1
            if kind == "auth":
                self.failed = {"resource": res_name, "why": "the server's gh login is not valid (run `gh auth login`)", "at": self.clock()}
                return res
            if kind in ("primary", "secondary"):
                # GitHub rejected the call: nothing was done, so even a mutation may be sent again
                delay, known = self.backoff(kind, attempt, res.last, res_name)
                if kind == "secondary":
                    self.block(res_name, delay, "GitHub's secondary rate limit (too many requests at once)")
                elif "x-ratelimit-remaining" not in res.last:
                    # "rate limit" without GitHub's numbers: read them (free) before the next request instead of guessing
                    with self.lock:
                        self.checked = 0.0
                        bk = self.buckets.get(self.key(res_name))
                        if bk:
                            bk.observed = 0.0
                if attempt >= self.cfg.max_retries:
                    return res
                lvl, may_wait = _prio.get()
                if mutation:
                    lvl = CRITICAL
                # a person waiting: only for a delay GitHub stated and that is short; else a clear answer now
                if delay > self.max_wait(lvl, may_wait) or (lvl != BACKGROUND and not known):
                    return res
                m["retries"] += 1
                log.warning("github %s %s rate limit (attempt %d): retrying when allowed (~%.0f s)", res_name, kind, attempt + 1, delay)
                if kind == "primary" and not known:
                    self.sleep(delay)  # (background) the backoff itself: acquire() knows no reset to wait for
                    self.metrics["blocked_seconds"] += delay
                continue  # acquire() waits (or raises for callers that cannot wait)
            if kind == "transient":
                if not idempotent:
                    if reconcile is not None:
                        done = reconcile()
                        if done is not None:
                            log.info("github %s: the outcome was unclear; GitHub shows it was done", op)
                            return GhResult(0, done if isinstance(done, str) else json.dumps(done), "", 200)
                    raise ApiError(502, "GH_UNCERTAIN", f"GitHub did not answer clearly whether '{op}' was done; check on GitHub before trying again",
                                   {"detail": (res.err or res.out)[-300:]})
                if attempt >= min(1 if level != BACKGROUND else 3, self.cfg.max_retries):
                    return res
                m["retries"] += 1
                self.sleep(self.backoff("transient", attempt, res.last, res_name)[0])
                continue
            return res
        return res

    # ------------------------------------------------------------------ status
    def state(self) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            waits = list(self.waiting.values())
            buckets = {}
            for k, b in self.buckets.items():
                ident, _, res = k.partition("|")
                if ident != self.identity:
                    continue
                b2 = self._bucket(res) or b
                buckets[res] = {"limit": b2.limit, "remaining": max(0, b2.remaining), "used": b2.used, "reset": b2.reset,
                                "reset_at": _iso(b2.reset), "source": b2.source, "observed_ago": round(now - b2.observed) if b2.observed else None}
            blocked = {k.partition("|")[2]: {"until": v[0], "until_at": _iso(v[0]), "why": v[1]} for k, v in self.blocked.items() if v[0] > now}
        empty = [v["reset"] for r, v in buckets.items() if r in ("core", "graphql", "search") and v["limit"] and v["remaining"] <= 0 and v["reset"] > now]
        if self.failed:
            overall = FAILED
        elif any(w["state"] == WAITING for w in waits) or blocked or empty:
            overall = WAITING
        elif waits or any(v["limit"] and v["remaining"] / v["limit"] < self.cfg.warn for r, v in buckets.items() if r in ("core", "graphql", "search")):
            overall = THROTTLED
        elif now < self.resuming_until:
            overall = RESUMING
        else:
            overall = RUNNING
        nxt = min([w["until"] for w in waits] + [v["until"] for v in blocked.values()] + empty, default=None)
        return {"state": overall, "identity": self.identity, "skew_seconds": round(self.skew, 1), "buckets": buckets, "blocked": blocked,
                "waiting": [{**w, "until_at": _iso(w["until"])} for w in waits], "next_retry_at": _iso(nxt) if nxt else None,
                "failed": self.failed, "metrics": self.metrics_view(),
                "config": {k: v for k, v in asdict(self.cfg).items() if k in ("warn", "suspend", "reserve", "max_interactive_wait")}}

    def metrics_view(self) -> dict[str, Any]:
        m = self.metrics
        hits, miss = m["cache_hits"], m["cache_misses"]
        top = sorted(m["by_op"].items(), key=lambda kv: kv[1], reverse=True)[:10]
        return {**{k: v for k, v in m.items() if k != "by_op"}, "blocked_seconds": round(m["blocked_seconds"], 1),
                "cache_hit_rate": round(hits / (hits + miss), 3) if hits + miss else None, "top_consumers": top}

    def cache(self, hit: bool) -> None:
        self.metrics["cache_hits" if hit else "cache_misses"] += 1

    # the old RateGuard interface (callers outside this module)
    def status(self) -> dict[str, Any]:
        return {r: {"remaining": max(0, b["remaining"]), "reset": int(b["reset"]), "limit": b["limit"]} for r, b in self.state()["buckets"].items()
                if r in ("core", "graphql", "search")}

    def before(self, resource: str, mutation: bool = False) -> None:
        self.acquire(resource, mutation=mutation)


def _graphql_partial(res: GhResult) -> bool:
    body = res.out.strip() if isinstance(res.out, str) else ""
    if not body.startswith("{") or '"data"' not in body:
        return False
    try:
        j = json.loads(body)
    except ValueError:
        return False
    errs = j.get("errors") or []
    return bool(j.get("data")) and not any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in errs)


def _iso(t: float | None) -> str | None:
    if not t:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.gmtime(t + 8 * 3600)) if t else "?"


LIMITER = Limiter()


def configure(state_dir: Path | None, overrides: dict[str, float] | None = None) -> None:
    """Share the state with the other processes of this data folder; [github.rate] settings."""
    base = Config()
    for k, v in (overrides or {}).items():
        if hasattr(base, k):
            setattr(base, k, type(getattr(base, k))(v))
    LIMITER.cfg = base
    fresh = Limiter(LIMITER.cfg)  # (a new app: start clean, then take what the other processes know)
    for k in ("buckets", "blocked", "metrics", "inflight", "waiting", "last_bg"):
        setattr(LIMITER, k, getattr(fresh, k))
    LIMITER.failed, LIMITER.checked, LIMITER.skew, LIMITER.last_mutation = None, 0.0, 0.0, 0.0
    LIMITER.shared = Path(state_dir) / "github-rate.json" if state_dir else None
    LIMITER.refresher = _rate_limit_free
    with LIMITER.lock:
        LIMITER._load_shared()


def _rate_limit_free() -> dict[str, Any] | None:
    """`gh api rate_limit` (does not count against the quota)."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        p = subprocess.run([gh, "api", "-i", "rate_limit"], capture_output=True, text=True, timeout=20, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    heads, body = split_included(p.stdout)
    for _, h in heads:
        LIMITER.observe({k: v for k, v in h.items() if k == "date"})
    try:
        return json.loads(body) if p.returncode == 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- running gh

def resource_of(args: list[str]) -> str:
    """The bucket a gh command spends: `gh api graphql` and `gh pr/issue/repo …` (GraphQL) → graphql;
    `gh api search/…` → search; other `gh api …` → core."""
    if args and args[0] == "api":
        rest = [a for a in args[1:] if not a.startswith("-")]
        path = next((a for a in rest if "/" in a or a in ("graphql", "user", "rate_limit")), "")
        if path == "graphql":
            return "graphql"
        if path.lstrip("/").startswith("search/"):
            return "search"
        return "core"
    return "graphql"


_VALUED = {"-f", "-F", "--field", "--raw-field", "--jq", "-q", "-X", "--method", "-H", "--header", "--input", "-t", "--template", "--json", "--repo",
           "-R", "--title", "--body", "--head", "--base", "--comment", "--state", "--limit", "--author"}


def _endpoint(args: list[str]) -> str:
    """A label for the metrics and the log: `api repos/{o}/{r}/pulls/N/files`, `graphql PrDetail`, `pr view`."""
    if args and args[0] == "api":
        rest, skip = [], False
        for a in args[1:]:
            if skip:
                skip = False
                if a.startswith("query="):
                    m = re.search(r"\b(query|mutation)\s+(\w+)", a) or re.search(r"\{\s*(\w+)", a)
                    rest.append(f"graphql {m.group(m.lastindex)}" if m else "graphql")
                continue
            if a in _VALUED:
                skip = True
            elif not a.startswith("-"):
                rest.append(a)
        label = next((x for x in rest if x.startswith("graphql ")), None) or (rest[0] if rest else "?")
        if label == "graphql":
            label = next((x for x in rest if x.startswith("graphql ")), "graphql")
        return label if label.startswith("graphql") else "api " + re.sub(r"/\d+", "/N", label.split("?")[0])
    return " ".join(args[:2])


def run_gh(args: list[str], *, timeout: float = 60, input: str | bytes | None = None, mutation: bool = False, idempotent: bool | None = None,
           reconcile: Callable[[], Any] | None = None, cwd: str | None = None, binary: bool = False, headers: bool = True,
           dedupe: bool = False) -> GhResult:
    """Run `gh <args>` under the limiter. `gh api` calls get -i (GitHub's rate headers; the body is returned
    without them). Raises GH_UNAVAILABLE / GH_TIMEOUT / GH_RATE_LIMITED; other failures come back as rc != 0."""
    gh = shutil.which("gh")
    if not gh:
        raise ApiError(503, "GH_UNAVAILABLE", "the GitHub CLI (gh) is not installed on the server")
    argv = list(args)
    with_i = headers and not binary and argv[:1] == ["api"]
    if with_i:
        argv.insert(1, "-i")

    def once() -> GhResult:
        try:
            p = subprocess.run([gh, *argv], input=input, capture_output=True, text=not binary, timeout=timeout, check=False, cwd=cwd,
                               stdin=None if input is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return GhResult(124, "", f"timed out after {timeout:.0f} s")
        if binary:
            return GhResult(p.returncode, p.stdout, p.stderr.decode("utf-8", "replace"))
        if not with_i:
            return GhResult(p.returncode, p.stdout, p.stderr, 200 if p.returncode == 0 else None)
        heads, body = split_included(p.stdout)
        st = heads[-1][0] if heads else None
        m = re.search(r"HTTP (\d{3})", p.stderr or "")
        if m and not st:
            st = int(m[1])
        return GhResult(p.returncode, body, p.stderr, st, [h for _, h in heads])

    res = LIMITER.call(once, resource_of(args), mutation=mutation, idempotent=idempotent, reconcile=reconcile,
                       dedupe=" ".join(map(str, argv)) if dedupe else None, endpoint=_endpoint(args))
    if res.rc == 124 and not res.out:
        raise ApiError(504, "GH_TIMEOUT", "GitHub did not answer in time")
    return res


def raise_for(res: GhResult, what: str = "gh") -> None:
    """The app's usual errors for a failed gh call."""
    if res.rc == 0:
        return
    err = (res.err or res.out).strip()
    low = err.lower()
    kind = classify(res.status, error_text(res), res.last)
    if kind in ("primary", "secondary"):
        h = res.last
        reset = None
        if h.get("retry-after", "").isdigit():
            reset = LIMITER.clock() + int(h["retry-after"])
        elif h.get("x-ratelimit-reset", "").isdigit():
            reset = int(h["x-ratelimit-reset"]) - LIMITER.skew
        else:
            b = LIMITER.state()["buckets"].get(h.get("x-ratelimit-resource") or ("graphql" if "graphql" in low else "core")) or {}
            reset = b.get("reset") or None
        raise RateLimited(429, "GH_RATE_LIMITED", "GitHub's rate limit is reached"
                          + (f"; it resets at {_hhmm(reset)} (UTC+8)" if reset else "; try again in a minute"),
                          {"detail": err[-300:], "retry_at": reset})
    if "http 502" in low or "http 504" in low or "invalid character '<'" in low:
        raise ApiError(502, "GH_BUSY", "GitHub took too long to answer (HTTP 502)", {"detail": err[-300:]})
    if kind == "auth":
        raise ApiError(503, "GH_AUTH", "the server's gh CLI is not logged in to GitHub (run `gh auth login` in a terminal)", {"detail": err[-300:]})
    if "could not resolve host" in low or "timeout" in low or "connection" in low:
        raise ApiError(503, "GH_UNREACHABLE", "the server cannot reach GitHub (network or proxy)", {"detail": err[-300:]})
    raise ApiError(502, "GH_ERROR", f"{what} failed: {err[-300:]}")


async def run_gh_async(args: list[str], **kw: Any) -> GhResult:
    import asyncio

    return await asyncio.to_thread(run_gh, args, **kw)


async def run_with(runner: Callable[[], Any], args: list[str], *, mutation: bool = False,
                   reconcile: Callable[[], Any] | None = None) -> tuple[int, str, str]:
    """A gh command run by someone else's runner (e.g. a PR Development target: `await runner()` → (rc, out, err))
    under the limiter: waits for the quota, retries rate-limited answers, and for a mutation whose outcome is unclear
    asks `await reconcile()` (→ (rc, out, err) of what GitHub shows, or None) instead of running it again."""
    import asyncio

    resource = resource_of(args)
    lim = LIMITER
    op = _op.get()
    if op == "other":
        op = _endpoint(args)
    for attempt in range(lim.cfg.max_retries + 1):
        await asyncio.to_thread(contextvars.copy_context().run, lim.acquire, resource, mutation)
        t0 = time.time()
        rc, out, err = await runner()
        lim.metrics["requests"] += 1
        lim.metrics["by_op"][op] = lim.metrics["by_op"].get(op, 0) + 1
        lim.metrics["by_resource"][resource] = lim.metrics["by_resource"].get(resource, 0) + 1
        log.info("github call op=%s endpoint=%s identity=%s resource=%s rc=%s latency_ms=%d", op, _endpoint(args), lim.identity, resource, rc,
                 (time.time() - t0) * 1000)
        if rc == 0:
            return rc, out, err
        kind = classify(None, err or (out if len(out) < 500 else ""), {})
        lim.metrics["errors"][kind] = lim.metrics["errors"].get(kind, 0) + 1
        if kind in ("primary", "secondary") and attempt < lim.cfg.max_retries:
            delay, _known = lim.backoff(kind, attempt, {}, resource)
            if kind == "secondary":
                lim.block(resource, delay, "GitHub's secondary rate limit (too many requests at once)")
            level, may_wait = _prio.get()
            if delay > lim.max_wait(CRITICAL if mutation else level, may_wait) or level != BACKGROUND:
                b = lim.state()["buckets"].get(resource) or {}
                raise RateLimited(429, "GH_RATE_LIMITED", "GitHub's rate limit is reached"
                                  + (f"; it resets at {_hhmm(b['reset'])} (UTC+8)" if b.get("reset") else "; try again in a minute"),
                                  {"detail": err[-300:]})
            lim.metrics["retries"] += 1
            await asyncio.sleep(delay)
            continue
        if kind == "transient" and mutation and reconcile is not None:
            done = await reconcile()
            if done is not None:
                log.info("github %s: the outcome was unclear; GitHub shows it was done", op)
                return done
            raise ApiError(502, "GH_UNCERTAIN", "GitHub did not answer clearly whether it was done; check on GitHub before trying again",
                           {"detail": (err or out)[-300:]})
        if kind == "transient" and not mutation and attempt < 1:
            lim.metrics["retries"] += 1
            await asyncio.sleep(1.0 + random.random())
            continue
        return rc, out, err
    return rc, out, err

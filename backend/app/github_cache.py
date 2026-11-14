"""A local cache of PR Review data, so a pull request's tab opens at once.

What GitHub returned for a PR — details + files + viewed marks + checks ("pr"), review threads + conversation
("comments"), contributors ("people") — is kept in a folder (default <state_dir>/github-cache; ⚙ → Cache), one folder
per PR, written atomically, mode 0700 / 0600 (private repositories end up here). Files at a commit are kept too
(they never change).

Only a tab's first load reads it (the endpoints' `cached=true`); right after such a load the same full GitHub fetch
as before runs in the background, and when the result differs the cache is updated and a `github_cache` event (with
an opaque token, never a repository name — events reach every signed-in browser) tells the tab to reload normally.
Every other request (↻, reloads after your actions, polling, actions) goes to GitHub exactly as before; actions that
change a PR mark its cached copy stale. Cached data is never put into the in-memory caches that actions rely on.

Prefetch: after each refresh of the review list, PRs that are new or changed since their cached copy are fetched in
the background (2 at a time, only while ≥ PREFETCH_RESERVE GitHub requests remain; errors are dropped).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .errors import ApiError
from . import gh_limits
from .kv import kv_get, kv_put

log = logging.getLogger("waw.ghcache")

SETTINGS_KEY = "github_cache_settings"
DEFAULTS: dict[str, Any] = {"dir": None, "cap_mb": 2048, "prefetch": True}
PARTS = ("pr", "comments", "people")
CLOSED_DAYS = 7
PREFETCH_CONCURRENCY = 2
PREFETCH_RESERVE = 1000

_revalidating: set[str] = set()
_prefetch_queue: list[dict[str, Any]] = []
_prefetch_workers: list[asyncio.Task] = []
_state: dict[str, Any] = {"prefetching": 0, "prefetched": 0, "last_cleanup": 0.0}
_writes_since_cleanup = 0


# ---------------------------------------------------------------------- settings and folders

def settings(ctx: Any) -> dict[str, Any]:
    s = {**DEFAULTS, **(kv_get(ctx, SETTINGS_KEY, {}) or {})}
    s["dir"] = s.get("dir") or str(Path(ctx.settings.state_dir) / "github-cache")
    return s


def root(ctx: Any) -> Path:
    p = Path(settings(ctx)["dir"]).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def _pr_dir(ctx: Any, key: str) -> Path:
    owner_repo, _, n = key.partition("#")
    owner, _, repo = owner_repo.partition("/")
    return root(ctx) / owner / repo / n


def token(ctx: Any, key: str) -> str:
    """An opaque id of a PR for events (they reach every signed-in browser)."""
    secret = (ctx.settings.server_secret or "waw").encode()
    return hmac.new(secret, key.lower().encode(), hashlib.sha256).hexdigest()[:20]


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in (path.parent, path.parent.parent, path.parent.parent.parent):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _meta(ctx: Any, key: str) -> dict[str, Any]:
    return _read(_pr_dir(ctx, key) / "meta.json") or {}


# ---------------------------------------------------------------------- reading and writing parts

def get(ctx: Any, key: str, part: str) -> tuple[Any, float] | None:
    """(data, fetched at) of a part, unless it is missing or marked stale."""
    key = key.lower()
    m = _meta(ctx, key)
    if part in (m.get("stale") or []):
        return None
    data = _read(_pr_dir(ctx, key) / f"{part}.json")
    gh_limits.LIMITER.cache(data is not None)
    if data is None:
        return None
    m.setdefault("opened", {})
    m["opened_at"] = time.time()
    try:
        _write(_pr_dir(ctx, key) / "meta.json", m)
    except OSError:
        pass
    return data, float((m.get("fetched") or {}).get(part) or 0)


def peek(ctx: Any, key: str, part: str) -> Any:
    """A part as stored (also when stale), without counting as opened."""
    return _read(_pr_dir(ctx, key.lower()) / f"{part}.json")


def has(ctx: Any, key: str, part: str) -> bool:
    key = key.lower()
    return (_pr_dir(ctx, key) / f"{part}.json").is_file() and part not in (_meta(ctx, key).get("stale") or [])


def put(ctx: Any, key: str, part: str, data: Any) -> bool:
    """Save what GitHub returned; True when it differs from the cached copy (or there was none)."""
    global _writes_since_cleanup
    key = key.lower()
    d = _pr_dir(ctx, key)
    old = _read(d / f"{part}.json")
    changed = old is None or json.dumps(old, sort_keys=True, default=str) != json.dumps(data, sort_keys=True, default=str)
    try:
        if changed:
            _write(d / f"{part}.json", data)
        m = _meta(ctx, key)
        m.setdefault("fetched", {})[part] = time.time()
        m["stale"] = [x for x in m.get("stale") or [] if x != part]
        if part == "pr":
            pr = (data or {}).get("pr") or {}
            m.update(state=pr.get("state"), updated_at=pr.get("updated_at"), head_sha=pr.get("head_sha"), ci=pr.get("ci"))
        m.setdefault("opened_at", time.time())
        _write(d / "meta.json", m)
    except OSError as exc:
        log.warning("cannot write the PR cache %s: %s", d, exc)
        return changed
    _writes_since_cleanup += 1
    if _writes_since_cleanup >= 20:
        _writes_since_cleanup = 0
        try:
            cleanup(ctx)
        except OSError:
            pass
    return changed


def mark_stale(ctx: Any, key: str, parts: tuple[str, ...] = PARTS) -> None:
    """Your action changed the PR: its cached copy is not used until it is fetched again."""
    key = key.lower()
    d = _pr_dir(ctx, key)
    if not d.is_dir():
        return
    m = _meta(ctx, key)
    m["stale"] = sorted(set(m.get("stale") or []) | set(parts))
    try:
        _write(d / "meta.json", m)
    except OSError:
        pass


def set_viewed(ctx: Any, key: str, path: str, viewed: str) -> None:
    """Mirror a viewed mark you set (as the in-memory copy does)."""
    hit = get(ctx, key, "pr")
    if not hit:
        return
    data = hit[0]
    for f in data.get("files") or []:
        if f.get("path") == path:
            f["viewed"] = viewed
    try:
        _write(_pr_dir(ctx, key.lower()) / "pr.json", data)
    except OSError:
        pass


def file_get(ctx: Any, repo: str, sha: str, path: str) -> str | None:
    h = hashlib.sha256(f"{repo.lower()}\0{sha}\0{path}".encode()).hexdigest()
    p = root(ctx) / "files" / h[:2] / h
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def file_put(ctx: Any, repo: str, sha: str, path: str, text: str) -> None:
    h = hashlib.sha256(f"{repo.lower()}\0{sha}\0{path}".encode()).hexdigest()
    p = root(ctx) / "files" / h[:2] / h
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_name(f".{p.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except OSError:
        pass


# ---------------------------------------------------------------------- size, cleanup, clear, move

def usage(ctx: Any) -> dict[str, Any]:
    r = root(ctx)
    size = 0
    for p in r.rglob("*"):
        try:
            if p.is_file():
                size += p.stat().st_size
        except OSError:
            pass
    prs = sum(1 for _ in r.glob("*/*/*/meta.json"))
    return {"bytes": size, "prs": prs}


def cleanup(ctx: Any) -> dict[str, int]:
    """Closed / merged PRs after CLOSED_DAYS; then the PRs opened longest ago until under the cap."""
    r = root(ctx)
    s = settings(ctx)
    removed = 0
    entries = []
    now = time.time()
    for meta in r.glob("*/*/*/meta.json"):
        m = _read(meta) or {}
        d = meta.parent
        last = max([float(v) for v in (m.get("fetched") or {}).values()] or [0])
        if m.get("state") in ("CLOSED", "MERGED") and now - last > CLOSED_DAYS * 86400:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
            continue
        size = sum(p.stat().st_size for p in d.iterdir() if p.is_file())
        entries.append((float(m.get("opened_at") or last), size, d))
    cap = int(s.get("cap_mb") or 0) * 1024 * 1024
    files_dir = r / "files"
    files = sorted(((p.stat().st_mtime, p.stat().st_size, p) for p in files_dir.rglob("*") if p.is_file()), key=lambda x: x[0]) if files_dir.is_dir() else []
    total = sum(e[1] for e in entries) + sum(f[1] for f in files)
    if cap:
        for _, size, p in files:  # file contents first (cheap to fetch again)
            if total <= cap:
                break
            p.unlink(missing_ok=True)
            total -= size
        for _, size, d in sorted(entries, key=lambda e: e[0]):
            if total <= cap:
                break
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            removed += 1
    _state["last_cleanup"] = now
    return {"removed": removed}


def clear(ctx: Any) -> None:
    r = root(ctx)
    for p in r.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def change_dir(ctx: Any, new: str, roots: list[Path]) -> str:
    """Use another folder; the cached PRs are moved there (the old folder is removed after the copy)."""
    dest = Path(new).expanduser()
    if not dest.is_absolute():
        raise ApiError(400, "INVALID_PATH", "give an absolute folder")
    dest = dest.resolve()
    for w in roots:
        try:
            dest.relative_to(w.resolve())
        except ValueError:
            continue
        raise ApiError(400, "INVALID_PATH", f"{dest} is inside the folder {w}; choose a folder outside your wikis")
    old = Path(settings(ctx)["dir"]).expanduser().resolve()
    if dest == old:
        return str(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = dest / ".waw-write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError as exc:
        raise ApiError(400, "NOT_WRITABLE", f"cannot write in {dest}: {exc}")
    if old.is_dir():
        try:
            dest.relative_to(old)
            raise ApiError(400, "INVALID_PATH", "the new folder cannot be inside the current one")
        except ValueError:
            pass
        for p in old.iterdir():
            target = dest / p.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
            shutil.copytree(p, target) if p.is_dir() else shutil.copy2(p, target)
        shutil.rmtree(old, ignore_errors=True)
    os.chmod(dest, 0o700)
    kv_put(ctx, SETTINGS_KEY, {**(kv_get(ctx, SETTINGS_KEY, {}) or {}), "dir": str(dest)})
    return str(dest)


# ---------------------------------------------------------------------- background work

def publish(ctx: Any, key: str, part: str, changed: bool) -> None:
    if ctx.events:
        ctx.events.publish("global", {"type": "github_cache", "token": token(ctx, key), "part": part, "changed": changed})


def revalidate(ctx: Any, key: str, part: str, fetch, on_fresh=None) -> None:
    """After a cached first load: the same fetch as before, in the background; tell the tab what came of it."""
    rk = f"{key}|{part}"
    if rk in _revalidating:
        return
    _revalidating.add(rk)

    async def run() -> None:
        try:
            # the tab already shows the cached copy: no waiting for the GitHub quota here
            data = await asyncio.to_thread(gh_limits.in_priority, gh_limits.INTERACTIVE, False, f"revalidate:{part}", fetch)
            changed = put(ctx, key, part, data)
            if on_fresh is not None:
                on_fresh(data, changed)
            publish(ctx, key, part, changed)
        except Exception as exc:  # noqa: BLE001 - the cached copy stays; the tab's own reloads go to GitHub
            log.info("revalidating %s %s failed: %s", key, part, exc)
            publish(ctx, key, part, False)
        finally:
            _revalidating.discard(rk)
    asyncio.get_running_loop().create_task(run())


def _quota_ok(rate: Any) -> bool:
    lim = (rate.status() or {}).get("graphql") or {}
    return not lim or lim.get("remaining", PREFETCH_RESERVE + 1) > PREFETCH_RESERVE


def prefetch(ctx: Any, items: list[dict[str, Any]], fetch_all, rate: Any) -> int:
    """Queue the review list's PRs that are new or changed since their cached copy."""
    s = settings(ctx)
    enabled = ctx.extra.get("gh_cache_prefetch", "gh_graphql" not in ctx.extra)  # tests: only when asked
    if not s.get("prefetch") or not enabled:
        return 0
    queued = {x["key"] for x in _prefetch_queue}
    for it in items:
        key = f"{it['repo']}#{it['number']}".lower()
        if key in queued:
            continue
        m = _meta(ctx, key)
        fresh = m.get("updated_at") == it.get("updated_at") and m.get("ci") == it.get("ci") and not m.get("stale") \
            and all(p in (m.get("fetched") or {}) for p in ("pr", "comments"))
        if not fresh:
            _prefetch_queue.append({"key": key, "repo": it["repo"], "number": it["number"]})
    _prefetch_workers[:] = [w for w in _prefetch_workers if not w.done()]
    while _prefetch_queue and len(_prefetch_workers) < PREFETCH_CONCURRENCY:
        _prefetch_workers.append(asyncio.get_running_loop().create_task(_prefetch_worker(ctx, fetch_all, rate)))
    return len(_prefetch_queue)


async def _prefetch_worker(ctx: Any, fetch_all, rate: Any) -> None:
    while _prefetch_queue:
        if not _quota_ok(rate):
            _prefetch_queue.clear()
            return
        job = _prefetch_queue.pop(0)
        _state["prefetching"] += 1
        try:
            parts = await asyncio.to_thread(gh_limits.in_priority, gh_limits.BACKGROUND, False, "prefetch", fetch_all, job["repo"], job["number"])
            for part, data in parts.items():
                put(ctx, job["key"], part, data)
            _state["prefetched"] += 1
        except gh_limits.RateLimited as exc:  # the quota is for people now: stop; the list's next refresh queues again
            log.info("prefetch paused: %s", exc.message)
            _prefetch_queue.clear()
        except Exception as exc:  # noqa: BLE001 - prefetch is best effort
            log.info("prefetching %s failed: %s", job["key"], exc)
        finally:
            _state["prefetching"] -= 1


def status() -> dict[str, Any]:
    return {"queued": len(_prefetch_queue), "prefetching": _state["prefetching"], "prefetched": _state["prefetched"]}


def reset() -> None:
    _revalidating.clear()
    _prefetch_queue.clear()
    _state.update(prefetching=0, prefetched=0)

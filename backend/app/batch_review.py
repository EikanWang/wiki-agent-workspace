"""Agent review of several pull requests at once (Reviews → select PRs → Agent review).

Each selected PR gets exactly what the PR tab's **Agent review** button starts: its copy is loaded (from the local
cache when there is one, so a batch costs little GitHub quota), the discussion document `.waw-github/<o>/<r>/<n>.md`
is opened in the repository's wiki and a whole-document `pr_review` thread is created — with the review learning
lessons, the waw-review block and the suggestions it becomes when the PR is opened. A repository without a wiki gets
the wiki the request came from (the same as choosing it in one of its PR tabs).

At most CONCURRENCY reviews run at the same time; the rest wait in order. A worker keeps its slot until the review's
job has finished (or JOB_SLOT_SECONDS passed, e.g. a job waiting for a permission answer: it goes on, the slot is
freed). The queue is in memory: a restart drops PRs not started yet (started ones are agent runs like any other).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .errors import ApiError
from .github_hub import require_owner_mutating

log = logging.getLogger("waw.batch_review")

router = APIRouter()

CONCURRENCY = 2            # agent reviews running at the same time
MAX_PRS = 50               # PRs per request
JOB_SLOT_SECONDS = 1800.0  # a review keeps its slot at most this long
POLL_SECONDS = 2.0
DEFAULT_QUESTION = ("Review this pull request: correctness, performance, tests, API and design. "
                    "Comment on the specific lines that need attention.")
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

_queue: list[str] = []                    # PR keys ("owner/repo#n", lower case) waiting, in order
_items: dict[str, dict[str, Any]] = {}    # key → {owner, repo, number, question, workspace_id, login}
_running: dict[str, str] = {}             # key → "preparing" | "reviewing"
_failed: dict[str, dict[str, Any]] = {}   # key → {error, at}; cleared when the PR is queued again
_workers: list[asyncio.Task] = []
_state: dict[str, Any] = {"started": 0, "finished": 0}


def reset() -> None:
    for w in _workers:
        try:
            w.cancel()
        except RuntimeError:  # its event loop is gone (tests)
            pass
    _workers.clear()
    _queue.clear()
    _items.clear()
    _running.clear()
    _failed.clear()
    _state.update(started=0, finished=0)


def pr_key(repo: str, number: int) -> str:
    return f"{repo}#{number}".lower()


def status() -> dict[str, Any]:
    """For the Reviews list: what waits, what runs, what failed (with why)."""
    return {"queued": list(_queue), "running": dict(_running), "failed": dict(_failed), "concurrency": CONCURRENCY, "max_prs": MAX_PRS, **_state}


class BatchPr(BaseModel):
    repo: str = Field(max_length=200)  # owner/name
    number: int = Field(ge=1)


class BatchReviewIn(BaseModel):
    prs: list[BatchPr] = Field(min_length=1, max_length=MAX_PRS)
    question: str | None = Field(None, max_length=20000)
    # wiki for repositories that have none yet; default: the wiki of this request (X-Workspace)
    workspace_id: str | None = None


class BatchCancelIn(BaseModel):
    prs: list[BatchPr] | None = None  # none: every PR still waiting


@router.post("/github/batch-review", status_code=202)
async def start_batch_review(body: BatchReviewIn, auth: tuple[Any, str] = Depends(require_owner_mutating)):
    """Queue an agent review for each PR. Returns which were queued and which were skipped (and why)."""
    from .github_hub import NAME, agent_review_states

    ctx, login = auth
    wiki = body.workspace_id or ctx.workspace_id or None
    if wiki and ctx.workspaces is not None and wiki not in ctx.workspaces.runtimes:
        raise ApiError(400, "WORKSPACE_NOT_OPEN", "choose a folder that is open in this window")
    question = (body.question or "").strip() or DEFAULT_QUESTION
    active = agent_review_states(ctx)
    queued: list[str] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for p in body.prs:
        parts = p.repo.split("/")
        if len(parts) != 2 or not NAME.match(parts[0]) or not NAME.match(parts[1]):
            raise ApiError(400, "INVALID_REPO", f"invalid repository name: {p.repo}")
        key = pr_key(p.repo, p.number)
        if key in seen:
            continue
        seen.add(key)
        if key in _running or key in _items:
            skipped.append({"pr": key, "reason": "already queued"})
            continue
        if (active.get(key) or {}).get("state") == "running":
            skipped.append({"pr": key, "reason": "the agent is already reviewing it"})
            continue
        _failed.pop(key, None)
        _items[key] = {"owner": parts[0], "repo": parts[1], "number": p.number, "question": question, "workspace_id": wiki, "login": login}
        _queue.append(key)
        queued.append(key)
    _start_workers(ctx)
    return {"queued": queued, "skipped": skipped, "status": status()}


@router.post("/github/batch-review/cancel")
async def cancel_batch_review(body: BatchCancelIn, auth: tuple[Any, str] = Depends(require_owner_mutating)):
    """Take PRs that have not started yet off the queue (running reviews go on; stop them in their discussion)."""
    keys = None if body.prs is None else {pr_key(p.repo, p.number) for p in body.prs}
    removed = [k for k in _queue if keys is None or k in keys]
    for k in removed:
        _queue.remove(k)
        _items.pop(k, None)
    return {"removed": removed, "status": status()}


def _start_workers(ctx: Any) -> None:
    _workers[:] = [w for w in _workers if not w.done()]
    while _queue and len(_workers) < CONCURRENCY:
        _workers.append(asyncio.get_running_loop().create_task(_worker(ctx)))


async def _worker(ctx: Any) -> None:
    while _queue:
        key = _queue.pop(0)
        item = _items.pop(key, None)
        if item is None:
            continue
        _running[key] = "preparing"
        _state["started"] += 1
        try:
            job_id = await start_review(ctx, item)
            _running[key] = "reviewing"
            await _wait_job(ctx, job_id)
        except Exception as exc:  # noqa: BLE001 - reported per PR in the list
            msg = str(getattr(exc, "message", None) or exc)[:300]
            log.warning("batch agent review of %s failed: %s", key, msg)
            _failed[key] = {"error": msg, "code": getattr(exc, "code", None), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        finally:
            _running.pop(key, None)
            _state["finished"] += 1


async def start_review(ctx: Any, item: dict[str, Any]) -> str:
    """Load the PR's copy, open its discussion document in the repository's wiki, start the review. → job id."""
    from .annotations import ThreadCreateIn, ThreadService
    from .github_hub import get_pull_request, repo_wiki
    from .models import GithubRepoWiki

    owner, repo, number = item["owner"], item["repo"], item["number"]
    full = f"{owner}/{repo}".lower()
    data = await get_pull_request(owner, repo, number, refresh=False, cached=True, auth=(ctx, item["login"]))
    if ctx.workspaces is None:
        raise ApiError(400, "NO_REPO_WIKI", "no wiki folder is open")
    wiki = repo_wiki(ctx, full)
    if not wiki or wiki not in ctx.workspaces.runtimes:
        wiki = item.get("workspace_id")
        if not wiki or wiki not in ctx.workspaces.runtimes:
            raise ApiError(400, "NO_REPO_WIKI", f"choose the agent wiki for {full} first (in one of its PR tabs)")
        with ctx.db.write() as s:  # the same as choosing it in the PR tab
            row = s.get(GithubRepoWiki, full)
            if row is None:
                s.add(GithubRepoWiki(repo=full, workspace_id=wiki))
            else:
                row.workspace_id = wiki
    rt = ctx.workspaces.runtimes[wiki]
    sctx = replace(ctx, documents=rt.docs, locks=rt.locks, watcher=rt.watcher, workspace_id=rt.id, workspace_root=rt.root)
    opened = rt.docs.open_document(data["doc_path"])
    r = await ThreadService(sctx).create_thread(opened["document_id"], ThreadCreateIn(
        version_sha256=opened["sha256"], question=item["question"], whole_document=True, pr_review=True))
    return r["job_id"]


async def _wait_job(ctx: Any, job_id: str) -> None:
    from .models import Job

    end = time.monotonic() + JOB_SLOT_SECONDS
    while time.monotonic() < end:
        with ctx.db.read() as s:
            job = s.get(Job, job_id)
            st = job.status if job else "failed"
            err = job.error_message if job else None
        if st in _TERMINAL:
            if st != "completed":
                raise ApiError(500, "AGENT_REVIEW_FAILED", err or f"the agent review {st}")
            return
        await asyncio.sleep(POLL_SECONDS)

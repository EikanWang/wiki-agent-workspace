"""Search in the open folders: file and folder names, and the text inside files.

* GET /search/names: files and folders under a folder whose path has every word of the query (or matches a glob such
  as `*.docx`), the ones named like it first.
* GET /search/content: a stream (one JSON object per line) of the files whose text has the query, each with its
  matching lines (and pages / slides), then a summary. Markdown, text and source files are read as they are; Word,
  PowerPoint, Visio and PDF files through their text, extracted once into a cache (`TextIndex`) and kept while the
  file stays the same. Folders reached through a symbolic link are not searched (they often lead to large corpora
  that are open as folders of their own).

The cache is filled in the background (all open folders, after the server starts) by worker processes
(`search_extract`); a search first extracts what is missing in the folder it searches, for a few seconds, and tells
how many files are still waiting.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from .deps import AppContext, require_auth
from .documents import EXCLUDED_DIRS, DocumentService
from .errors import bad_request
from .refs import SKIP
from .textmodel import document_format

log = logging.getLogger("waw.search")
router = APIRouter()

SKIP_DIRS = EXCLUDED_DIRS | SKIP
EXTRACTED = {"docx", "pptx", "visio", "pdf"}  # formats searched through their extracted text
TEXT_MAX = 10 * 1024 * 1024  # larger text files (logs, dumps) are not searched
OFFICE_MAX = 200 * 1024 * 1024
MAX_NAME_RESULTS = 300
NAME_SCAN_S = 4.0
MAX_FILES = 200  # files with matches listed by one search
MAX_HITS = 20  # matches listed per file
WAIT_EXTRACT_S = 8.0  # how long a search waits for missing extracted text
JOB_TIMEOUT_S = 180.0
GREP_PARALLEL = max(2, min(32, (os.cpu_count() or 4) // 2))
WORKERS = max(1, min(6, (os.cpu_count() or 2) // 2))
STARTUP_DELAY_S = 30.0


# ---------------------------------------------------------------------------------------------------------- walking
@dataclass
class Scan:
    text: list[tuple[str, str]] = field(default_factory=list)  # (rel, abs) Markdown / text / source files
    office: list[tuple[str, str, str, int, int]] = field(default_factory=list)  # (rel, abs, fmt, mtime_ns, size)
    links: int = 0  # linked folders not followed


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".waw")


def scan(root: Path, sub: str) -> Scan:
    out = Scan()
    base = root / sub if sub else root
    for dirpath, dirnames, filenames in os.walk(base):
        keep = []
        for d in sorted(dirnames):
            if _skip_dir(d):
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                out.links += 1
                continue
            keep.append(d)
        dirnames[:] = keep
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        for f in sorted(filenames):
            fmt = document_format(f)
            if fmt not in ("markdown", "text") and fmt not in EXTRACTED:
                continue
            ab = os.path.join(dirpath, f)
            try:
                st = os.stat(ab)
            except OSError:
                continue
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if fmt in EXTRACTED:
                if st.st_size <= OFFICE_MAX:
                    out.office.append((rel, ab, fmt, st.st_mtime_ns, st.st_size))
            elif st.st_size <= TEXT_MAX:
                out.text.append((rel, ab))
    return out


# ------------------------------------------------------------------------------------------------- extracted text
@dataclass
class _Job:
    ws: str
    rel: str
    src: str
    fmt: str
    mtime: int
    size: int
    futures: list[asyncio.Future] = field(default_factory=list)


class TextIndex:
    """Extracted text of the Word / PowerPoint / Visio / PDF files, one cache file per file version."""

    def __init__(self, state_dir: Path, workers: int = WORKERS):
        self.dir = state_dir / "search"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.dir / "index.sqlite3", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS files (ws TEXT, path TEXT, mtime_ns INTEGER, size INTEGER, key TEXT,"
                         " chars INTEGER, error TEXT, PRIMARY KEY (ws, path))")
        self._db.commit()
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, tuple[int, int, str, str | None]]] = {}
        self._queue: list[_Job] = []
        self._queued: dict[tuple[str, str], _Job] = {}
        self._workers: list[asyncio.Task] = []
        self.n_workers = workers
        self.done: dict[str, int] = {}

    # rows
    def _load(self, ws: str) -> dict[str, tuple[int, int, str, str | None]]:
        if ws not in self._rows:
            with self._lock:
                rows = self._db.execute("SELECT path, mtime_ns, size, key, error FROM files WHERE ws = ?", (ws,)).fetchall()
            self._rows[ws] = {p: (m, s, k, e) for p, m, s, k, e in rows}
        return self._rows[ws]

    def text_path(self, ws: str, key: str) -> Path:
        return self.dir / "text" / ws / key[:2] / f"{key}.txt"

    def split(self, ws: str, office: list[tuple[str, str, str, int, int]]) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, int, int]]]:
        """(rel, cached text, fmt) of the files whose text is extracted, and the files still to extract."""
        rows = self._load(ws)
        fresh, stale = [], []
        for rel, ab, fmt, mtime, size in office:
            r = rows.get(rel)
            if r and r[0] == mtime and r[1] == size:
                if not r[3]:
                    fresh.append((rel, str(self.text_path(ws, r[2])), fmt))
            else:
                stale.append((rel, ab, fmt, mtime, size))
        return fresh, stale

    def _record(self, job: _Job, key: str, chars: int, error: str | None) -> None:
        rows = self._load(job.ws)
        old = rows.get(job.rel)
        if old and old[2] != key:
            self.text_path(job.ws, old[2]).unlink(missing_ok=True)
        rows[job.rel] = (job.mtime, job.size, key, error)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?)", (job.ws, job.rel, job.mtime, job.size, key, chars, error))
            self._db.commit()

    def prune(self, ws: str, present: set[str]) -> int:
        """Forget the files of a folder that are gone (after a scan of the whole folder)."""
        rows = self._load(ws)
        gone = [p for p in rows if p not in present]
        for p in gone:
            self.text_path(ws, rows.pop(p)[2]).unlink(missing_ok=True)
        if gone:
            with self._lock:
                self._db.executemany("DELETE FROM files WHERE ws = ? AND path = ?", [(ws, p) for p in gone])
                self._db.commit()
        return len(gone)

    def pending(self, ws: str, under: str = "") -> int:
        return sum(1 for j in self._queue if j.ws == ws and (not under or j.rel.startswith(under + "/")))

    # the queue
    def schedule(self, ws: str, items: list[tuple[str, str, str, int, int]], *, front: bool = False) -> list[asyncio.Future]:
        """Extract these files (the ones searched now go first); a future per file, done when it is."""
        loop = asyncio.get_running_loop()
        futs: list[asyncio.Future] = []
        firsts: list[_Job] = []
        for rel, ab, fmt, mtime, size in items:
            job = self._queued.get((ws, rel))
            if job is None or job.mtime != mtime or job.size != size:
                job = _Job(ws, rel, ab, fmt, mtime, size)
                self._queued[(ws, rel)] = job
                if not front:
                    self._queue.append(job)
            if front:
                firsts.append(job)
            f = loop.create_future()
            job.futures.append(f)
            futs.append(f)
        if front:
            ids = {id(j) for j in firsts}
            self._queue = firsts + [j for j in self._queue if id(j) not in ids]
        self._start_workers()
        return futs

    def _start_workers(self) -> None:
        self._workers = [w for w in self._workers if not w.done()]
        while len(self._workers) < min(self.n_workers, len(self._queue)):
            self._workers.append(asyncio.create_task(self._worker()))

    async def _worker(self) -> None:
        proc: asyncio.subprocess.Process | None = None
        try:
            while self._queue:
                job = self._queue.pop(0)
                if self._queued.get((job.ws, job.rel)) is job:
                    del self._queued[(job.ws, job.rel)]
                key = hashlib.sha1(f"{job.ws}\0{job.rel}\0{job.mtime}\0{job.size}".encode()).hexdigest()
                out = self.text_path(job.ws, key)
                if proc is None or proc.returncode is not None:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", f"{__package__}.search_extract", cwd=str(Path(__file__).resolve().parents[2]),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                assert proc.stdin is not None and proc.stdout is not None
                error: str | None = None
                chars = 0
                try:
                    proc.stdin.write((json.dumps({"src": job.src, "fmt": job.fmt, "out": str(out)}) + "\n").encode())
                    await proc.stdin.drain()
                    line = await asyncio.wait_for(proc.stdout.readline(), JOB_TIMEOUT_S)
                    ans = json.loads(line) if line else {"ok": False, "error": "the extractor stopped"}
                    chars = int(ans.get("chars") or 0)
                    error = None if ans.get("ok") else str(ans.get("error") or "failed")
                except TimeoutError:
                    error = f"took longer than {int(JOB_TIMEOUT_S)} s"
                    proc.kill()
                    await proc.wait()
                except (OSError, ValueError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    proc.kill()
                    await proc.wait()
                self._record(job, key, chars, error)
                self.done[job.ws] = self.done.get(job.ws, 0) + 1
                for f in job.futures:
                    if not f.done():
                        f.set_result(error is None)
        finally:
            # no longer counted from here on: files scheduled meanwhile get a worker of their own
            me = asyncio.current_task()
            self._workers = [w for w in self._workers if w is not me]
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def close(self) -> None:
        workers, self._queue = list(self._workers), []
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers = []
        with self._lock:
            self._db.close()


def index_of(ctx: AppContext) -> TextIndex:
    ix = ctx.extra.get("search_index")
    if ix is None:
        ix = ctx.extra["search_index"] = TextIndex(ctx.settings.state_dir)
    return ix


async def index_all(ctx: AppContext) -> None:
    """Fill the cache for every open folder (after a start; files already extracted are kept)."""
    await asyncio.sleep(STARTUP_DELAY_S)
    ix = index_of(ctx)
    for rt in list(ctx.workspaces.runtimes.values()) if ctx.workspaces is not None else []:
        try:
            sc = await asyncio.to_thread(scan, rt.root, "")
        except OSError as exc:
            log.warning("search index: cannot scan %s: %s", rt.root, exc)
            continue
        ix.prune(rt.id, {o[0] for o in sc.office})
        _, stale = ix.split(rt.id, sc.office)
        if stale:
            log.info("search index: extracting the text of %d files in %s", len(stale), rt.root)
            ix.schedule(rt.id, stale)


def start(ctx: AppContext) -> None:
    ctx.extra["search_task"] = asyncio.create_task(index_all(ctx))


async def stop(ctx: AppContext) -> None:
    t = ctx.extra.pop("search_task", None)
    if t is not None:
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    ix = ctx.extra.pop("search_index", None)
    if ix is not None:
        await ix.close()


# ----------------------------------------------------------------------------------------------------------- names
def _folder(ctx: AppContext, path: str) -> tuple[DocumentService, str]:
    svc = ctx.documents
    assert svc is not None
    norm, p = svc.resolve(path)
    if not p.is_dir():
        raise bad_request("not a directory", "NOT_A_DIRECTORY")
    return svc, norm


def search_names(svc: DocumentService, sub: str, q: str, limit: int = MAX_NAME_RESULTS) -> dict[str, Any]:
    q = q.strip()
    glob = any(c in q for c in "*?[")
    terms = q.lower().split()
    found: list[tuple[tuple, dict[str, Any]]] = []
    deadline = time.monotonic() + NAME_SCAN_S
    truncated = False
    root = svc.root
    base = root / sub if sub else root
    for dirpath, dirnames, filenames in os.walk(base):
        if time.monotonic() > deadline or len(found) > 20 * limit:
            truncated = True
            break
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        entries = [(d, "dir") for d in dirnames] + [(f, "file") for f in filenames]
        for name, typ in entries:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            low = name.lower()
            if glob:
                if not fnmatch.fnmatch(low, q.lower()):
                    continue
                score: tuple = (0, len(rel))
            else:
                hay = rel.lower()
                if not all(t in hay for t in terms):
                    continue
                in_name = sum(1 for t in terms if t in low)
                score = (-in_name, not low.startswith(terms[0]), len(rel), rel)
            item: dict[str, Any] = {"path": rel, "name": name, "type": typ}
            if typ == "file":
                fmt = document_format(name)
                item.update(kind=DocumentService.classify(name), format=fmt, openable=fmt != "unsupported")
            else:
                item["kind"] = "dir"
            found.append((score, item))
        # linked folders are listed (as in the tree) but not entered
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
    found.sort(key=lambda x: x[0])
    return {"path": sub, "items": [it for _, it in found[:limit]], "truncated": truncated or len(found) > limit}


@router.get("/search/names")
async def names(q: str = Query(..., min_length=1, max_length=300), path: str = Query(""), limit: int = Query(MAX_NAME_RESULTS, ge=1, le=1000),
                ctx: AppContext = Depends(require_auth)):
    svc, norm = _folder(ctx, path)
    if not q.strip():
        raise bad_request("give a name to look for", "INVALID_QUERY")
    return await asyncio.to_thread(search_names, svc, norm, q, limit)


# --------------------------------------------------------------------------------------------------------- content
def pattern(q: str, case: bool, word: bool) -> re.Pattern[str]:
    p = re.escape(q)
    if word:
        p = rf"(?<!\w){p}(?!\w)"
    return re.compile(p, 0 if case else re.IGNORECASE)


def file_hits(path: str, pat: re.Pattern[str], paged: bool) -> tuple[list[dict[str, Any]], int]:
    """The first matches in a file (line, page, the line around it), and how many there are."""
    with open(path, "rb") as f:
        text = f.read(TEXT_MAX).decode("utf-8", errors="replace")
    hits: list[dict[str, Any]] = []
    total = 0
    line, page, pos = 1, 1, 0
    for m in pat.finditer(text):
        s, e = m.start(), m.end()
        if e == s:
            continue
        total += 1
        if len(hits) >= MAX_HITS:
            continue
        seg = text[pos:s]
        line += seg.count("\n")
        page += seg.count("\f")
        pos = s
        ls = max(text.rfind("\n", 0, s), text.rfind("\f", 0, s)) + 1
        ends = [i for i in (text.find("\n", e), text.find("\f", e)) if i >= 0]
        le = min(ends) if ends else len(text)
        a, b = max(ls, s - 70), min(le, e + 90)
        raw = text[a:b]
        lead = len(raw) - len(raw.lstrip())
        snippet = raw.strip().replace("\t", " ")
        hit: dict[str, Any] = {"n": total - 1, "text": snippet, "s": s - a - lead, "e": e - a - lead,
                               "ctx": text[max(0, s - 60):s], "cut": [a > ls, b < le]}
        if paged:
            hit["page"] = page
        else:
            hit["line"] = line
        hits.append(hit)
    return hits, total


async def _grep(files: list[str], q: str, case: bool, word: bool) -> list[str]:
    """The files (of these) that have the text (a fast first pass; the matches are then found in Python)."""
    args = ["grep", "-a", "-l", "-Z", "-F"]
    if not case:
        args.append("-i")
    if word:
        args.append("-w")
    proc = await asyncio.create_subprocess_exec(*args, "-e", q, "--", *files, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C.UTF-8"})
    try:
        out, _ = await proc.communicate()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]


def _chunks(items: list[tuple[str, str, str]], max_files: int = 400, max_bytes: int = 120_000) -> list[list[tuple[str, str, str]]]:
    out: list[list[tuple[str, str, str]]] = []
    cur: list[tuple[str, str, str]] = []
    size = 0
    for it in items:
        n = len(it[1].encode("utf-8", errors="surrogateescape")) + 1
        if cur and (len(cur) >= max_files or size + n > max_bytes):
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += n
    if cur:
        out.append(cur)
    return out


async def search_content(ctx: AppContext, sub: str, q: str, case: bool, word: bool) -> AsyncIterator[dict[str, Any]]:
    rt_id = ctx.workspace_id
    svc = ctx.documents
    assert svc is not None
    t0 = time.monotonic()
    sc = await asyncio.to_thread(scan, svc.root, sub)
    ix = index_of(ctx)
    fresh, stale = ix.split(rt_id, sc.office)
    if stale:
        yield {"type": "status", "extracting": len(stale)}
        futs = ix.schedule(rt_id, stale, front=True)
        await asyncio.wait(futs, timeout=WAIT_EXTRACT_S)
        fresh, stale = ix.split(rt_id, sc.office)
    # (rel, file to read, fmt): text files as they are, the others through their extracted text
    targets = [(rel, ab, document_format(rel)) for rel, ab in sc.text] + fresh
    by_file = {t[1]: t for t in targets}
    total = len(targets)
    yield {"type": "progress", "searched": 0, "total": total}
    pat = pattern(q, case, word)
    sem = asyncio.Semaphore(GREP_PARALLEL)

    async def run(chunk: list[tuple[str, str, str]]) -> tuple[int, list[str]]:
        async with sem:
            return len(chunk), await _grep([c[1] for c in chunk], q, case, word)

    tasks = [asyncio.create_task(run(c)) for c in _chunks(targets)]
    files = hits = searched = 0
    truncated = False
    try:
        for fut in asyncio.as_completed(tasks):
            n, matched = await fut
            searched += n
            for p in sorted(matched):
                t = by_file.get(p)
                if t is None:
                    continue
                rel, _, fmt = t
                try:
                    found, count = await asyncio.to_thread(file_hits, p, pat, fmt in ("pdf", "pptx"))
                except OSError:
                    continue
                if not count:
                    continue
                files += 1
                hits += count
                yield {"type": "file", "path": rel, "format": fmt, "kind": DocumentService.classify(rel.rsplit("/", 1)[-1]),
                       "count": count, "hits": found}
                if files >= MAX_FILES:
                    truncated = True
                    break
            if truncated:
                break
            yield {"type": "progress", "searched": searched, "total": total}
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    yield {"type": "done", "files": files, "hits": hits, "truncated": truncated, "searched": searched,
           "pending": len(stale), "indexing": ix.pending(rt_id), "links": sc.links, "ms": int((time.monotonic() - t0) * 1000)}


@router.get("/search/content")
async def content(q: str = Query(..., min_length=1, max_length=300), path: str = Query(""), case: bool = Query(False), word: bool = Query(False),
                  ctx: AppContext = Depends(require_auth)):
    _, norm = _folder(ctx, path)
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        raise bad_request("give a text to look for", "INVALID_QUERY")

    async def gen() -> AsyncIterator[bytes]:
        async for ev in search_content(ctx, norm, q, case, word):
            yield (json.dumps(ev) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

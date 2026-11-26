"""References in agent answers: the agent links what it refers to (absolute path + a place), the reader jumps there.

* `citation_rules`: the prompt part asking for those links (document discussions and chats, not agent reviews — their
  comments may be posted to GitHub).
* POST /refs/resolve: link targets → where they are. An absolute path inside an open folder is that folder's document;
  elsewhere on the server it opens in Local files. A path that is not there (relative, mistyped folder) is looked up
  by file name in every open folder: several matches are all returned and the user picks one.

Fragments, kept as given: `#<heading text>` (a section), `#page=N` (a PDF page), `#L10` / `#L10-L20` (lines).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .deps import AppContext, require_auth_mutating

router = APIRouter()

MAX_CANDIDATES = 20
MAX_WALK = 50_000  # entries looked at per folder when looking a file name up
SKIP = {"node_modules", "__pycache__", ".git", ".venv", "venv", ".waw", ".waw-chat", ".waw-github"}


def citation_rules(ctx: AppContext, here: Path | None = None) -> str:
    """The prompt part that makes the agent link what it refers to."""
    roots = [str(rt.root) for rt in (ctx.workspaces.runtimes.values() if ctx.workspaces is not None else [])]
    return (
        "LINKS TO WHAT YOU REFER TO: the user reads your answer in an app where a link opens the place it points at. "
        "In your answer, every time you refer to a document, file, section, figure, table, page or lines — of this "
        "document or of any other — make it a Markdown link whose target is the file's ABSOLUTE path plus a fragment "
        "for the place:\n"
        "- a section of a Markdown / text / Word document: `#` and the heading text as written, spaces as %20, e.g. "
        "[the L3 cache](/wiki/gpu/memory.md#L3%20cache%20hierarchy)\n"
        "- a page of a PDF or a slide of a deck: `#page=N`, e.g. [Figure 3](/papers/kernels.pdf#page=7)\n"
        "- lines of a source or plain-text file: `#L120` or `#L120-L140`\n"
        "- the whole file: no fragment.\n"
        "The link text says what is there (section title, figure, function), not the path. When a point rests on "
        "several places, link each one. Only use headings, pages and lines you actually saw; never invent them. "
        + (f"This document is {here}. " if here else "")
        + ("Open folders: " + ", ".join(roots) + "." if roots else "")
        + "\n\nDIAGRAMS: when a sequence, state, flow or structure is easier to see than to read, draw it in a "
        "```mermaid block (sequenceDiagram, stateDiagram-v2, flowchart, classDiagram); the app draws it."
    )


def _runtimes(ctx: AppContext) -> list[Any]:
    if ctx.workspaces is None:
        return []
    rts = list(ctx.workspaces.runtimes.values())
    # the folder of this request first, then the window's order
    return sorted(rts, key=lambda rt: rt.id != ctx.workspace_id)


def _inside(rt: Any, p: Path) -> str | None:
    """The path of `p` relative to the folder `rt`, or None when it is not in it."""
    for root in {rt.root, rt.root.resolve()}:
        for cand in {p, p.resolve() if p.exists() else p}:
            try:
                rel = cand.relative_to(root)
            except ValueError:
                continue
            return rel.as_posix() if str(rel) != "." else ""
    return None


def _doc(rt: Any, rel: str, frag: str) -> dict[str, Any]:
    return {"kind": "doc", "workspace_id": rt.id, "workspace_name": rt.root.name, "path": rel,
            "abs": str(rt.root / rel), "fragment": frag}


def _by_name(ctx: AppContext, name: str, tail: list[str]) -> list[Any]:
    """(runtime, rel) of every file called `name` in the open folders; those whose folders end like `tail` first."""
    found: list[tuple[int, Any, str]] = []
    low = name.lower()
    for rt in _runtimes(ctx):
        seen = 0
        for dirpath, dirnames, filenames in os.walk(rt.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith("."))
            seen += len(dirnames) + len(filenames)
            for f in filenames:
                if f.lower() == low:
                    rel = (Path(dirpath) / f).relative_to(rt.root).as_posix()
                    parts = rel.split("/")
                    score = sum(1 for a, b in zip(reversed(parts[:-1]), reversed(tail), strict=False) if a == b)
                    found.append((-score, rt, rel))
            if seen > MAX_WALK or len(found) > MAX_CANDIDATES * 3:
                break
    found.sort(key=lambda x: (x[0], x[2]))
    return [(rt, rel) for _, rt, rel in found[:MAX_CANDIDATES]]


def resolve_target(ctx: AppContext, target: str, from_path: str | None = None) -> list[dict[str, Any]]:
    raw = target.strip()
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    path_part, _, frag = raw.partition("#")
    path_part = unquote(path_part).strip()
    frag = unquote(frag)
    if not path_part:
        return []
    out: list[dict[str, Any]] = []
    if path_part.startswith("/"):
        p = Path(os.path.normpath(path_part))
        for rt in _runtimes(ctx):
            rel = _inside(rt, p)
            if rel is not None and (rt.root / rel).is_file():
                return [_doc(rt, rel, frag)]
        if p.is_file():
            return [{"kind": "local", "abs": str(p), "fragment": frag}]
    else:
        rel = os.path.normpath(path_part)
        rts = _runtimes(ctx)
        if from_path and rts:  # relative to the document discussed, as in the document's own links
            base = os.path.normpath(os.path.join(os.path.dirname(from_path), path_part))
            if not base.startswith("..") and (rts[0].root / base).is_file():
                out.append(_doc(rts[0], base, frag))
        for rt in rts:
            if not rel.startswith("..") and (rt.root / rel).is_file() and not any(
                    c["workspace_id"] == rt.id and c["path"] == rel for c in out):
                out.append(_doc(rt, rel, frag))
        if out:
            return out
    # not where it says: the files of that name in the open folders
    parts = [x for x in path_part.split("/") if x and x != "."]
    if not parts:
        return []
    return [_doc(rt, rel, frag) for rt, rel in _by_name(ctx, parts[-1], parts[:-1])]


class ResolveIn(BaseModel):
    targets: list[str] = Field(min_length=1, max_length=200)
    from_path: str | None = Field(None, max_length=4096)  # the document discussed (in this request's folder)


@router.post("/refs/resolve")
def resolve_refs(body: ResolveIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Each link target → its candidates (none: not found; several: the user picks)."""
    return {"results": [{"target": t, "candidates": resolve_target(ctx, t[:4096], body.from_path)} for t in body.targets]}

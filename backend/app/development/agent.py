"""Agent turns of a development workspace: grounded in the whole reference wiki, aware of the editor.

A turn runs the provider CLI in the worktree (on the SSH host for remote workspaces) and tells the agent where the
reference wiki is: the wiki folder itself on this server, or a read-only snapshot copied to the host
(`<work_dir>/wiki/<folder id>`, refreshed when older than SNAPSHOT_MAX_AGE before a turn). The agent searches the
wiki for what applies to the task, follows it, and lists the pages it relied on.

Editor context (from the integration extension or the app): file, selection, language, diagnostics, unsaved buffer.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import Any

from ..deps import AppContext
from ..errors import ApiError
from ..models import DevWorkspace
from .service import DevService, settings_of
from .targets import TargetError

SNAPSHOT_MAX_AGE = 600  # seconds
_snap: dict[str, float] = {}  # "<host>:<wiki ws id>" -> last sync

KINDS = ("ask", "explain", "fix", "tests", "implement", "review", "ci", "verify")

KIND_INSTRUCTIONS = {
    "ask": "Answer the user's question about the code. Only change files if the message asks for a change.",
    "explain": "Explain the selected code: what it does, how it fits the surrounding code, and how it relates to what the "
               "wiki documents. Do not modify files.",
    "fix": "Fix the problem described (and the diagnostics given, if any) with a minimal, focused change. Then build / run the "
           "relevant tests and report the result.",
    "tests": "Write or extend tests for the selected code (follow the repository's existing test layout and style), run them, "
             "and report the result. Change production code only if a test reveals a real bug — say so explicitly.",
    "implement": "Implement the requested change end to end: plan it briefly from the wiki's requirements and the existing "
                 "code, make the edits, add or update tests, build and run them, and summarize the change.",
    "review": "Address the pull request's review comments: read the unresolved comments (e.g. `gh api repos/{repo}/pulls/{n}/comments` "
              "and `gh pr view {n} --repo {repo} --comments`), change the code where a comment is right, and for each comment "
              "say what you did or why you disagree.",
    "ci": "Fix the pull request's failing CI: find the failing checks (`gh pr checks {n} --repo {repo}`) and their logs "
          "(`gh run view <run id> --repo {repo} --log-failed`), reproduce the failure locally when feasible, fix it, and run "
          "the relevant tests.",
    "verify": "Verify the current changes of the branch (`git diff {base}...HEAD` and uncommitted changes) against the wiki: "
              "check each relevant requirement, convention and documented behavior, and report what is satisfied, what is "
              "missing or contradicted (with file/line references), and what the wiki does not cover. Do not modify files.",
}


async def remote_wiki_dir(ctx: AppContext, row: DevWorkspace) -> str | None:
    if not row.wiki_workspace_id:
        return None
    svc = DevService(ctx)
    t = svc.target(row)
    host = ctx.settings.development.host(t.name)
    if host is None:
        return None
    return f"{await t.expand(host.work_dir)}/wiki/{row.wiki_workspace_id}"


async def sync_wiki_snapshot(ctx: AppContext, row: DevWorkspace, *, force: bool = True) -> dict[str, Any]:
    """Copy the reference wiki folder (without .git) to the SSH host as a read-only snapshot."""
    svc = DevService(ctx)
    t = svc.target(row)
    if t.is_local:
        return {"synced": False, "reason": "local workspaces read the wiki folder directly"}
    root = svc.wiki_root(row)
    if root is None:
        raise ApiError(409, "WIKI_NOT_OPEN", "the reference wiki folder is not open in this window")
    key = f"{t.name}:{row.wiki_workspace_id}"
    if not force and time.time() - _snap.get(key, 0) < SNAPSHOT_MAX_AGE:
        return {"synced": False, "reason": "fresh"}
    dest = await remote_wiki_dir(ctx, row)
    assert dest
    tar = await asyncio.create_subprocess_exec("tar", "-C", str(root), "--exclude=.git", "--exclude=node_modules", "-czf", "-", ".",
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    data, err = await tar.communicate()
    if tar.returncode != 0:
        raise ApiError(400, "WIKI_SNAPSHOT_FAILED", f"tar failed: {err.decode('utf-8', 'replace')[:300]}")
    q = shlex.quote
    script = (f"rm -rf {q(dest + '.new')} && mkdir -p {q(dest + '.new')} && tar -xzf - -C {q(dest + '.new')} && "
              f"chmod -R a-w {q(dest + '.new')} && (chmod -R u+w {q(dest)} 2>/dev/null; rm -rf {q(dest)}) && mv {q(dest + '.new')} {q(dest)}")
    rc, _, e2 = await t.run(["sh", "-c", script], timeout=900, stdin=data)
    if rc != 0:
        raise ApiError(400, "WIKI_SNAPSHOT_FAILED", f"copying the wiki to {t.name} failed: {e2.strip()[:300]}")
    _snap[key] = time.time()
    return {"synced": True, "path": dest, "bytes": len(data), "at": _snap[key]}


KIND_OF = {**{e: "an image" for e in ("png", "jpg", "jpeg", "gif", "webp", "bmp")}, "svg": "an SVG image (XML text)",
           "pdf": "a PDF document", "docx": "a Word document", "pptx": "a PowerPoint deck", "xlsx": "an Excel workbook",
           "zip": "a ZIP archive", "tar": "a tar archive", "gz": "a compressed file", "so": "a shared library", "a": "a static library",
           "o": "an object file", "onnx": "an ONNX model", "pt": "a PyTorch file", "pth": "a PyTorch file", "npy": "a NumPy array",
           "bin": "a binary file", "whl": "a Python wheel"}


def _context_text(c: dict[str, Any] | None) -> str | None:
    if not c:
        return None
    parts: list[str] = []
    if c.get("folder"):
        parts.append(f"This discussion is about the folder `{c['folder']}` of the worktree: focus on it and what it contains "
                     "(list and read its files as needed), and look at the rest of the code when it helps.")
    f = c.get("file")
    if f:
        loc = f"`{f}`"
        sel = c.get("selection") or {}
        if sel.get("start_line"):
            loc += f" lines {sel['start_line']}–{sel.get('end_line', sel['start_line'])}"
        kind = KIND_OF.get(str(f).lower().rsplit(".", 1)[-1]) if "." in str(f) else None
        if kind:
            parts.append(f"The file {loc} is {kind}." + (" The picture is attached to this message." if kind.startswith("an image")
                         else " Read it with a suitable tool (e.g. `pdftotext`, `python -c` with a library) when you need its content."))
        if c.get("whole_file"):
            parts.append(f"This discussion is about the whole file {loc}" + (f" ({c['language']})" if c.get("language") else "") +
                         " (read its current content when you need it; it may have changed since earlier messages).")
        else:
            parts.append(f"The user is in the editor at {loc}" + (f" ({c['language']})" if c.get("language") else "") + ".")
        if sel.get("text"):
            parts.append("Selected code:\n```\n" + str(sel["text"])[:20000] + "\n```")
    if c.get("diff"):
        what = c.get("diff_label") or "the change"
        parts.append(f"The user is reviewing {what} in the Changes view. The diff they are asking about:\n```diff\n"
                     + str(c["diff"])[:30000] + "\n```")
    diags = c.get("diagnostics") or []
    if diags:
        lines = [f"- {d.get('file', f or '')}:{d.get('line', '?')}: {d.get('severity', 'error')}: {str(d.get('message', ''))[:500]}"
                 + (f" [{d['source']}]" if d.get("source") else "") for d in diags[:60] if isinstance(d, dict)]
        parts.append("Diagnostics reported by the editor's language servers:\n" + "\n".join(lines))
    parts.extend(
        f"The editor has UNSAVED changes in `{u['file']}` (the file on disk is older). Its current editor content:\n```\n"
        + str(u.get("text", ""))[:40000] + "\n```\nBase your work on this content; if you edit this file, tell the user, "
        "because the unsaved editor buffer will conflict with your change."
        for u in (c.get("unsaved") or [])[:5] if isinstance(u, dict) and u.get("file"))
    files = c.get("files") or []
    if files:
        parts.append("Files the user put in the context of this request" + (" (their current content follows)" if any(f.get("text") is not None for f in files) else "") + ":")
        for f in files:
            if f.get("text") is None:
                parts.append(f"- `{f['path']}`" + (f" ({f['note']})" if f.get("note") else " (read it yourself)"))
            else:
                parts.append(f"`{f['path']}`:\n```\n{f['text']}\n```")
    if c.get("page_text"):
        parts.append(f"Text of {c.get('page') or 'what the user selected'}:\n```\n" + str(c["page_text"])[:20000] + "\n```")
    if c.get("_images") and c.get("page"):
        parts.append(f"A picture of {c['page']} is attached to this message.")
    elif c.get("_images"):
        parts.append("A picture is attached to this message (what the user is looking at).")
    if c.get("task_output"):
        parts.append("Output of the failed command" + (f" `{c.get('task_command')}`" if c.get("task_command") else "") + " (tail):\n```\n"
                     + str(c["task_output"])[-30000:] + "\n```")
    return "\n\n".join(parts) or None


def build_dev_prompt(*, row: DevWorkspace, host_label: str, wiki: str | None, question: str, kind: str,
                     context: dict[str, Any] | None, prior: list[dict[str, str]] | None) -> str:
    cfg = settings_of(row)
    base = (row.base_ref or "").removeprefix("refs/waw/base/")
    p = [
        f"You are a software engineer working in a development workspace: the {'folder' if row.mode == 'folder' else 'Git worktree'} at {row.worktree_path} on "
        f"{host_label} (your working directory), branch `{row.branch}`" + (f", based on `{base}`" if base else "") + ".",
    ]
    if row.pr_number and row.github_repo:
        p.append(f"This work belongs to pull request {row.github_repo}#{row.pr_number}"
                 + (f"; pushes go to branch `{row.push_branch}`" if row.push_branch else "") + ".")
    elif row.github_repo:
        p.append(f"The changes will become a new pull request against {row.github_repo}" + (f" `{base}`" if base else "") + ".")
    if wiki:
        p.append(
            f"REFERENCE WIKI: the whole folder {wiki} documents the requirements, design decisions and conventions for this "
            "code. Before you change code, search it for everything relevant to the task and to the code you touch (for "
            f"example `rg -n -i '<terms>' {shlex.quote(wiki)}`, then read the matching pages and the pages they link to). "
            "Follow what it says; when the code or the request contradicts the wiki, point that out instead of silently "
            "picking one. The wiki is read-only for you: never modify it. End your answer with a section **Wiki references** "
            "listing the wiki pages you relied on (paths relative to the wiki folder) and one line each on what you took from "
            "them, and say explicitly when the wiki does not cover something.")
    rules = ["Edit files only inside the worktree.",
             "Do not commit, push, switch branches, reset or rewrite history: the user reviews the diff and commits from the app."]
    if cfg.get("build_command"):
        rules.append(f"Build command: `{cfg['build_command']}`.")
    if cfg.get("test_command"):
        rules.append(f"Test command: `{cfg['test_command']}`.")
    if cfg.get("python"):
        rules.append(f"Python interpreter: `{cfg['python']}`.")
    p.append("Rules: " + " ".join(rules))
    ctx_text = _context_text(context)
    if ctx_text:
        p.append(ctx_text)
    if prior:
        p.append("The conversation so far:")
        p.extend(f"[{m['role']}] {m['content'][:6000]}" for m in prior[-30:])
    p += ["User message:", question]
    instr = KIND_INSTRUCTIONS.get(kind, KIND_INSTRUCTIONS["ask"]).format(repo=row.github_repo or "<owner/repo>", n=row.pr_number or "<number>",
                                                                       base=base or "HEAD~1")
    p.append("Instructions: " + instr + " Do not ask for additional confirmation beyond your own configured permission rules.")
    return "\n\n".join(p)


_skills_snap: dict[str, tuple[float, str]] = {}  # "<host>" -> (time, fingerprint) of the last copy


def local_skill_folders(ctx: AppContext, row: DevWorkspace) -> list[Path]:
    """Folders whose skills a turn of this workspace gets: worktree, checkout, wiki, every open folder."""
    svc = DevService(ctx)
    wiki = svc.wiki_root(row)
    folders: list[Path] = [Path(x) for x in (row.worktree_path, row.source_repo) if x and svc.target(row).is_local]
    if wiki:
        folders.append(wiki)
    if ctx.workspaces is not None:
        folders += [rt.root for rt in ctx.workspaces.runtimes.values() if rt.root not in folders]
    from ..skills import connected_folders

    folders += [p for p in connected_folders(ctx.db) if p not in folders]
    return folders


async def sync_skills(ctx: AppContext, row: DevWorkspace) -> dict[str, str]:
    """Copy the skill folders of every connected folder of this window to the SSH host (read-only), refreshed when they
    changed. Returns {local skill folder: its copy on the host}."""
    import hashlib

    from .. import skills as sk

    svc = DevService(ctx)
    t = svc.target(row)
    host = ctx.settings.development.host(t.name)
    if host is None:
        return {}
    roots = sk.skill_roots(local_skill_folders(ctx, row))
    if not roots:
        return {}
    base = f"{await t.expand(host.work_dir)}/skills"
    mapping = {str(r): f"{base}/{hashlib.sha1(str(r.resolve()).encode()).hexdigest()[:12]}" for r in roots}
    fp = hashlib.sha1()
    for r in roots:
        for f in sorted(r.rglob("*")):
            try:
                st = f.stat()
                fp.update(f"{f}:{st.st_size}:{st.st_mtime_ns}".encode())
            except OSError:
                continue
    finger = fp.hexdigest()
    last = _skills_snap.get(t.name)
    if last and last[1] == finger and time.time() - last[0] < 3600:
        return mapping
    q = shlex.quote
    for r, dest in mapping.items():
        tar = await asyncio.create_subprocess_exec("tar", "-C", r, "-czhf", "-", ".", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        data, _err = await tar.communicate()
        if tar.returncode != 0:
            continue
        script = (f"(chmod -R u+w {q(dest)} 2>/dev/null; rm -rf {q(dest)}); mkdir -p {q(dest)} && tar -xzf - -C {q(dest)} && "
                  f"chmod -R a-w {q(dest)}")
        rc, _, e2 = await t.run(["sh", "-c", script], timeout=300, stdin=data)
        if rc != 0:
            raise ApiError(400, "SKILLS_COPY_FAILED", f"copying skills to {t.name} failed: {e2.strip()[:300]}")
    _skills_snap[t.name] = (time.time(), finger)
    return mapping


async def dev_turn(ctx: AppContext, row: DevWorkspace, *, question: str, kind: str, context: dict[str, Any] | None,
                   prior: list[dict[str, str]] | None) -> tuple[str, dict[str, Any]]:
    """(prompt, job context additions) for a turn of this development workspace's conversation."""
    if row.status != "ready" or not row.worktree_path:
        raise ApiError(409, "DEV_NOT_READY", f"the development workspace is {row.status}")
    svc = DevService(ctx)
    if context and context.get("files"):
        context = {**context, "files": await _file_contents(ctx, row, context["files"])}
    t = svc.target(row)
    extra: dict[str, Any] = {"dev_id": row.id, "_cwd": row.worktree_path}
    # pictures the agent sees: one sent with the question (a PDF page / area), the image file discussed
    images = [x for x in ((context or {}).get("_images") or []) if isinstance(x, str)]
    fpath = str((context or {}).get("file") or "")
    if t.is_local and fpath and fpath.lower().rsplit(".", 1)[-1] in IMAGE_EXTS:
        p = Path(fpath) if fpath.startswith("/") else Path(row.worktree_path) / fpath
        if p.is_file():
            images.append(str(p))
    if images:
        extra["attachments"] = images
    wiki: str | None = None
    if row.wiki_workspace_id:
        if t.is_local:
            root = svc.wiki_root(row)
            wiki = str(root) if root else None
        else:
            try:
                await sync_wiki_snapshot(ctx, row, force=False)
                wiki = await remote_wiki_dir(ctx, row)
            except (ApiError, TargetError):
                wiki = None
    if t.is_local:
        # the folder / file discussed first (its own skills and its project's), then the worktree, checkout, wiki
        c = context or {}
        fpath = str(c.get("file") or "")
        focus = c.get("folder") or (fpath.rsplit("/", 1)[0] if "/" in fpath else "")
        here = [f"{row.worktree_path.rstrip('/')}/{focus}"] if focus and not str(focus).startswith("/") else []
        extra["_skill_folders"] = [x for x in (*here, row.worktree_path, row.source_repo, wiki) if x]
    else:
        rex = t.remote_exec(row.worktree_path)
        assert rex is not None
        try:
            skill_map = await sync_skills(ctx, row)
        except (ApiError, TargetError):
            skill_map = {}
        extra["_remote"] = {"ssh_prefix": rex.ssh_prefix, "bins": rex.bins, "cwd": rex.cwd,
                            "skill_map": skill_map, "skill_dirs": list(skill_map.values())}
    prompt = build_dev_prompt(row=row, host_label=t.label(), wiki=wiki, question=question, kind=kind if kind in KINDS else "ask",
                              context=context, prior=prior)
    return prompt, extra


IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
FILES_MAX = 30          # files listed
FILE_CHARS = 40_000     # content of one file
FILES_CHARS = 120_000   # content of all files together


async def _file_contents(ctx: AppContext, row: DevWorkspace, files: Any) -> list[dict[str, Any]]:
    """`[{root, path}]` from the editor → paths as the agent sees them, with the content while it fits."""
    from .files import DevFiles

    if not isinstance(files, list):
        return []
    df = DevFiles(ctx, row.id)
    roots = await df.roots()
    out: list[dict[str, Any]] = []
    budget = FILES_CHARS
    for f in files[:FILES_MAX]:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            continue
        root = f.get("root") if f.get("root") in roots else "wt"
        shown = f["path"] if root == "wt" else df.join(roots[root]["path"], f["path"])
        item: dict[str, Any] = {"path": shown, "text": None}
        if budget > 0:
            try:
                r = await df.read(root, f["path"])
                if r["binary"]:
                    item["note"] = "binary"
                elif len(r["text"]) <= min(FILE_CHARS, budget):
                    item["text"] = r["text"]
                    budget -= len(r["text"])
                else:
                    item["note"] = f"{len(r['text'])} characters — read it yourself"
            except ApiError as exc:
                item["note"] = exc.message
        out.append(item)
    if len(files) > FILES_MAX:
        out.append({"path": f"… and {len(files) - FILES_MAX} more", "text": None, "note": "not listed"})
    return out

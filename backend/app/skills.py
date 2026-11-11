"""Agent skills of the connected folders and projects.

Every app-driven turn should know the skills (`<skill>/SKILL.md` with `name` / `description` frontmatter) of all
folders open in the window and of the projects it works on (the development worktree, its source checkout, the
reference wiki, a PR's connected local checkout) — not only those of its working directory, which is all the agent
CLIs discover by themselves. Two mechanisms:

* OpenCode (CLI): the skill folders are added to its config for the turn (`OPENCODE_CONFIG_CONTENT`
  `{"skills": {"paths": [...]}}`, merged with any value already in the environment), so its skill tool has them.
* Every provider (also Codex and remote hosts): the prompt lists them (name, description, path of SKILL.md) with the
  instruction to read a matching SKILL.md before doing the task.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# where agent tools keep project skills (OpenCode, Claude Code, Codex / the shared .agents convention)
SKILL_DIRS = (".opencode/skills", ".opencode/skill", ".claude/skills", ".agents/skills", ".codex/skills")
MAX_SKILLS = 200
SKIP = {"node_modules", ".git", "__pycache__", ".venv"}
_FRONT = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def _frontmatter(text: str) -> dict[str, str]:
    m = _FRONT.match(text)
    out: dict[str, str] = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("name", "description"):
            out[k.strip()] = v.strip().strip("'\"")
    return out


def connected_folders(db: Any) -> list[Path]:
    """Folders connected to the app besides the open ones: every reference folder (wiki folders' and PR repositories'
    `context:*`), every repository's connected local checkout (`local_repo:*`) and every PR repository's wiki."""
    from .models import GithubRepoWiki, KvSetting, Workspace

    out: list[Path] = []
    try:
        with db.read() as s:
            for row in s.query(KvSetting).filter(KvSetting.key.like("context:%") | KvSetting.key.like("local_repo:%")).all():
                try:
                    v = json.loads(row.value)
                except ValueError:
                    continue
                vals = v if isinstance(v, list) else [v.get("path")] if isinstance(v, dict) else []
                out += [Path(x) for x in vals if isinstance(x, str) and x]
            for w in s.query(GithubRepoWiki).all():
                ws = s.get(Workspace, w.workspace_id)
                if ws is not None:
                    out.append(Path(ws.root_path))
    except Exception:  # pragma: no cover - never fail a turn over this
        return []
    return [p for p in out if p.is_dir()]


def _project_chain(folder: Path) -> list[Path]:
    """The folder and its parents up to its project root (the Git top level), like the agents' own lookup from their
    working directory: a wiki that is a sub-folder of a repository also gets the repository's skills."""
    chain = [folder]
    try:
        cur = folder.resolve()
    except OSError:
        return chain
    for parent in [cur, *cur.parents]:
        if parent != cur:
            chain.append(parent)
        if (parent / ".git").exists() or parent == Path.home() or parent.parent == parent:
            break
    if not any((p / ".git").exists() for p in chain):
        return [folder]  # not in a project: only the folder itself
    return chain


def skill_roots(folders: list[Path]) -> list[Path]:
    """Existing skill folders (`.opencode/skills` …) of the given folders and their project roots, deduplicated."""
    out: list[Path] = []
    seen: set[str] = set()
    for f in folders:
        for base in _project_chain(f):
            for d in SKILL_DIRS:
                p = base / d
                try:
                    if p.is_dir():
                        key = str(p.resolve())
                        if key not in seen:
                            seen.add(key)
                            out.append(p)
                except OSError:
                    continue
    return out


def discover(folders: list[Path]) -> list[dict[str, Any]]:
    """Skills of the folders: [{name, description, path (SKILL.md), folder}], first definition of a name wins."""
    return discover_in_roots(skill_roots(folders))


def discover_in_roots(roots: list[Path]) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    names: set[str] = set()
    for root in roots:
        files: list[Path] = []
        # every SKILL.md below the skills folder, at any depth (as OpenCode scans skills.paths)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
            if "SKILL.md" in filenames:
                files.append(Path(dirpath) / "SKILL.md")
            if len(files) > MAX_SKILLS * 2:
                break
        for f in files:
            try:
                meta = _frontmatter(f.read_text("utf-8", "replace")[:4000])
            except OSError:
                continue
            name = meta.get("name") or f.parent.name
            if name in names:
                continue
            names.add(name)
            folder = str(root.parent.parent) if root.parent.name in (".opencode", ".claude", ".agents", ".codex") else str(root)
            skills.append({"name": name, "description": meta.get("description", "")[:400], "path": str(f), "folder": folder,
                           "root": str(root)})
            if len(skills) >= MAX_SKILLS:
                return skills
    return skills


def prompt_section(skills: list[dict[str, Any]], path_map: dict[str, str] | None = None) -> str | None:
    """The prompt's skills list; `path_map` rewrites local folder prefixes to where the agent sees them (SSH hosts)."""
    if not skills:
        return None
    lines = []
    for s in skills:
        p = s["path"]
        for local, remote in (path_map or {}).items():
            if p.startswith(local.rstrip("/") + "/"):
                p = remote.rstrip("/") + p[len(local.rstrip("/")):]
                break
        lines.append(f"- {s['name']}: {s['description'] or '(no description)'} — {p}")
    return ("SKILLS of the current folder and of every folder and project connected to this session (in addition to "
            "your own). Each entry is: name: description — path of its SKILL.md. When the task matches a skill, read "
            "its SKILL.md first and follow it (paths in it are relative to the skill's folder):\n" + "\n".join(lines))


def opencode_env(roots: list[Path], base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for an `opencode run` whose config also loads these skill folders."""
    env = dict(base if base is not None else os.environ)
    if not roots:
        return env
    cfg: dict[str, Any] = {}
    raw = env.get("OPENCODE_CONFIG_CONTENT")
    if raw:
        try:
            cfg = json.loads(raw)
            if not isinstance(cfg, dict):
                cfg = {}
        except ValueError:
            cfg = {}
    sk = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
    paths = [p for p in (sk.get("paths") or []) if isinstance(p, str)]
    for r in roots:
        if str(r) not in paths:
            paths.append(str(r))
    cfg["skills"] = {**sk, "paths": paths}
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
    return env


# ---------------------------------------------------------------------- what a discussion's agent gets

from fastapi import APIRouter, Depends, Query  # noqa: E402

router = APIRouter()


def _route() -> None:
    from .deps import AppContext, require_auth

    @router.get("/agent-skills")
    def agent_skills_route(doc_id: str | None = Query(None, max_length=36), ctx: AppContext = Depends(require_auth)):
        """The skills an agent turn of this folder gets (and of this document: a PR's connected local checkout)."""
        extra: list[str] = []
        if doc_id and ctx.documents is not None:
            from .annotations import ThreadService

            doc = ctx.documents.get_document(doc_id)
            extra = ThreadService(ctx)._skill_ctx(doc).get("_skill_folders", [])
        assert ctx.jobs is not None
        folders = ctx.jobs.skill_folders(ctx.workspace_root, None, {"_skill_folders": extra})
        return {"skills": discover(folders), "folders": [str(f) for f in folders]}


_route()

"""Small JSON settings in the kv_settings table, and the extra reference folders given to the agent.

Keys: `github_hub_prefs` (review hub preferences), `context:repo:<owner>/<name>` (folders for that repository's PRs),
`context:ws:<workspace id>` (folders for questions about documents of that wiki folder).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deps import AppContext


def kv_get(ctx: AppContext, key: str, default: Any) -> Any:
    from .models import KvSetting

    with ctx.db.read() as s:
        row = s.get(KvSetting, key)
        try:
            return json.loads(row.value) if row else default
        except ValueError:
            return default


def kv_put(ctx: AppContext, key: str, value: Any) -> None:
    from .models import KvSetting

    with ctx.db.write() as s:
        row = s.get(KvSetting, key)
        if row is None:
            s.add(KvSetting(key=key, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)


def context_dirs(ctx: AppContext, scope: str) -> list[str]:
    """The scope's folders that still exist."""
    return [d for d in kv_get(ctx, f"context:{scope}", []) if isinstance(d, str) and Path(d).is_dir()]


def context_prompt(dirs: list[str]) -> str | None:
    if not dirs:
        return None
    return ("Additional reference folders the user added for more accurate answers (read files there as needed for "
            "background; do not modify them): " + "; ".join(dirs))


def scopes_for(doc_path: str, workspace_id: str) -> list[str]:
    """Context scopes that apply to a document: its wiki folder, plus the repository for a PR tab."""
    from .documents import PR_PREFIX, is_pr_path

    scopes = [f"ws:{workspace_id}"]
    if is_pr_path(doc_path):
        parts = doc_path.replace("\\", "/").lstrip("/")[len(PR_PREFIX):].split("/")
        if len(parts) == 3:
            scopes.insert(0, f"repo:{parts[0]}/{parts[1]}".lower())
    return scopes


def extra_context(ctx: AppContext, doc_path: str, workspace_id: str) -> str | None:
    dirs: list[str] = []
    for sc in scopes_for(doc_path, workspace_id):
        dirs += [d for d in context_dirs(ctx, sc) if d not in dirs]
    parts = [context_prompt(dirs)]
    from .documents import PR_PREFIX, is_pr_path

    if is_pr_path(doc_path):  # a PR tab: its repository's connected local checkout comes first
        from .local_repos import local_repo_prompt

        bits = doc_path.replace("\\", "/").lstrip("/")[len(PR_PREFIX):].split("/")
        if len(bits) == 3 and bits[2].removesuffix(".md").isdigit():
            parts.insert(0, local_repo_prompt(ctx, f"{bits[0]}/{bits[1]}", int(bits[2].removesuffix(".md"))))
    text = "\n\n".join(p for p in parts if p)
    return text or None

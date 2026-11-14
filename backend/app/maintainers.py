"""Who maintains a repository, from what the repository itself publishes — for PR review priority (rules and the agent).

Sources, read from the default branch over REST (a few requests per repository per day, background priority):
- `docs/source/community/persons_of_interest.md` (PyTorch's governance page): Lead Core Maintainer, Core Maintainers,
  Core Module maintainers (one `### Module` per area) and Core Governed External Module maintainers; "(emeritus)" and
  the "Sunsetted Modules" section do not count as current.
- `CODEOWNERS` (`.github/`, the top folder, `docs/`): the users named as owners of a path (teams `@org/team` are not
  expanded: that needs org membership lookups).
- The people you list in Reviews ⚙ ("core maintainers"), for every repository.

Kept in the kv table (`review_maintainers`), refreshed once a day; a repository whose files are not there is remembered
as such for a day too. Lookups never wait for GitHub: the first time a repository is seen its list is fetched in the
background and the next refresh of the review list uses it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from .kv import kv_get, kv_put

log = logging.getLogger("waw.maintainers")
KEY = "review_maintainers"
TTL = 86400
POI = "docs/source/community/persons_of_interest.md"
CODEOWNERS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_LINK = re.compile(r"\[([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\]\(https://github\.com/([A-Za-z0-9-]+)/?\)")
_running: dict[str, asyncio.Task] = {}


def parse_poi(text: str) -> dict[str, dict[str, Any]]:
    """{login_lower: {"login", "name", "roles": [{"role": lead|core|module|external, "area", "current"}]}}"""
    people: dict[str, dict[str, Any]] = {}
    section, area = "", ""
    for line in text.splitlines():
        if line.startswith("## "):
            section, area = line[3:].strip().lower(), ""
            continue
        if line.startswith("### "):
            area = line[4:].strip()
            continue
        if not line.lstrip().startswith("- "):
            continue
        m = _LINK.search(line)
        if not m:
            continue
        login = m.group(2)
        if "lead core maintainer" in section:
            role = "lead"
        elif section == "core maintainers":
            role = "core"
        elif "core module maintainers" in section:
            role = "module"
        elif "external module" in section:
            role = "external"
        elif "sunset" in section:
            role = "sunset"
        else:
            continue
        name = line.lstrip()[2:].split("([", 1)[0].replace("(emeritus)", "").strip()
        current = "(emeritus)" not in line and role != "sunset"
        p = people.setdefault(login.lower(), {"login": login, "name": name, "roles": []})
        p["roles"].append({"role": role, "area": area if role in ("module", "external", "sunset") else "", "current": current})
    return people


def parse_codeowners(text: str) -> dict[str, dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        path, *owners = line.split()
        for o in owners:
            if not o.startswith("@") or "/" in o:  # emails and @org/team are not users we can match
                continue
            login = o[1:]
            p = people.setdefault(login.lower(), {"login": login, "name": "", "roles": []})
            if len([r for r in p["roles"] if r["role"] == "codeowner"]) < 8:
                p["roles"].append({"role": "codeowner", "area": path, "current": True})
    return people


def _merge(*maps: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in maps:
        for k, v in m.items():
            cur = out.setdefault(k, {"login": v["login"], "name": v.get("name") or "", "roles": []})
            cur["name"] = cur["name"] or v.get("name") or ""
            cur["roles"] += v["roles"]
    return out


def _raw(ctx: Any, repo: str, path: str) -> str | None:
    """A file of the default branch, or None when it is not there."""
    fake = ctx.extra.get("maintainer_files")
    if fake is not None:  # tests
        return fake.get(repo, {}).get(path)
    from . import gh_limits

    res = gh_limits.run_gh(["api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw"], timeout=60)
    if res.rc != 0:
        if "404" in (res.err or "") or res.status == 404 or "Not Found" in (res.out or ""):
            return None
        gh_limits.raise_for(res)
    return res.out


def fetch(ctx: Any, repo: str) -> dict[str, Any]:
    found, sources = [], []
    poi = _raw(ctx, repo, POI)
    if poi:
        found.append(parse_poi(poi))
        sources.append(POI)
    for path in CODEOWNERS:
        co = _raw(ctx, repo, path)
        if co:
            found.append(parse_codeowners(co))
            sources.append(path)
            break
    return {"fetched": time.time(), "sources": sources, "people": _merge(*found)}


def _all(ctx: Any) -> dict[str, Any]:
    return kv_get(ctx, KEY, {}) or {}


def refresh_soon(ctx: Any, repos: set[str]) -> None:
    """Fetch the lists of repositories not known or older than a day, in the background (never waits for quota)."""
    if "gh_graphql" in ctx.extra and "maintainer_files" not in ctx.extra:  # tests with a fake GitHub: only when given files
        return
    have = _all(ctx)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for repo in sorted(repos):
        key = repo.lower()
        hit = have.get(key)
        if hit and time.time() - hit.get("fetched", 0) < TTL:
            continue
        t = _running.get(key)
        if t is not None and not t.done():
            continue

        async def run(repo=repo, key=key) -> None:
            from . import gh_limits

            try:
                data = await asyncio.to_thread(gh_limits.in_priority, gh_limits.BACKGROUND, False, "maintainers", fetch, ctx, repo)
            except Exception as exc:  # noqa: BLE001 - tried again on a later refresh (after the hour, not at once)
                log.info("maintainers of %s: %s", repo, getattr(exc, "message", exc))
                data = {**(have.get(key) or {"people": {}, "sources": []}), "fetched": time.time() - TTL + 3600,
                        "error": str(getattr(exc, "message", exc))[:200]}
            cur = _all(ctx)
            cur[key] = data
            kv_put(ctx, KEY, cur)
            log.info("maintainers of %s: %d people from %s", repo, len(data["people"]), ", ".join(data.get("sources") or []) or "nothing")
        _running[key] = loop.create_task(run())


def roles_of(ctx: Any, repo: str, login: str | None, extra: list[str], cache: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The current roles of a person in a repository (your list counts as core maintainer)."""
    if not login:
        return []
    m = (cache if cache is not None else _all(ctx)).get(repo.lower()) or {}
    p = (m.get("people") or {}).get(login.lower()) or {}
    roles = [r for r in p.get("roles") or [] if r.get("current")]
    if login.lower() in {x.lower() for x in extra}:
        roles = [{"role": "listed", "area": "", "current": True}, *roles]
    return roles


def describe(roles: list[dict[str, Any]]) -> str:
    """"lead core maintainer; module maintainer: TorchInductor, Distributed; code owner of 3 paths" (or "")."""
    out = []
    kinds = {r["role"] for r in roles}
    if "listed" in kinds:
        out.append("core maintainer (your list)")
    if "lead" in kinds:
        out.append("lead core maintainer")
    elif "core" in kinds:
        out.append("core maintainer")
    mods = [r["area"] for r in roles if r["role"] in ("module", "external")]
    if mods:
        out.append("module maintainer: " + ", ".join(dict.fromkeys(mods)))
    owners = [r["area"] for r in roles if r["role"] == "codeowner"]
    if owners:
        out.append(f"code owner of {', '.join(owners[:3])}{' …' if len(owners) > 3 else ''}")
    return "; ".join(out)


def annotate(ctx: Any, items: list[dict[str, Any]], extra: list[str]) -> list[dict[str, Any]]:
    """Each review item with requested_by_core / requested_by_maintainer / requested_by_roles and the author's."""
    refresh_soon(ctx, {p["repo"] for p in items if p.get("repo")})
    cache = _all(ctx)
    out = []
    for p in items:
        rq = roles_of(ctx, p["repo"], p.get("requested_by"), extra, cache)
        au = roles_of(ctx, p["repo"], p.get("author"), extra, cache)
        out.append({**p, "requested_by_core": any(r["role"] in ("lead", "core", "listed") for r in rq),
                    "requested_by_maintainer": bool(rq), "requested_by_roles": describe(rq),
                    "author_maintainer": bool(au), "author_roles": describe(au)})
    return out


def status(ctx: Any) -> list[dict[str, Any]]:
    return [{"repo": k, "people": len(v.get("people") or {}), "sources": v.get("sources") or [], "fetched": v.get("fetched"),
             "error": v.get("error")} for k, v in sorted(_all(ctx).items())]

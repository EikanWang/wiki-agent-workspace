"""Who maintains a repository (persons_of_interest.md, CODEOWNERS, your list) for review priority."""

from __future__ import annotations

import asyncio

from test_github_hub import as_github

from backend.app import github_hub, maintainers
from backend.app import review_priority as rp

POI = """# PyTorch Governance | Maintainers

## Lead Core Maintainer

- Alban Desmaison ([albanD](https://github.com/albanD))

## Core Maintainers

- Edward Yang ([ezyang](https://github.com/ezyang))
- (emeritus) Dmytro Dzhulgakov ([dzhulgakov](https://github.com/dzhulgakov))

## Core Module maintainers

### TorchInductor

- Jason Ansel ([jansel](https://github.com/jansel))
- Edward Yang ([ezyang](https://github.com/ezyang))

### XPU/Intel GPU

- (emeritus) Old Person ([oldp](https://github.com/oldp))

## Sunsetted Modules

### TorchX

- Some One ([someone](https://github.com/someone))
"""
CODEOWNERS = "# owners\n/torch/_inductor/ @jansel @pytorch/inductor-team\n/aten/src/ATen/native/mkldnn/ @mkl-dev  # oneDNN\n"


def test_parse_the_governance_page_and_codeowners():
    p = maintainers.parse_poi(POI)
    assert maintainers.describe([r for r in p["ezyang"]["roles"] if r["current"]]) == "core maintainer; module maintainer: TorchInductor"
    assert maintainers.describe(p["albanD".lower()]["roles"]) == "lead core maintainer"
    assert not any(r["current"] for r in p["dzhulgakov"]["roles"]) and not any(r["current"] for r in p["oldp"]["roles"])
    assert not any(r["current"] for r in p["someone"]["roles"])  # sunsetted module
    co = maintainers.parse_codeowners(CODEOWNERS)
    assert set(co) == {"jansel", "mkl-dev"}  # teams are not users
    assert maintainers.describe(co["mkl-dev"]["roles"]) == "code owner of /aten/src/ATen/native/mkldnn/"


def item(n, requested_by, author="dev"):
    return {"repo": "pytorch/pytorch", "number": n, "title": f"PR {n}", "url": f"https://github.com/pytorch/pytorch/pull/{n}", "author": author,
            "requested_by": requested_by, "updated_at": "2026-11-01T00:00:00Z", "created_at": "2026-11-01T00:00:00Z", "labels": [],
            "additions": 1, "deletions": 1, "draft": False}


async def test_review_items_get_the_requester_roles_rules_use_them_and_the_agent_is_told(running_app):
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["maintainer_files"] = {"pytorch/pytorch": {maintainers.POI: POI, ".github/CODEOWNERS": CODEOWNERS}}
    items = [item(1, "ezyang"), item(2, "jansel"), item(3, "mkl-dev"), item(4, "stranger", author="albanD"), item(5, "dzhulgakov")]
    first = maintainers.annotate(h.ctx, items, [])  # not known yet: fetched in the background
    assert not any(x["requested_by_maintainer"] for x in first)
    for _ in range(100):
        if maintainers.status(h.ctx):
            break
        await asyncio.sleep(0.02)
    got = {x["number"]: x for x in maintainers.annotate(h.ctx, items, ["myfriend"])}
    assert got[1]["requested_by_core"] and got[1]["requested_by_roles"] == "core maintainer; module maintainer: TorchInductor"
    assert got[2]["requested_by_maintainer"] and not got[2]["requested_by_core"]
    assert got[2]["requested_by_roles"] == "module maintainer: TorchInductor; code owner of /torch/_inductor/"
    assert got[3]["requested_by_maintainer"] and got[3]["requested_by_roles"].startswith("code owner")
    assert not got[4]["requested_by_maintainer"] and got[4]["author_maintainer"] and got[4]["author_roles"] == "lead core maintainer"
    assert not got[5]["requested_by_maintainer"]  # emeritus
    assert maintainers.annotate(h.ctx, [item(6, "MyFriend")], ["myfriend"])[0]["requested_by_core"]  # your list
    st = maintainers.status(h.ctx)
    assert st[0]["repo"] == "pytorch/pytorch" and st[0]["sources"] == [maintainers.POI, ".github/CODEOWNERS"]
    # a rule on it
    prefs = rp.normalize({"priority_rules": [{"id": "c", "enabled": True, "level": "P0", "conditions": [{"field": "requested_by_core", "op": "is", "value": True}]}]})
    assert rp.hard_level(got[1], prefs)[0] == "P0" and rp.hard_level(got[2], prefs)[0] is None
    # the agent's prompt says it
    pr = {"repo": "pytorch/pytorch", "number": 2, "title": "PR 2", "author": "dev", "labels": [], "body": ""}
    prompt = rp.decision_prompt(pr, {**got[2], "requested_by_company": "Meta"}, prefs, [], None, None)
    assert "Review requested by: jansel (company: Meta; in this repository: module maintainer: TorchInductor; code owner of /torch/_inductor/)" in prompt
    assert "Author: dev (not listed as a maintainer or code owner of this repository)" in prompt
    # your list is saved with the preferences
    r = await h.api.put("/api/v1/github/prefs", json={"core_maintainers": ["myfriend", "myfriend", "not a login!"]})
    assert r.status_code == 400
    r = await h.api.put("/api/v1/github/prefs", json={"core_maintainers": ["myfriend", "MyFriend"]})
    assert r.status_code == 200 and r.json()["core_maintainers"] == ["myfriend"]

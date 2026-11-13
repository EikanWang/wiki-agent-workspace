"""Start a backend for browser tests: temp workspace, fake agents, fixed token, port 8799."""
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "backend"))
from conftest import make_settings, make_workspace  # noqa: E402

import uvicorn  # noqa: E402

from backend.app.main import create_app  # noqa: E402


PREFIX = "waw-e2e-"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_stale_runs() -> None:
    """Remove what earlier runs left behind when they were killed (SIGKILL skips the cleanup below): their tmux
    servers (one per run, each on its own socket) and their temp folders. A run still alive (its OWNER pid
    exists) is left alone, so two e2e servers on different ports do not disturb each other."""
    base = Path(tempfile.gettempdir())
    stale = []
    for d in base.glob(PREFIX + "*"):
        if not d.is_dir():
            continue
        try:
            owner = int((d / "OWNER").read_text().strip())
        except (OSError, ValueError):
            owner = 0
        if owner and _pid_alive(owner):
            continue
        stale.append(d)
    stale_set = {str(d) for d in stale}
    tmux = shutil.which("tmux")
    if tmux:
        # find tmux servers by their -f <tmp>/waw-e2e-XXXX/state/tmux.conf argument
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            args = [a.decode(errors="replace") for a in args if a]
            if not args or Path(args[0]).name != "tmux" or "-L" not in args or "-f" not in args:
                continue
            conf = args[args.index("-f") + 1] if args.index("-f") + 1 < len(args) else ""
            run_dir = str(Path(conf).parent.parent)
            if not Path(run_dir).name.startswith(PREFIX):
                continue
            if run_dir in stale_set or not Path(run_dir).exists():
                sock = args[args.index("-L") + 1]
                subprocess.run([tmux, "-L", sock, "kill-server"], capture_output=True, timeout=5, check=False)
    for d in stale:
        shutil.rmtree(d, ignore_errors=True)


def add_fake_github(app) -> None:
    """Minimal GitHub OAuth endpoints. The account it signs in as comes from the `fake_gh_user` cookie the test sets
    (default: the allowed `e2e-user`); `fake_gh_cancel=1` simulates clicking Cancel on GitHub."""
    from urllib.parse import urlencode

    from fastapi import Request
    from fastapi.responses import JSONResponse, RedirectResponse
    from starlette.routing import Route

    async def authorize(request: Request):
        q = request.query_params
        if request.cookies.get("fake_gh_cancel") == "1":
            return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'error': 'access_denied', 'state': q['state']})}", status_code=302)
        user = request.cookies.get("fake_gh_user", "e2e-user")
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': user, 'state': q['state']})}", status_code=302)

    async def new_app(request: Request):  # github.com/settings/apps/new: the user confirms "Create GitHub App"
        import json
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        manifest = json.loads(form["manifest"])
        return RedirectResponse(f"{manifest['redirect_url']}?{urlencode({'code': 'mcode', 'state': request.query_params['state']})}", status_code=302)

    async def conversions(request: Request):
        return JSONResponse({"id": 1, "slug": "wiki-agent-workspace-e2e", "client_id": "e2e-client", "client_secret": "e2e-secret",
                             "owner": {"login": "e2e-user"}, "html_url": "https://github.com/apps/wiki-agent-workspace-e2e"}, status_code=201)

    async def token(request: Request):
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        if form.get("client_secret") != "e2e-secret":
            return JSONResponse({"error": "incorrect_client_credentials"})
        return JSONResponse({"access_token": f"tok-{form.get('code')}"})

    async def user(request: Request):
        return JSONResponse({"login": request.headers.get("authorization", "").removeprefix("Bearer tok-")})

    async def g_auth(request: Request):
        q = request.query_params
        email = request.cookies.get("fake_google_user", "e2e@example.com")
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': email, 'state': q['state']})}", status_code=302)

    async def g_token(request: Request):
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        if form.get("client_secret") != "e2e-google-secret":
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        return JSONResponse({"access_token": f"g-{form.get('code')}"})

    async def g_userinfo(request: Request):
        email = request.headers.get("authorization", "").removeprefix("Bearer g-")
        return JSONResponse({"sub": "1", "email": email, "email_verified": True})

    # before the SPA catch-all route
    for r in reversed([Route("/fake-github/settings/apps/new", new_app, methods=["POST"]),
                       Route("/fake-github/app-manifests/mcode/conversions", conversions, methods=["POST"]),
                       Route("/fake-google/auth", g_auth), Route("/fake-google/token", g_token, methods=["POST"]),
                       Route("/fake-google/userinfo", g_userinfo),Route("/fake-github/login/oauth/authorize", authorize), Route("/fake-github/login/oauth/access_token", token, methods=["POST"]),
                       Route("/fake-github/user", user)]):
        app.router.routes.insert(0, r)


def add_fake_review_queue(app) -> None:
    """The GitHub review hub against canned data: the server's gh account is e2e-user (the fake GitHub sign-in)."""
    ctx = app.state.ctx
    ctx.extra["gh_owner"] = lambda: "e2e-user"

    def pr(n, repo, title, *, draft=False, ci="SUCCESS", updated="2026-11-08T00:00:00Z", private=False, adds=10):
        return {"number": n, "title": title, "url": f"https://github.com/{repo}/pull/{n}", "isDraft": draft,
                "createdAt": "2026-10-01T00:00:00Z", "updatedAt": updated, "additions": adds, "deletions": 1, "changedFiles": 1,
                "reviewDecision": "REVIEW_REQUIRED", "author": {"login": "alice"}, "repository": {"nameWithOwner": repo, "isPrivate": private},
                "labels": {"nodes": [{"name": "module: xpu", "color": "0e8a16"}]},
                "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "e2e-user"}}]},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": ci}}}]}}

    nodes = [pr(101, "pytorch/pytorch", "[xpu] Fix the lazy Level Zero stub", updated="2026-11-09T00:00:00Z"),
             pr(102, "pytorch/pytorch", "Draft: refactor allocator", draft=True),
             pr(103, "pytorch/pytorch", "Add XPU event timing", ci="FAILURE", adds=500),
             pr(201, "intel/torch-xpu-ops", "Speed up GEMM tiling"),
             None]  # a PR in an organization that requires SAML SSO
    meta = pr(104, "pytorch/pytorch", "[inductor] XPU codegen fix", updated="2026-11-07T00:00:00Z")
    meta["timelineItems"] = {"nodes": [{"actor": {"login": "metadev", "company": "Meta", "email": ""},
                                        "requestedReviewer": {"__typename": "User", "login": "e2e-user"}}]}  # a priority request
    me, bob = {"__typename": "User", "login": "e2e-user"}, {"__typename": "User", "login": "bob"}
    meta["comments"] = {"nodes": [{"createdAt": "2026-11-05T00:00:00Z", "author": me}, {"createdAt": "2026-11-06T00:00:00Z", "author": bob}]}  # news since yours
    nodes.insert(3, meta)
    nodes[nodes.index(next(n for n in nodes if n and n["number"] == 201))]["comments"] = {"nodes": [{"createdAt": "2026-11-05T00:00:00Z", "author": me}]}  # quiet
    done = pr(106, "pytorch/pytorch", "Already approved, nothing new")  # left out of the list until "approved" is ticked
    done.update({"headRefOid": "h106", "mine": {"nodes": [{"state": "APPROVED", "submittedAt": "2026-11-05T00:00:00Z", "commit": {"oid": "h106"}}]},
                 "comments": {"nodes": [{"createdAt": "2026-11-06T00:00:00Z", "author": {"__typename": "User", "login": "pytorchmergebot"}}]}})
    nodes.insert(4, done)
    watched = [pr(301, "pytorch/ao", "int4 weight-only quant on XPU")]  # open PRs of a watched repository

    issue = {"number": 555, "title": "XPU OOM on large batch", "url": "https://github.com/pytorch/pytorch/issues/555", "body": "Crashes with **OOM**.",
             "state": "OPEN", "stateReason": None, "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z", "author": {"login": "carol"},
             "labels": {"nodes": []}, "assignees": {"nodes": [{"login": "alice"}]},
             "comments": {"totalCount": 1, "nodes": [{"databaseId": 6001, "author": {"login": "dan"}, "body": "Same here.", "createdAt": "2026-11-02T00:00:00Z", "url": None}]}}
    posted: list = []  # what "GitHub" received (inspected by tests via /fake-github/posted)
    state = {
        "titles": {},  # PR number -> a new title on "GitHub" (the PR changed while the app had it cached)
        "viewed": {},
        "threads": [{"id": "PRRT_bob", "isResolved": False, "isOutdated": False, "path": "c10/xpu/stub.cpp", "line": 11, "originalLine": 11,
                     "startLine": None, "diffSide": "RIGHT", "startDiffSide": None, "subjectType": "LINE", "resolvedBy": None,
                     "comments": {"totalCount": 1, "nodes": [{"databaseId": 501, "author": {"login": "bob"}, "body": "Is `ok` ever false?",
                                                               "createdAt": "2026-11-08T00:00:00Z", "url": "https://github.com/x/1", "diffHunk": "@@ -10,3 +10,4 @@\n+  if (ok)"}]}},
                    {"id": "PRRT_file", "isResolved": False, "isOutdated": False, "path": "docs/xpu.md", "line": None, "originalLine": None,
                     "startLine": None, "diffSide": "RIGHT", "startDiffSide": None, "subjectType": "FILE", "resolvedBy": None,
                     "comments": {"totalCount": 1, "nodes": [{"databaseId": 502, "author": {"login": "carol"}, "body": "Move these notes to the wiki?",
                                                               "createdAt": "2026-11-08T01:00:00Z", "url": None, "diffHunk": None}]}}],
        "reviews": [{"databaseId": 701, "author": {"login": "bob"}, "state": "CHANGES_REQUESTED", "body": "Please add a test.",
                     "submittedAt": "2026-11-08T02:00:00Z", "url": None, "comments": {"totalCount": 1, "nodes": [
                         {"databaseId": 501, "path": "c10/xpu/stub.cpp", "line": 11, "originalLine": 11, "body": "Is `ok` ever false?",
                          "diffHunk": "@@ -10,3 +10,4 @@\n+  if (ok)", "outdated": False, "url": None, "replyTo": None}]}},
                    # a reply to bob's line comment: GitHub makes it a review of its own; shown inside bob's thread
                    {"databaseId": 702, "author": {"login": "alice"}, "state": "COMMENTED", "body": "", "submittedAt": "2026-11-08T03:00:00Z", "url": None,
                     "comments": {"totalCount": 1, "nodes": [{"databaseId": 503, "author": {"login": "alice"}, "createdAt": "2026-11-08T03:00:00Z",
                                                              "path": "c10/xpu/stub.cpp", "line": 11, "originalLine": 11, "body": "Only without a driver.",
                                                              "diffHunk": "@@ -10,3 +10,4 @@\n+  if (ok)", "outdated": False, "url": None, "replyTo": {"databaseId": 501}}]}}],
        "comments": [{"databaseId": 801, "author": {"login": "pytorch-bot"}, "body": "<!-- drci -->\n<details><summary>CI started</summary>2 jobs</details>", "createdAt": "2026-11-07T00:00:00Z", "url": None}],
    }
    keys = {"MarkViewed": "markFileAsViewed", "MarkUnviewed": "unmarkFileAsViewed", "Resolve": "resolveReviewThread", "Unresolve": "unresolveReviewThread",
            "AddReview": "addPullRequestReview", "AddThread": "addPullRequestReviewThread", "Submit": "submitPullRequestReview", "DeleteReview": "deletePullRequestReview",
            "MarkReady": "markPullRequestReadyForReview", "ToDraft": "convertPullRequestToDraft"}

    def graphql(query, variables):
        q = query.lstrip()
        if q.startswith("mutation"):
            name = q.split()[1].split("(")[0]
            posted.append({"mutation": name, "variables": variables})
            if name in ("MarkViewed", "MarkUnviewed"):
                state["viewed"][variables["path"]] = "VIEWED" if name == "MarkViewed" else "UNVIEWED"
            if name in ("Resolve", "Unresolve"):
                for t in state["threads"]:
                    if t["id"] == variables["id"]:
                        t["isResolved"] = name == "Resolve"
                        t["resolvedBy"] = {"login": "e2e-user"} if name == "Resolve" else None
            res = {"AddReview": {"pullRequestReview": {"id": "PRR_e2e"}},
                   "Submit": {"pullRequestReview": {"url": "https://github.com/pytorch/pytorch/pull/101#pullrequestreview-9001"}},
                   "Resolve": {"thread": {"id": variables.get("id"), "isResolved": True}},
                   "Unresolve": {"thread": {"id": variables.get("id"), "isResolved": False}}}.get(name, {"clientMutationId": None, "thread": {"id": "T"}})
            return {keys[name]: res}
        if "query PrFiles" in query:
            return {"repository": {"pullRequest": {"files": {"pageInfo": {"hasNextPage": False},
                    "nodes": [{"path": p, "viewerViewedState": state["viewed"].get(p, "UNVIEWED")} for p in ("c10/xpu/stub.cpp", "docs/xpu.md")]}}}}
        if "query PrThreads" in query:
            return {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": state["threads"]}}}}
        if "query PrConversation" in query:
            return {"repository": {"pullRequest": {"reviews": {"totalCount": len(state["reviews"]), "nodes": state["reviews"]},
                                                    "comments": {"totalCount": len(state["comments"]), "nodes": state["comments"]},
                                                    "pending": {"nodes": []}}}}
        if "query Issue" in query:
            return {"repository": {"nameWithOwner": "pytorch/pytorch", "issue": issue if int(variables["number"]) == 555 else None}}
        if "query ReviewerCandidates" in query:
            people = [{"login": "ezyang", "name": "Edward Z. Yang"}, {"login": "albanD", "name": "Alban D"}]
            return {"repository": {"pullRequest": {"suggestedReviewers": [{"isAuthor": False, "isCommenter": False, "reviewer": people[0]}]},
                                   "assignableUsers": {"nodes": [u for u in people if variables["q"].lower() in (u["login"] + u["name"]).lower()]}}}
        if "query Mentionable" in query:
            people = [{"login": "albanD", "name": "Alban D"}, {"login": "alice", "name": "Alice A."}, {"login": "ezyang", "name": "Edward Z. Yang"}]
            return {"repository": {"mentionableUsers": {"nodes": [u for u in people if variables["q"].lower() in (u["login"] + u["name"]).lower()]}}}
        if "query PrPeople" in query:
            alice = {"login": "alice", "name": "Alice A.", "company": "@intel", "location": "Shanghai", "bio": None, "websiteUrl": None,
                     "twitterUsername": None, "url": "https://github.com/alice", "avatarUrl": None, "createdAt": "2015-01-01T00:00:00Z",
                     "organizations": {"nodes": [{"login": "intel", "name": "Intel"}]}, "followers": {"totalCount": 7}}
            return {"repository": {"pullRequest": {"author": alice, "commits": {"totalCount": 2, "nodes": [
                {"commit": {"authors": {"nodes": [{"name": "Alice A.", "email": "alice@intel.com", "user": alice},
                                                  {"name": "Dan Helper", "email": "dan@example.org", "user": None}]}}},
                {"commit": {"authors": {"nodes": [{"name": "Alice A.", "email": "alice@intel.com", "user": alice}]}}}]}}}}
        if "query InsightsItems" in query:  # Repo Insights: issues / PRs active in a period
            q = variables["q"]
            day = re.search(r"updated:>=(\S+)", q).group(1)
            items = [] if ("label:" in q or "in:title" in q or "repo:pytorch/rfcs" in q) else [
                {"__typename": "Issue", "number": 777, "title": "XPU kernel crash", "url": "https://github.com/pytorch/pytorch/issues/777",
                 "body": "crash", "state": "OPEN", "createdAt": f"{day}T03:00:00Z", "updatedAt": f"{day}T04:00:00Z", "author": {"login": "alice", "company": None},
                 "labels": {"nodes": []}, "assignees": {"nodes": []}, "comments": {"nodes": []}}]
            return {"search": {"issueCount": len(items), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": items}}
        if "query PrDetail" in query:
            n = int(variables["number"])
            src = next((x for x in nodes if x and x["number"] == n), None)
            if src is None:  # not a pull request (e.g. issue #555)
                return {"repository": {"nameWithOwner": "pytorch/pytorch", "isPrivate": False, "pullRequest": None}}
            src = {**src, **({"title": state["titles"][n]} if n in state["titles"] else {})}  # (renamed by a test)
            return {"repository": {"nameWithOwner": src["repository"]["nameWithOwner"], "isPrivate": False, "pullRequest": {
                **src, "id": f"PR_e2e{n}", "body": "This PR **fixes** the lazy stub.\n\n- adds a check\n\nFollows #103 (see `#999` in code), fixes #555. cc @bob, not mail@example.com"
                + (" prio-hint:P0" if n == 201 else ""),  # (the fake agent's priority decision for #201)
               "state": "OPEN", "mergeable": "MERGEABLE",
                "baseRefName": "main", "headRefName": f"fix-{n}",
                "latestReviews": {"nodes": [{"author": {"login": "bob"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-11-08T00:00:00Z", "url": "u"}]},
                "commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
                    {"__typename": "CheckRun", "name": "xpu-test", "conclusion": "FAILURE", "status": "COMPLETED", "detailsUrl": None},
                    {"__typename": "CheckRun", "name": "lint", "conclusion": "SUCCESS", "status": "COMPLETED", "detailsUrl": None}]}}}}]}}}}
        if "repo:" in variables.get("q", ""):
            return {"search": {"issueCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": watched if "repo:pytorch/ao" in variables["q"] else []}}
        return {"rateLimit": {"remaining": 4999}, "search": {"issueCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes},
                "_errors": [{"type": "FORBIDDEN", "message": "Resource protected by organization SAML enforcement."}]}

    ctx.extra["gh_graphql"] = graphql

    def rest(method, path, body=None):
        if method == "GET" and "/check-runs" in path:  # the head commit's checks: one failing GitHub Actions job
            return [{"total_count": 2, "check_runs": [
                {"id": 4242, "name": "xpu-test", "conclusion": "failure", "html_url": "https://github.com/pytorch/pytorch/actions/runs/1/job/4242", "app": {"slug": "github-actions"}},
                {"id": 4243, "name": "lint", "conclusion": "success", "html_url": None, "app": {"slug": "github-actions"}}]}]
        if method == "GET" and "/actions/runs?head_sha=" in path:
            return [{"workflow_runs": [{"id": 900, "name": "pull", "status": "completed", "conclusion": "failure", "html_url": None, "run_number": 1, "run_attempt": 1, "event": "pull_request"},
                                       {"id": 901, "name": "lint", "status": "in_progress", "conclusion": None, "html_url": None, "run_number": 1, "run_attempt": 1, "event": "pull_request"}]}]
        if method == "GET" and "/actions/workflows?" in path:
            return [{"workflows": [{"id": 77, "name": "trunk", "path": ".github/workflows/trunk.yml", "state": "active"}]}]
        if method == "GET" and "/labels?" in path:
            return [{"name": "ciflow/trunk"}, {"name": "ciflow/xpu"}, {"name": "module: xpu"}]
        if "/actions/" in path or re.search(r"/issues/\d+/labels", path):  # CI triggers (recorded)
            posted.append({"method": method, "path": path, "body": body})
            return {}
        if path.endswith("/requested_reviewers"):
            posted.append({"method": method, "path": path, "body": body})
            return {}
        if method == "GET" and "/contents/c10/xpu/stub.cpp?ref=" in path:  # the file at the head: 40 lines around the hunk
            import base64 as _b

            body_lines = [f"// line {n}" for n in range(1, 41)]
            body_lines[9:13] = ["  load();", "  if (ok)", "    call();", " }"]
            text = "\n".join(body_lines) + "\n"
            return {"size": len(text), "sha": "blob1", "content": _b.b64encode(text.encode()).decode()}
        if method == "GET" and path.startswith("users/"):  # a contributor's public profile (not a change: not recorded)
            return {"email": "alice@example.com", "blog": "alice.dev", "hireable": None, "twitter_username": None, "public_repos": 3, "type": "User"}
        posted.append({"method": method, "path": path, "body": body})
        if path.endswith("/replies"):
            cid = int(path.split("/")[-2])
            for t in state["threads"]:
                if t["comments"]["nodes"][0]["databaseId"] == cid:
                    t["comments"]["nodes"].append({"databaseId": 900 + len(posted), "author": {"login": "e2e-user"}, "body": body["body"],
                                                   "createdAt": "2026-11-09T00:00:00Z", "url": None, "diffHunk": None})
                    t["comments"]["totalCount"] += 1
            return {"id": 900 + len(posted), "body": body["body"], "user": {"login": "e2e-user"}}
        if path.endswith("/issues/101/comments"):
            state["comments"].append({"databaseId": 950 + len(posted), "author": {"login": "e2e-user"}, "body": body["body"], "createdAt": "2026-11-09T01:00:00Z", "url": None})
            return {"id": 950 + len(posted), "body": body["body"], "user": {"login": "e2e-user"}}
        if method == "PATCH" and re.search(r"/(pulls|issues)/\d+$", path):  # close / reopen
            if "/issues/555" in path:
                issue["state"] = body["state"].upper()
                issue["stateReason"] = (body.get("state_reason") or "").upper() or None
            return {"state": body["state"], "state_reason": body.get("state_reason")}
        if method == "POST" and path.endswith("/issues/555/comments"):
            issue["comments"]["nodes"].append({"databaseId": 7000 + len(posted), "author": {"login": "e2e-user"}, "body": body["body"], "createdAt": "2026-11-10T00:00:00Z", "url": None})
            return {"id": 7000 + len(posted)}
        raise AssertionError(path)

    ctx.extra["gh_rest"] = rest
    ctx.extra["gh_job_log"] = lambda o, r, jid: ("2026-11-09T10:00:00.0000000Z collecting tests\n" * 20 + "2026-11-09T10:00:01.0000000Z E   AssertionError: XPU result differs from CPU\n"
                                                 "2026-11-09T10:00:01.0000000Z ##[error]Process completed with exit code 1.\n")

    from fastapi.responses import JSONResponse as _J
    from starlette.routing import Route as _R

    async def show_posted(request):
        return _J(posted)

    app.router.routes.insert(0, _R("/fake-github/posted", show_posted))
    ctx.extra["gh_cache_prefetch"] = True  # the PR Review cache prefetches, as with the real gh (only GETs: nothing is posted)
    # Repo Insights' model: a fake that names what it was given (no network)
    ctx.extra["insights_llm"] = lambda task, prompt, as_json: (
        '{"score": 80, "justification": "fake"}' if as_json else f"# Fake {task}\n\n- covered {len(prompt)} characters\n- [issue #777](https://github.com/pytorch/pytorch/issues/777)")

    async def rename_pr(request):  # a change on GitHub (the next fetch of the PR sees it)
        state["titles"][int(request.query_params["n"])] = request.query_params["title"]
        return _J({"ok": True})

    async def forget_memory(request):  # as after a restart: only the local cache (on disk) has the PRs
        from backend.app import github_hub as _gh

        _gh._pr_cache.clear()
        _gh._people_cache.clear()
        return _J({"ok": True})

    app.router.routes.insert(0, _R("/fake-github/rename-pr", rename_pr))
    app.router.routes.insert(0, _R("/fake-github/forget-memory", forget_memory))
    ctx.extra["gh_pr_files"] = lambda o, r, n: [
        {"filename": "c10/xpu/stub.cpp", "status": "modified", "additions": 2, "deletions": 1,
         "patch": "@@ -10,3 +10,4 @@ void init() {\n   load();\n-  call();\n+  if (ok)\n+    call();\n }"},
        {"filename": "docs/xpu.md", "status": "modified", "additions": 2, "deletions": 1, "patch": "@@ -1,3 +1,4 @@\n # XPU\n----\n+XPU notes\n+++i counter\n end"}]


def main() -> None:
    logging.disable(logging.NOTSET)  # conftest silences logging; re-enable for the e2e server log
    reap_stale_runs()
    tmp = Path(tempfile.mkdtemp(prefix=PREFIX))
    (tmp / "OWNER").write_text(str(os.getpid()))
    state = tmp / "fake-agent-state"
    state.mkdir()
    os.environ["FAKE_AGENT_STATE"] = str(state)
    codex_home = tmp / "codex-home"
    codex_home.mkdir()
    os.environ["CODEX_HOME"] = str(codex_home)
    ws = tmp / "wiki"
    make_workspace(ws)
    settings = make_settings(tmp, ws)
    settings.port = int(os.environ.get("WAW_E2E_PORT", "8799"))
    # Repo Insights: some history in the folder (as copied from the earlier jobs)
    ins = settings.state_dir / "insights"
    (ins / "summary" / "pytorch" / "pytorch").mkdir(parents=True, exist_ok=True)
    (ins / "summary" / "pytorch" / "pytorch" / "summary_pytorch_pytorch_0D_2026-11-10_05-35-01.md").write_text(
        "Summary of pytorch/pytorch from 2026-11-10 00-00-00 to 2026-11-10 23-59-59:\n\n# Old day\n\n- [PR #1](https://github.com/pytorch/pytorch/pull/1) did a thing.\n")
    (ins / "highlight" / "pytorch" / "pytorch").mkdir(parents=True, exist_ok=True)
    import json as _json

    (ins / "highlight" / "pytorch" / "pytorch" / "highlight_pytorch_pytorch_2026-11-10_02-05-02.json").write_text(_json.dumps([
        {"File Information": "Highlights of pytorch/pytorch from 2026-11-08_02:05:02 to 2026-11-10_02:05:02"},
        {"title": "Inductor heuristic tuned for CUDA", "url": "https://github.com/pytorch/pytorch/pull/2", "xpu_relevance_score": 88,
         "xpu_relevance_justification": "may regress XPU", "body": "Changes tiling.", "submitter": "bob", "state": "open"}]))
    # PR Development: the fake editor and a small C++/Python source checkout to branch from
    from backend.app.config import DevSettings

    settings.development = DevSettings(code_server=str(ROOT / "tests" / "fakes" / "fake-code-server"), worktrees_dir=tmp / "worktrees")
    src = tmp / "devsrc"
    src.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "e2e@example.com"], ["config", "user.name", "E2E"]):
        subprocess.run(["git", *args], cwd=src, check=True)
    (src / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\nproject(p CXX)\n")
    (src / "pyproject.toml").write_text("[project]\nname = 'p'\n")
    (src / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (src / "doc.md").write_text("# Design notes\n\nThe add function must be commutative.\n\nSee [the code](lib.py) and [details](#details).\n\n## Details\n\nMore text here.\n")
    (src / "tools").mkdir()
    (src / "tools" / "defs.bzl").write_text("def my_macro(name):\n    pass\n")
    (src / "pkg").mkdir()
    (src / "pkg" / "BUILD.bazel").write_text('load("//tools:defs.bzl", "my_macro")\n\nmy_macro(\n    name = "lib",\n)\n')
    import base64 as _b64

    (src / "logo.png").write_bytes(_b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    (src / "model.onnx").write_bytes(b"\0\1\2\3")
    (src / "ci.yaml").write_text("jobs:\n  build:\n    runs-on: ubuntu\n  test:\n    needs: build\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)
    # "Your pull requests" of acme/demo (a repository the e2e test makes; its PR head is refs/pull/5/head)
    from backend.app.development import service as dev_service

    pr_states: dict[int, str] = {}  # closed / reopened through the app

    async def my_prs(repo, state="open"):
        if repo.lower() != "acme/demo":
            return []
        return [{"number": 5, "title": "Demo fix", "url": "https://github.com/acme/demo/pull/5", "isDraft": False, "state": pr_states.get(5, "OPEN"),
                 "headRefName": "demo-fix", "baseRefName": "main", "updatedAt": "2026-11-10T00:00:00Z", "reviewDecision": None, "ci": "SUCCESS"},
                {"number": 6, "title": "Demo draft", "url": "https://github.com/acme/demo/pull/6", "isDraft": True, "state": pr_states.get(6, "OPEN"),
                 "headRefName": "demo-draft", "baseRefName": "main", "updatedAt": "2026-11-09T00:00:00Z", "reviewDecision": None, "ci": None}]

    async def pr_meta(repo, number):
        return {"headRefName": {5: "demo-fix", 6: "demo-draft"}.get(number, f"pr{number}"), "head_repo": repo, "baseRefName": "main",
                "isCrossRepository": False, "maintainerCanModify": True, "title": f"Demo PR {number}", "state": pr_states.get(number, "OPEN")}

    async def set_state(repo, number, action, comment):
        pr_states[number] = "CLOSED" if action == "close" else "OPEN"
    dev_service.list_my_prs = my_prs
    dev_service.fetch_pr_meta = pr_meta
    dev_service.set_pr_state = set_state
    Path(f"/tmp/waw-e2e-devsrc-{settings.port}").write_text(str(src))  # per port: runs side by side do not mix
    settings.frontend_dist = ROOT / "frontend" / "dist"
    # "Sign in with GitHub" against a fake GitHub served by this same server (routes below)
    base = f"http://127.0.0.1:{settings.port}/fake-github"
    # GitHub starts unconfigured (no client id): the first click on the login page sets it up via the app manifest
    # flow. The server's owner is e2e-user (normally taken from `gh`).
    settings.github_allowed_users = ["e2e-user"]
    settings.github_oauth_base, settings.github_api_base = base, base
    g = f"http://127.0.0.1:{settings.port}/fake-google"
    settings.google_client_id, settings.google_client_secret = "e2e-google", "e2e-google-secret"
    settings.google_allowed_emails = ["e2e@example.com"]
    settings.google_auth_url, settings.google_token_url, settings.google_userinfo_url = f"{g}/auth", f"{g}/token", f"{g}/userinfo"
    os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    app = create_app(settings)
    add_fake_github(app)
    add_fake_review_queue(app)
    app.state.ctx.extra["insights_remote"] = {"pytorch/pytorch": str(src)}  # Repo Insights commits: the e2e checkout stands in for GitHub
    print(f"E2E workspace: {ws}", flush=True)
    (tmp / "WORKSPACE").write_text(str(ws))
    Path(f"/tmp/waw-e2e-workspace-{settings.port}").write_text(str(ws))
    # uvicorn shuts down gracefully on SIGTERM, then re-raises the signal with the handler that was installed
    # before it started. With the default handler that kills the process before `finally` runs; this one exits
    # normally, so the cleanup below always happens.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="info")
    finally:
        # test isolation: stop this run's own tmux server (all terminals the tests created) and drop the temp folder
        try:
            app.state.ctx.terminals.tmux.kill_server()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":  # guard: the watcher child process re-imports this module
    main()

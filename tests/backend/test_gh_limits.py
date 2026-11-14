"""GitHub API rate-limit management (gh_limits, gh_queue): GitHub's headers, throttling by priority, waiting and
retrying, shared quotas, restarts, unclear mutations, auth failures, clock skew — on a fake clock (no real waiting,
no real GitHub)."""

from __future__ import annotations

import asyncio
import email.utils
import json
import threading

import pytest

from backend.app import gh_limits, gh_queue
from backend.app.errors import ApiError
from backend.app.gh_limits import BACKGROUND, CRITICAL, INTERACTIVE, Bucket, GhResult, Limiter, priority

T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def lim(shared=None, clock=None):
    c = clock or Clock()
    g = Limiter(shared=shared, clock=c, sleep=c.sleep)
    g.checked = c.t + 10**9  # no `gh api rate_limit` refresh unless a test asks
    return g, c


def hdr(remaining, limit=5000, reset_in=1800, resource="core", clock=None, date_skew=0.0, **extra):
    now = clock.t if clock else T0
    h = {"x-ratelimit-limit": str(limit), "x-ratelimit-remaining": str(remaining), "x-ratelimit-used": str(limit - remaining),
         "x-ratelimit-reset": str(int(now + date_skew + reset_in)), "x-ratelimit-resource": resource,
         "date": email.utils.formatdate(now + date_skew, usegmt=True)}
    h.update(extra)
    return h


def ok(h):
    return GhResult(0, "{}", "", 200, [h])


def test_normal_operation_with_quota_and_headers_are_authoritative():
    g, c = lim()
    calls = []
    res = g.call(lambda: (calls.append(1), ok(hdr(4321, clock=c)))[1], "core", endpoint="api repos/x")
    assert res.rc == 0 and calls == [1] and c.slept == []
    st = g.state()
    assert st["state"] == "RUNNING" and st["buckets"]["core"]["remaining"] == 4321 and st["buckets"]["core"]["limit"] == 5000
    assert st["metrics"]["requests"] == 1 and st["metrics"]["by_resource"] == {"core": 1}
    # a non-default limit is taken as GitHub states it (never assumed to be 5000)
    g.observe(hdr(900, limit=1000, resource="search", clock=c))
    assert g.state()["buckets"]["search"]["limit"] == 1000


def test_near_the_warning_threshold_background_is_spread_out_people_are_not():
    g, c = lim()
    g.observe(hdr(800, clock=c, reset_in=1000))  # 16% left: below warn (20%)
    with priority(BACKGROUND):
        g.acquire("core")
        g.acquire("core")  # the second one waits for its turn
    assert c.slept and 0.5 <= sum(c.slept) <= 30
    assert g.state()["state"] == "THROTTLED"
    c.slept.clear()
    with priority(INTERACTIVE):
        for _ in range(5):
            g.acquire("core")
    assert c.slept == []


def test_below_the_suspend_threshold_background_waits_people_keep_working_reserve_is_for_mutations():
    g, c = lim()
    g.observe(hdr(200, clock=c, reset_in=600))  # 4%: background suspended
    with priority(BACKGROUND):
        g.acquire("core")
    assert 600 <= c.t - T0 <= 640  # waited for the reset
    g2, c2 = lim()
    g2.observe(hdr(200, clock=c2, reset_in=600))
    with priority(BACKGROUND, wait=False), pytest.raises(ApiError) as e:  # speculative work: told at once instead
        g2.acquire("core")
    assert e.value.code == "GH_RATE_LIMITED" and e.value.details["retry_after"] >= 600
    with priority(INTERACTIVE):
        g2.acquire("core")  # 200 > reserve (100): fine
    g2.observe(hdr(60, clock=c2, reset_in=600))
    with priority(INTERACTIVE), pytest.raises(ApiError):  # the last 2% are kept…
        g2.acquire("core")
    g2.acquire("core", mutation=True)  # …for your own actions


def test_primary_exhaustion_waits_for_the_reset_then_resumes():
    g, c = lim()
    answers = [GhResult(1, '{"message":"API rate limit exceeded"}', "gh: API rate limit exceeded (HTTP 403)", 403, [hdr(0, clock=c, reset_in=1200)]),
               ok(hdr(4999, clock=c))]
    n = []

    def fn():
        n.append(1)
        a = answers.pop(0)
        if not answers:  # the second answer: after the reset
            a.headers = [hdr(4999, clock=c)]
        return a
    with priority(BACKGROUND):
        res = g.call(fn, "core")
    assert res.rc == 0 and len(n) == 2 and 1200 <= c.t - T0 <= 1300
    assert g.metrics["retries"] == 1 and g.metrics["blocked_seconds"] >= 1200
    assert g.state()["buckets"]["core"]["remaining"] == 4999


def test_primary_exhaustion_a_person_gets_a_clear_429_with_the_reset_time():
    g, c = lim()
    res = g.call(lambda: GhResult(1, "", "gh: API rate limit exceeded (HTTP 403)", 403, [hdr(0, clock=c, reset_in=1800)]), "graphql")
    with pytest.raises(ApiError) as e:
        gh_limits.raise_for(res)
    assert e.value.status == 429 and e.value.code == "GH_RATE_LIMITED" and "resets at" in e.value.message
    with pytest.raises(ApiError) as e2:  # and the next request does not even go to GitHub
        g.acquire("core")
    assert e2.value.code == "GH_RATE_LIMITED" and c.slept == []


def test_secondary_limit_waits_at_least_a_minute_with_backoff_and_retry_after_is_respected():
    g, c = lim()
    sec = lambda: GhResult(1, "", "You have exceeded a secondary rate limit (HTTP 403)", 403, [{}])  # noqa: E731
    seq = [sec(), sec(), ok({})]
    with priority(BACKGROUND):
        res = g.call(lambda: seq.pop(0), "core")
    assert res.rc == 0
    waits = [round(x) for x in c.slept]
    assert sum(waits) >= 60 + 120  # ≥ 60 s, doubling
    g2, c2 = lim()
    seq2 = [GhResult(1, "", "HTTP 429", 429, [{"retry-after": "7"}]), ok({})]
    with priority(BACKGROUND):
        g2.call(lambda: seq2.pop(0), "core")
    assert 7 <= sum(c2.slept) <= 7 * 1.25  # Retry-After (with jitter only upwards)
    # a person: a short stated Retry-After is waited for, no guess is
    g3, c3 = lim()
    seq3 = [GhResult(1, "", "HTTP 429", 429, [{"retry-after": "5"}]), ok({})]
    assert g3.call(lambda: seq3.pop(0), "core").rc == 0 and 5 <= sum(c3.slept) < 10
    g4, c4 = lim()
    r4 = g4.call(sec, "core")
    assert r4.rc == 1 and c4.slept == []  # unknown delay: answered at once (the limit is remembered)
    assert g4.state()["state"] == "WAITING_FOR_RATE_LIMIT" and g4.state()["blocked"]["core"]["until"] > c4.t


def test_reset_passed_counts_as_a_new_window():
    g, c = lim()
    g.observe(hdr(0, clock=c, reset_in=100))
    c.t += 101
    with priority(BACKGROUND):
        g.acquire("core")
    assert c.slept == [] and g.state()["buckets"]["core"]["remaining"] == 4999


def test_resources_are_limited_separately():
    g, c = lim()
    g.observe(hdr(0, clock=c, resource="core"))
    g.observe(hdr(4000, clock=c, resource="graphql"))
    g.acquire("graphql")
    with pytest.raises(ApiError):
        g.acquire("core")
    assert gh_limits.resource_of(["api", "graphql", "-f", "query=x"]) == "graphql"
    assert gh_limits.resource_of(["api", "--paginate", "repos/a/b/pulls/1/files"]) == "core"
    assert gh_limits.resource_of(["api", "search/issues?q=x"]) == "search"
    assert gh_limits.resource_of(["pr", "list"]) == "graphql"


def test_processes_sharing_one_login_coordinate(tmp_path):
    shared = tmp_path / "github-rate.json"
    c = Clock()
    a, _ = lim(shared, c)
    b, _ = lim(shared, c)
    a.observe(hdr(0, clock=c, reset_in=1500))  # one process sees the quota used up …
    with pytest.raises(ApiError):
        b.acquire("core")  # … the other one does not spend more
    a.block("graphql", 120, "GitHub's secondary rate limit")
    with priority(BACKGROUND):
        b.acquire("graphql")
    assert 120 <= c.t - T0 < 130
    assert json.loads(shared.read_text())["buckets"]


def test_threads_sharing_the_limiter_and_identical_reads_in_flight_run_once():
    g = Limiter()
    gate = threading.Event()
    calls = []

    def fn():
        calls.append(1)
        gate.wait(2)
        return ok({})
    out = []
    ts = [threading.Thread(target=lambda: out.append(g.call(fn, "core", dedupe="same"))) for _ in range(4)]
    for t in ts:
        t.start()
    while not calls:
        pass
    gate.set()
    for t in ts:
        t.join()
    assert len(calls) == 1 and len(out) == 4 and g.metrics["deduped"] == 3


def test_transient_errors_are_retried_for_reads():
    g, c = lim()
    seq = [GhResult(1, "", "HTTP 502: Bad Gateway", 502), ok({})]
    assert g.call(lambda: seq.pop(0), "core").rc == 0 and len(c.slept) == 1
    g2, _ = lim()
    seq2 = [GhResult(1, "", "dial tcp: could not resolve host api.github.com", None)] * 5
    assert g2.call(lambda: seq2.pop(0), "core").rc == 1  # bounded: gives up with the error


def test_a_mutation_with_an_unclear_outcome_is_reconciled_never_sent_twice():
    g, _ = lim()
    sent = []

    def post():
        sent.append(1)
        return GhResult(124, "", "timed out after 90 s")
    res = g.call(post, "core", mutation=True, reconcile=lambda: {"id": 7})
    assert res.rc == 0 and json.loads(res.out) == {"id": 7} and sent == [1]  # GitHub shows it was done
    sent.clear()
    with pytest.raises(ApiError) as e:
        g.call(post, "core", mutation=True, reconcile=lambda: None)
    assert e.value.code == "GH_UNCERTAIN" and sent == [1]
    sent.clear()
    with pytest.raises(ApiError):
        g.call(post, "core", mutation=True)  # no way to check: not sent again either
    assert sent == [1]
    # a rate-limited mutation was rejected by GitHub (not done): sending it again is safe
    seq = [GhResult(1, "", "HTTP 429", 429, [{"retry-after": "3"}]), ok({})]
    assert g.call(lambda: seq.pop(0), "core", mutation=True).rc == 0


def test_authentication_failure_is_not_a_rate_limit():
    g, c = lim()
    n = []
    res = g.call(lambda: (n.append(1), GhResult(1, '{"message":"Bad credentials"}', "HTTP 401: Bad credentials", 401))[1], "core")
    assert len(n) == 1 and c.slept == [] and g.state()["state"] == "FAILED"
    with pytest.raises(ApiError) as e:
        gh_limits.raise_for(res)
    assert e.value.code == "GH_AUTH"
    # and a 403 for permissions (no rate words, no rate headers) is not one either
    assert gh_limits.classify(403, "Resource not accessible by integration", {}) == "other"
    # a PR titled "rate limit …" in a partial GraphQL answer is data, not a limit
    partial = GhResult(1, json.dumps({"data": {"x": {"title": "Fix rate limit handling, timeout"}}, "errors": [{"type": "FORBIDDEN", "message": "SSO"}]}), "", 200)
    assert g.call(lambda: partial, "graphql").rc == 1 and c.slept == []
    assert gh_limits.classify(200, gh_limits.error_text(partial), {}) == "other"
    rl = GhResult(1, json.dumps({"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}), "", 200)
    assert gh_limits.classify(200, gh_limits.error_text(rl), {}) == "primary"


def test_clock_skew_and_missing_headers():
    g, c = lim()
    g.observe(hdr(0, clock=c, reset_in=600, date_skew=210))  # GitHub's clock 3.5 min ahead of ours
    assert round(g.skew) == 210 and abs(g.buckets[g.key("core")].reset - (c.t + 600)) < 2  # reset in our time
    with priority(BACKGROUND):
        g.acquire("core")
    assert 600 <= c.t - T0 <= 610  # not 810
    g2, c2 = lim()
    g2.observe({"x-ratelimit-remaining": "oops"})  # junk: ignored
    g2.observe({})
    g2.acquire("core")  # nothing known: allowed
    g2.checked = c2.t - 1000
    asked = []
    g2.refresher = lambda: (asked.append(1), {"resources": {"core": {"limit": 5000, "remaining": 0, "reset": int(c2.t + 50)}}})[1]
    with priority(BACKGROUND):
        g2.acquire("core")  # old state: checked with `gh api rate_limit` (free) first
    assert asked and c2.t - T0 >= 50


def test_split_included_output_of_paginated_calls():
    out = ("HTTP/2.0 200 OK\nX-Ratelimit-Remaining: 10\nX-Ratelimit-Resource: core\n\n{\"a\":1}\n"
           "HTTP/2.0 200 OK\nX-Ratelimit-Remaining: 9\nX-Ratelimit-Resource: core\n\n{\"a\":2}\n")
    heads, body = gh_limits.split_included(out)
    assert [s for s, _ in heads] == [200, 200] and heads[1][1]["x-ratelimit-remaining"] == "9"
    assert body.split() == ['{"a":1}', '{"a":2}']


async def test_gh_queue_resumes_after_restart_bounded(running_app):
    if True:
        h = await running_app()
        seen = []
        gh_queue.register("test.kind", lambda ctx, op: seen.append((op["id"], op["params"], op["progress"])))
        gh_queue.add(h.ctx, "op1", "test.kind", {"x": 1})
        gh_queue.progress(h.ctx, "op1", done=["2026-11-01"])
        assert gh_queue.resume_all(h.ctx) == ["op1"]
        assert seen == [("op1", {"x": 1}, {"done": ["2026-11-01"]})]
        for _ in range(gh_queue.MAX_RESUMES):
            gh_queue.resume_all(h.ctx)
        assert gh_queue.pending(h.ctx) == []  # dropped after MAX_RESUMES: no endless loop


async def test_insights_report_interrupted_by_a_restart_resumes_and_finishes(running_app, tmp_path):
    from backend.app.insights import pipeline as pl
    from backend.app.insights import settings as st

    h = await running_app()
    h.ctx.extra["insights_llm"] = lambda task, prompt, as_json: f"[{task}]"
    h.ctx.extra["gh_graphql"] = lambda q, v: {"search": {"issueCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []}}
    st.save(h.ctx, {"data_dir": str(tmp_path / "ins")})
    # the state a restart leaves: the report's file says running, the queue still has it, no job lives
    meta = {"id": "rep1", "job_id": "rep1", "repo": "acme/proj", "since": "2026-11-02", "until": "2026-11-03", "label": "x", "kind": "dates",
            "sources": {"issues": True}, "focus": "", "preset": None, "status": "running", "created_at": "2026-11-04T00:00:00+00:00"}
    pl._meta_write(h.ctx, meta)
    gh_queue.add(h.ctx, "rep1", "insights.report", {"repo": "acme/proj"})
    gh_queue.register("insights.report", pl.resume)
    assert gh_queue.resume_all(h.ctx) == ["rep1"]
    for _ in range(100):
        if pl.report(h.ctx, "acme/proj", "rep1")["status"] != "running" and not gh_queue.pending(h.ctx):
            break
        await asyncio.sleep(0.05)
    r = pl.report(h.ctx, "acme/proj", "rep1")
    assert r["status"] == "done" and r["text"].startswith("Report of acme/proj") and gh_queue.pending(h.ctx) == []


async def test_insights_jobs_run_at_background_priority_and_show_the_wait(running_app, tmp_path):
    from backend.app.insights import pipeline as pl

    h = await running_app()
    seen = {}

    async def work():
        seen["prio"] = gh_limits._prio.get()
        cb = gh_limits._on_wait.get()
        cb({"state": "WAITING_FOR_RATE_LIMIT", "resource": "graphql", "why": "the graphql quota is used up", "until": T0})
        seen["step"] = j["step"]
        seen["state"] = j["state"]
        cb(None)
        return {}
    j = pl.new_job("daily", "acme/proj")
    pl.start(h.ctx, j, work())
    for _ in range(50):
        if j["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert seen["prio"] == (BACKGROUND, True) and seen["state"] == "WAITING_FOR_RATE_LIMIT"
    assert "waiting for GitHub: the graphql quota is used up; goes on by itself at" in seen["step"]
    assert j["status"] == "done"


async def test_rate_endpoint_and_pr_files_are_not_fetched_again_for_the_same_head(running_app):
    from backend.app import github_hub
    from test_github_hub import as_github, fake_pr_detail

    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = fake_pr_detail([], {})
    n = []
    h.ctx.extra["gh_pr_files"] = lambda o, r, num: (n.append(1), [{"filename": "a.cpp", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-x\n+y"}])[1]
    base = "/api/v1/github/pr/pytorch/pytorch/7"
    assert (await h.api.get(base)).status_code == 200
    assert (await h.api.get(base, params={"refresh": True})).status_code == 200  # the same head commit: its files are known
    assert n == [1]
    r = (await h.api.get("/api/v1/github/rate")).json()
    assert r["state"] in ("RUNNING", "THROTTLED", "RESUMING") and "metrics" in r and "queued" in r
    assert r["metrics"]["cache_hits"] >= 1


async def test_development_pr_create_never_makes_a_second_pr(running_app, monkeypatch):
    """`gh pr create` answering "already exists" (an earlier, unclear attempt made it): the existing PR is taken."""
    rc_out = {"n": 0}

    async def runner():
        rc_out["n"] += 1
        return 1, "", 'a pull request for branch "b" into branch "main" already exists:\nhttps://github.com/acme/proj/pull/42'
    rc, _out, _err = await gh_limits.run_with(runner, ["pr", "create"], mutation=True)
    assert rc == 1 and rc_out["n"] == 1  # not retried (not a rate limit); the caller adopts #42 (see service.create_pr)

    async def timeout():
        rc_out["n"] += 1
        return 124, "", "timed out"

    async def reconcile():
        return 0, "https://github.com/acme/proj/pull/43", ""
    rc_out["n"] = 0
    assert (await gh_limits.run_with(timeout, ["pr", "create"], mutation=True, reconcile=reconcile))[1].endswith("/43") and rc_out["n"] == 1


def test_priorities_names():
    assert (CRITICAL, INTERACTIVE, BACKGROUND) == (0, 1, 2) and isinstance(Bucket(), Bucket)


async def test_a_resumed_daily_run_skips_the_days_it_finished(running_app, monkeypatch):
    from backend.app.insights import pipeline as pl

    h = await running_app()
    made = []

    async def fake_day(ctx, repo, d, j=None, force=False):
        made.append(d)
        return ""
    monkeypatch.setattr(pl, "daily_summary", fake_day)
    gh_queue.add(h.ctx, "run1", "insights.run", {"repo": "acme/proj", "kind": "daily", "since": "2026-11-01", "until": "2026-11-03"})
    gh_queue.progress(h.ctx, "run1", done=["2026-11-01"])
    j = pl.new_job("daily", "acme/proj", jid="run1")
    try:
        await pl.run_days(h.ctx, j, "acme/proj", "daily", "2026-11-01", "2026-11-03")
    finally:
        j["status"] = "done"  # (run directly, not by start(): the job list must not keep it as running)
        gh_queue.done(h.ctx, "zzz")
    assert made == ["2026-11-02", "2026-11-03"]
    assert next(o for o in gh_queue.pending(h.ctx) if o["id"] == "run1")["progress"]["done"] == ["2026-11-01", "2026-11-02", "2026-11-03"]


def test_state_a_used_up_quota_is_waiting_with_its_reset():
    g, c = lim()
    g.observe(hdr(0, clock=c, reset_in=900, resource="graphql"))
    st = g.state()
    assert st["state"] == "WAITING_FOR_RATE_LIMIT" and st["next_retry_at"]


def test_background_work_is_paced_to_its_share_of_the_hourly_quota_people_are_not():
    g, c = lim()
    g.observe(hdr(5000, clock=c, reset_in=3600, resource="graphql"))
    with priority(BACKGROUND):
        for _ in range(600):  # a burst of 500 (10%), then 2000/h (40%): ~0.56 per second
            g.acquire("graphql")
    assert 150 <= c.t - T0 <= 200
    t = c.t
    with priority(INTERACTIVE):
        for _ in range(100):
            g.acquire("graphql")
    assert c.t == t  # your pages do not wait for the background pace
    assert g.state()["background_budget"]["graphql"]["per_hour"] == 2000


def test_the_real_cost_github_reports_is_charged_to_the_background_budget():
    g, c = lim()
    g.observe(hdr(4000, clock=c, reset_in=3000, resource="graphql"))
    tokens0 = g._budget("graphql")[0]
    with priority(BACKGROUND):
        g.call(lambda: ok(hdr(3986, clock=c, reset_in=3000, resource="graphql")), "graphql")  # a search page cost 14 points
    assert round(tokens0 - g._budget("graphql")[0]) == 14


def test_speculative_background_work_may_wait_a_few_seconds_for_its_pace():
    g, c = lim()
    g.observe(hdr(5000, clock=c, resource="core"))
    g.bg["core"] = (0.0, c.t)
    with priority(BACKGROUND, wait=False):
        g.acquire("core")  # ~2 s: waited
    assert 1 <= c.t - T0 <= 3
    g.bg["core"] = (-50.0, c.t)
    with priority(BACKGROUND, wait=False), pytest.raises(ApiError):
        g.acquire("core")  # far behind: stops instead

"""Sign in with GitHub: OAuth web flow against a fake GitHub (httpx MockTransport)."""

from __future__ import annotations

import dataclasses
from urllib.parse import parse_qs, urlsplit

import httpx

from conftest import TOKEN


def _fake_github(users_by_code: dict[str, str], seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.url.path == "/login/oauth/access_token":
            form = parse_qs(req.content.decode())
            code = form["code"][0]
            if form["client_id"] != ["cid"] or form["client_secret"] != ["csecret"] or code not in users_by_code:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": f"tok-{code}", "token_type": "bearer"})
        if req.url.path == "/user":
            code = (req.headers.get("authorization") or "").removeprefix("Bearer tok-")
            return httpx.Response(200, json={"login": users_by_code[code], "id": 1}) if code in users_by_code else httpx.Response(401)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def _start(api) -> str:
    r = await api.c.get("/api/v1/auth/github/start")
    assert r.status_code == 302, r.text
    loc = urlsplit(r.headers["location"])
    assert loc.netloc == "github.com" and loc.path == "/login/oauth/authorize"
    q = parse_qs(loc.query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["http://testserver/api/v1/auth/github/callback"]  # the address the browser used
    assert "waw_gh_state" in r.headers["set-cookie"] and "samesite=lax" in r.headers["set-cookie"].lower()
    return q["state"][0]


async def test_github_sign_in_disabled_by_default(running_app):
    h = await running_app(login=False)
    assert (await h.api.c.get("/api/v1/auth/options")).json() == {"token": True, "github": False}
    r = await h.api.c.get("/api/v1/auth/github/start")
    assert r.status_code == 303 and r.headers["location"] == "/?login_error=github_disabled"


async def test_github_sign_in_flow_allowed_users_only(running_app, settings):
    s = dataclasses.replace(settings, github_client_id="cid", github_client_secret="csecret", github_allowed_users=["EikanWang"])
    seen: list = []
    h = await running_app(s, login=False)
    h.ctx.extra["github_transport"] = _fake_github({"good": "eikanwang", "stranger": "someone-else"}, seen)
    api = h.api
    assert (await api.c.get("/api/v1/auth/options")).json()["github"] is True
    assert (await api.c.get("/api/v1/bootstrap")).status_code == 401
    # forged / missing state: refused before GitHub is even contacted
    state = await _start(api)
    r = await api.c.get(f"/api/v1/auth/github/callback?code=good&state={state}x")
    assert r.headers["location"] == "/?login_error=github_state" and seen == []
    # cancelled on GitHub
    state = await _start(api)
    r = await api.c.get(f"/api/v1/auth/github/callback?error=access_denied&state={state}")
    assert r.headers["location"] == "/?login_error=github_cancelled"
    # a GitHub account that is not allowed: no login
    state = await _start(api)
    r = await api.c.get(f"/api/v1/auth/github/callback?code=stranger&state={state}")
    assert r.headers["location"] == "/?login_error=github_not_allowed"
    assert (await api.c.get("/api/v1/bootstrap")).status_code == 401
    # a bad code
    state = await _start(api)
    r = await api.c.get(f"/api/v1/auth/github/callback?code=nope&state={state}")
    assert r.headers["location"] == "/?login_error=github_exchange"
    # the allowed account (login compared case-insensitively): a normal session
    state = await _start(api)
    r = await api.c.get(f"/api/v1/auth/github/callback?code=good&state={state}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    b = await api.c.get("/api/v1/bootstrap")
    assert b.status_code == 200 and b.json()["identity"] == "github:eikanwang"
    # the token was sent to /user only, and the state cookie is single-use
    assert ("GET", "/user", "Bearer tok-good") in seen
    r = await api.c.get(f"/api/v1/auth/github/callback?code=good&state={state}")
    assert r.headers["location"] == "/?login_error=github_state"


async def test_github_unreachable_is_reported(running_app, settings):
    s = dataclasses.replace(settings, github_client_id="cid", github_client_secret="csecret", github_allowed_users=["EikanWang"])
    h = await running_app(s, login=False)

    def boom(req):
        raise httpx.ConnectError("no route to github.com")

    h.ctx.extra["github_transport"] = httpx.MockTransport(boom)
    state = await _start(h.api)
    r = await h.api.c.get(f"/api/v1/auth/github/callback?code=good&state={state}")
    assert r.headers["location"] == "/?login_error=github_unreachable"


async def test_token_login_still_works_and_records_identity(running_app, settings):
    h = await running_app(login=False)
    r = await h.api.login(TOKEN)
    assert r.status_code == 200
    assert (await h.api.c.get("/api/v1/bootstrap")).json()["identity"] == "token"

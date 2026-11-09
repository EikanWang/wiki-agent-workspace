"""Social sign-in (GitHub, Google): OAuth / OpenID Connect web flows against fakes (httpx MockTransport)."""

from __future__ import annotations

import dataclasses
from urllib.parse import parse_qs, urlsplit

import httpx

import pytest

from conftest import TOKEN


@pytest.fixture(autouse=True)
def _no_host_gh(monkeypatch):
    """Never ask the host's `gh` who owns the server; tests that need an owner set one."""
    from backend.app import social_login

    monkeypatch.setattr(social_login, "server_owner", lambda s: s.github_allowed_users[0] if s.github_allowed_users else "")


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
    assert (await h.api.c.get("/api/v1/auth/options")).json() == {"token": True, "github": False, "github_setup": False, "google": False}
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


def _fake_google(people: dict[str, dict], seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.host, req.url.path))
        if req.url.path == "/token":
            form = parse_qs(req.content.decode())
            code = form["code"][0]
            ok = form["client_id"] == ["gid"] and form["client_secret"] == ["gsecret"] and form["grant_type"] == ["authorization_code"]
            return httpx.Response(200, json={"access_token": f"g-{code}", "id_token": "x"}) if ok and code in people else httpx.Response(400, json={"error": "invalid_grant"})
        if req.url.path == "/v1/userinfo":
            code = (req.headers.get("authorization") or "").removeprefix("Bearer g-")
            return httpx.Response(200, json={"sub": "1", **people[code]}) if code in people else httpx.Response(401)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_google_sign_in_verified_allowed_emails_only(running_app, settings):
    s = dataclasses.replace(settings, google_client_id="gid", google_client_secret="gsecret", google_allowed_emails=["me@example.com"])
    seen: list = []
    h = await running_app(s, login=False)
    h.ctx.extra["google_transport"] = _fake_google({
        "me": {"email": "Me@Example.com", "email_verified": True},
        "unverified": {"email": "me@example.com", "email_verified": False},
        "other": {"email": "other@example.com", "email_verified": True},
    }, seen)
    api = h.api
    assert (await api.c.get("/api/v1/auth/options")).json() == {"token": True, "github": False, "github_setup": False, "google": True}

    async def start() -> str:
        r = await api.c.get("/api/v1/auth/google/start")
        assert r.status_code == 302
        loc = urlsplit(r.headers["location"])
        q = parse_qs(loc.query)
        assert loc.netloc == "accounts.google.com" and q["scope"] == ["openid email"] and q["response_type"] == ["code"]
        assert q["redirect_uri"] == ["http://testserver/api/v1/auth/google/callback"]
        return q["state"][0]

    for code, expected in [("other", "google_not_allowed"), ("unverified", "google_unverified"), ("nope", "google_exchange")]:
        st = await start()
        r = await api.c.get(f"/api/v1/auth/google/callback?code={code}&state={st}")
        assert r.headers["location"] == f"/?login_error={expected}", code
    st = await start()
    assert (await api.c.get(f"/api/v1/auth/google/callback?code=me&state={st}x")).headers["location"] == "/?login_error=google_state"
    assert (await api.c.get("/api/v1/bootstrap")).status_code == 401
    st = await start()
    r = await api.c.get(f"/api/v1/auth/google/callback?code=me&state={st}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    b = await api.c.get("/api/v1/bootstrap")
    assert b.status_code == 200 and b.json()["identity"] == "google:me@example.com"


async def test_one_click_github_setup_via_app_manifest(running_app, settings):
    import json
    import stat

    seen: list = []

    def gh(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        if req.url.path == "/app-manifests/mcode/conversions" and req.method == "POST":
            return httpx.Response(201, json={"id": 7, "slug": "wiki-agent-workspace-abc", "client_id": "Iv23made", "client_secret": "sek",
                                             "owner": {"login": "EikanWang"}, "html_url": "https://github.com/apps/wiki-agent-workspace-abc"})
        return httpx.Response(404, json={"message": "Not Found"})

    h = await running_app()  # signed in with the token
    h.ctx.extra["github_transport"] = httpx.MockTransport(gh)
    api = h.api
    assert (await api.c.get("/api/v1/auth/options")).json()["github"] is False
    info = (await api.c.get("/api/v1/auth/github/setup")).json()
    assert info["enabled"] is False and info["action"].startswith("https://github.com/settings/apps/new?state=")
    m = json.loads(info["manifest"])
    assert m["callback_urls"] == ["http://testserver/api/v1/auth/github/callback"] and m["default_permissions"] == {} and m["public"] is True
    assert m["redirect_url"] == "http://testserver/api/v1/auth/github/setup/callback"
    state = info["action"].split("state=")[1]
    # a forged return (wrong state) changes nothing
    r = await api.c.get(f"/api/v1/auth/github/setup/callback?code=mcode&state={state}x")
    assert r.headers["location"] == "/?github_setup=state" and seen == []
    info = (await api.c.get("/api/v1/auth/github/setup")).json()
    state = info["action"].split("state=")[1]
    r = await api.c.get(f"/api/v1/auth/github/setup/callback?code=mcode&state={state}")
    assert r.headers["location"] == "/?github_setup=ok"
    saved = settings.state_dir / "github-signin.json"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert json.loads(saved.read_text())["allowed_users"] == ["EikanWang"]
    # live: the login page offers it now, and sign-in goes to GitHub with the new app
    assert (await api.c.get("/api/v1/auth/options")).json()["github"] is True
    loc = (await api.c.get("/api/v1/auth/github/start")).headers["location"]
    assert "client_id=Iv23made" in loc
    # the state is single-use
    assert (await api.c.get(f"/api/v1/auth/github/setup/callback?code=mcode&state={state}")).headers["location"] == "/?github_setup=state"
    # and it survives a restart
    await h.stop()
    h2 = await running_app(login=False)
    assert (await h2.api.c.get("/api/v1/auth/options")).json()["github"] is True


async def test_github_setup_requires_sign_in(running_app):
    h = await running_app(login=False)
    assert (await h.api.c.get("/api/v1/auth/github/setup")).status_code == 401


async def test_login_page_github_button_sets_up_for_the_server_owner_only(running_app, settings, monkeypatch):
    """Before GitHub sign-in exists, the login page's button creates the app on GitHub; only the server owner's
    GitHub account (here from `gh`) can complete it, then sign-in continues right away."""
    import json
    from backend.app import social_login

    monkeypatch.setattr(social_login, "server_owner", lambda s: "EikanWang")
    creator = {"login": "intruder"}

    def gh(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/app-manifests/mcode/conversions":
            return httpx.Response(201, json={"slug": "waw-x", "client_id": "Iv23x", "client_secret": "sx", "owner": creator, "html_url": "https://github.com/apps/waw-x"})
        return httpx.Response(404)

    h = await running_app(login=False)
    h.ctx.extra["github_transport"] = httpx.MockTransport(gh)
    api = h.api
    assert (await api.c.get("/api/v1/auth/options")).json() == {"token": True, "github": True, "github_setup": True, "google": False}

    async def begin() -> str:
        r = await api.c.get("/api/v1/auth/github/setup/begin")
        assert r.status_code == 200 and 'action="https://github.com/settings/apps/new?state=' in r.text
        csp = r.headers["content-security-policy"]
        assert "form-action https://github.com;" in csp and "script-src 'nonce-" in csp
        return r.text.split("state=")[1].split('"')[0]

    # someone else's GitHub account creates the app: nothing is kept, nobody gets in
    st = await begin()
    r = await api.c.get(f"/api/v1/auth/github/setup/callback?code=mcode&state={st}")
    assert r.headers["location"] == "/?login_error=github_setup_not_owner"
    assert not (settings.state_dir / "github-signin.json").exists()
    assert (await api.c.get("/api/v1/auth/options")).json()["github_setup"] is True
    # the owner: saved, and straight on to signing in with the new app
    creator["login"] = "eikanwang"
    st = await begin()
    r = await api.c.get(f"/api/v1/auth/github/setup/callback?code=mcode&state={st}")
    assert r.headers["location"] == "/api/v1/auth/github/start"
    assert json.loads((settings.state_dir / "github-signin.json").read_text())["allowed_users"] == ["eikanwang"]
    assert (await api.c.get("/api/v1/auth/options")).json() == {"token": True, "github": True, "github_setup": False, "google": False}
    assert "client_id=Iv23x" in (await api.c.get("/api/v1/auth/github/start")).headers["location"]
    # once set up, the begin URL just signs in
    assert (await api.c.get("/api/v1/auth/github/setup/begin")).headers["location"] == "/api/v1/auth/github/start"

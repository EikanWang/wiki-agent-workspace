"""Social sign-in: "Continue with GitHub" (OAuth app) and "Continue with Google" (OpenID Connect), as alternatives
to the access token. Both follow the same steps; Google identifies you by a verified email address.

1. ``GET /auth/github/start`` sets a short-lived random ``state`` cookie and redirects to GitHub's authorize page
   (scope: none, i.e. public identity only).
2. GitHub redirects back to ``GET /auth/github/callback?code&state``. The state must match the cookie (CSRF), the
   code is exchanged server-side for a token, ``GET /user`` gives the GitHub login, and only logins listed in
   ``[auth.github] allowed_users`` (case-insensitive) get a normal login session (same cookie as a token login).
   The GitHub token is used for that single request and never stored.
3. Errors redirect to ``/?login_error=<code>``; the login page explains them.

Having *a* GitHub/Google account is never enough: an empty ``allowed_users`` / ``allowed_emails`` keeps that
provider disabled.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from .config import Settings
from .deps import AppContext, get_ctx, require_auth

log = logging.getLogger("waw.auth")

STATE_COOKIE = "waw_gh_state"
CALLBACK_PATH = "/api/v1/auth/github/callback"

router = APIRouter()


def github_enabled(settings: Settings) -> bool:
    return bool(settings.github_client_id and settings.github_client_secret and settings.github_allowed_users)


def _callback_url(settings: Settings, request: Request) -> str:
    if settings.github_callback_url:
        return settings.github_callback_url
    # the address the browser used (must match the OAuth app's "Authorization callback URL")
    return str(request.base_url).rstrip("/") + CALLBACK_PATH


def google_enabled(settings: Settings) -> bool:
    return bool(settings.google_client_id and settings.google_client_secret and settings.google_allowed_emails)


def _fail(code: str, provider: str = "github") -> RedirectResponse:
    resp = RedirectResponse(f"/?{urlencode({'login_error': code})}", status_code=303)
    resp.delete_cookie(STATE_COOKIE, path=f"/api/v1/auth/{provider}")
    return resp


def _set_state(resp: RedirectResponse, state: str, provider: str, request: Request, s: Settings) -> None:
    # Lax (not Strict): it must come back on the provider's cross-site redirect to the callback
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=s.cookie_secure or request.url.scheme == "https", path=f"/api/v1/auth/{provider}")


def _state_ok(request: Request, state: str) -> bool:
    expected = request.cookies.get(STATE_COOKIE, "")
    return bool(state and expected and hmac.compare_digest(state, expected))


_owner_cache: dict[str, tuple[float, str]] = {}


def server_owner(settings: Settings) -> str:
    """The GitHub account that owns this server: the first `[auth.github] allowed_users` entry, else the account
    the server user's `gh` CLI is logged in as (cached 10 min). "" when unknown."""
    if settings.github_allowed_users:
        return settings.github_allowed_users[0]
    hit = _owner_cache.get("gh")
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    import shutil
    import subprocess

    login = ""
    if shutil.which("gh"):
        from . import gh_limits

        try:
            p = gh_limits.run_gh(["api", "user", "--jq", ".login"], timeout=15, dedupe=True)
            login = p.out.strip() if p.rc == 0 else ""
        except (OSError, subprocess.SubprocessError, gh_limits.ApiError):
            login = ""
        gh_limits.LIMITER.set_identity(login)
    _owner_cache["gh"] = (time.time(), login)
    return login


@router.get("/auth/options")
async def login_options(ctx: AppContext = Depends(get_ctx)):
    """Unauthenticated: which sign-in methods the login page offers. `github_setup`: GitHub sign-in is not set up
    yet, but the server knows its owner, so the button sets it up on GitHub first (only that owner can finish)."""
    import asyncio

    s = ctx.settings
    enabled = github_enabled(s)
    owner = "" if enabled else await asyncio.to_thread(server_owner, s)
    return {"token": True, "github": enabled or bool(owner), "github_setup": not enabled and bool(owner), "google": google_enabled(s)}


@router.get("/auth/github/start")
def github_start(request: Request, ctx: AppContext = Depends(get_ctx)):
    s = ctx.settings
    if not github_enabled(s):
        return _fail("github_disabled")
    state = secrets.token_urlsafe(24)
    params = {"client_id": s.github_client_id, "redirect_uri": _callback_url(s, request), "state": state,
              "scope": "", "allow_signup": "false"}
    resp = RedirectResponse(f"{s.github_oauth_base}/login/oauth/authorize?{urlencode(params)}", status_code=302)
    _set_state(resp, state, "github", request, s)
    return resp


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str = Query(""), state: str = Query(""), error: str = Query(""),
                          ctx: AppContext = Depends(get_ctx)):
    s = ctx.settings
    if not github_enabled(s):
        return _fail("github_disabled")
    if error:  # e.g. access_denied: the user clicked Cancel on GitHub
        return _fail("github_cancelled" if error == "access_denied" else "github_error")
    if not code or not _state_ok(request, state):
        return _fail("github_state")
    transport = ctx.extra.get("github_transport")  # tests inject a fake GitHub
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport, headers={"Accept": "application/json", "User-Agent": "wiki-agent-workspace"}) as client:
            r = await client.post(f"{s.github_oauth_base}/login/oauth/access_token", data={
                "client_id": s.github_client_id, "client_secret": s.github_client_secret, "code": code,
                "redirect_uri": _callback_url(s, request)})
            token = (r.json() if r.status_code == 200 else {}).get("access_token")
            if not token:
                log.warning("GitHub sign-in: code exchange failed (HTTP %s): %s", r.status_code, r.text[:200])
                return _fail("github_exchange")
            u = await client.get(f"{s.github_api_base}/user", headers={"Authorization": f"Bearer {token}"})
            login = (u.json() if u.status_code == 200 else {}).get("login") or ""
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("GitHub sign-in: cannot reach GitHub: %s", exc)
        return _fail("github_unreachable")
    if not login:
        return _fail("github_exchange")
    allowed = {a.lower() for a in s.github_allowed_users}
    if login.lower() not in allowed:
        log.warning("GitHub sign-in refused for %s (not in [auth.github] allowed_users)", login)
        return _fail("github_not_allowed")
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/api/v1/auth/github")
    ctx.auth.create_login(resp, request, identity=f"github:{login}")
    log.info("signed in with GitHub as %s", login)
    return resp


# ---------------------------------------------------------------------- Google (OpenID Connect)

def _google_callback_url(settings: Settings, request: Request) -> str:
    return settings.google_callback_url or (str(request.base_url).rstrip("/") + "/api/v1/auth/google/callback")


@router.get("/auth/google/start")
def google_start(request: Request, ctx: AppContext = Depends(get_ctx)):
    s = ctx.settings
    if not google_enabled(s):
        return _fail("google_disabled", "google")
    state = secrets.token_urlsafe(24)
    params = {"client_id": s.google_client_id, "redirect_uri": _google_callback_url(s, request), "response_type": "code",
              "scope": "openid email", "state": state, "prompt": "select_account"}
    resp = RedirectResponse(f"{s.google_auth_url}?{urlencode(params)}", status_code=302)
    _set_state(resp, state, "google", request, s)
    return resp


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = Query(""), state: str = Query(""), error: str = Query(""),
                          ctx: AppContext = Depends(get_ctx)):
    s = ctx.settings
    if not google_enabled(s):
        return _fail("google_disabled", "google")
    if error:
        return _fail("google_cancelled" if error == "access_denied" else "google_error", "google")
    if not code or not _state_ok(request, state):
        return _fail("google_state", "google")
    transport = ctx.extra.get("google_transport")  # tests inject a fake Google
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport, headers={"Accept": "application/json"}) as client:
            r = await client.post(s.google_token_url, data={
                "client_id": s.google_client_id, "client_secret": s.google_client_secret, "code": code,
                "redirect_uri": _google_callback_url(s, request), "grant_type": "authorization_code"})
            token = (r.json() if r.status_code == 200 else {}).get("access_token")
            if not token:
                log.warning("Google sign-in: code exchange failed (HTTP %s): %s", r.status_code, r.text[:200])
                return _fail("google_exchange", "google")
            u = await client.get(s.google_userinfo_url, headers={"Authorization": f"Bearer {token}"})
            info = u.json() if u.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Google sign-in: cannot reach Google: %s", exc)
        return _fail("google_unreachable", "google")
    email = str(info.get("email") or "").lower()
    if not email:
        return _fail("google_exchange", "google")
    if info.get("email_verified") is not True:
        return _fail("google_unverified", "google")
    if email not in {e.lower() for e in s.google_allowed_emails}:
        log.warning("Google sign-in refused for %s (not in [auth.google] allowed_emails)", email)
        return _fail("google_not_allowed", "google")
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/api/v1/auth/google")
    ctx.auth.create_login(resp, request, identity=f"google:{email}")
    log.info("signed in with Google as %s", email)
    return resp


# ---------------------------------------------------------------------- one-click GitHub setup (app manifest flow)
#
# Instead of registering an OAuth app by hand and copying its id/secret, the signed-in user clicks "Set up Continue
# with GitHub": the browser posts a GitHub App *manifest* to github.com, the user confirms "Create GitHub App", and
# GitHub redirects back with a one-time code that the server converts into the new app's client id and secret
# (POST /app-manifests/{code}/conversions). They are saved in <state_dir>/github-signin.json (mode 0600; config.toml
# values win), the creating account becomes the only allowed user, and the login page shows the button at once.

SETUP_COOKIE = "waw_gh_setup"
_setup_states: dict[str, float] = {}  # state -> issued at (only issued to signed-in sessions)


def saved_path(settings: Settings) -> Path:
    return settings.state_dir / "github-signin.json"


def apply_saved(settings: Settings) -> None:
    """At startup: use the keys saved by the one-click setup unless config.toml sets GitHub sign-in itself."""
    p = saved_path(settings)
    if settings.github_client_id or not p.is_file():
        return
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("ignoring unreadable %s", p)
        return
    settings.github_client_id = d.get("client_id", "")
    settings.github_client_secret = settings.github_client_secret or d.get("client_secret", "")
    settings.github_allowed_users = settings.github_allowed_users or list(d.get("allowed_users") or [])


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/auth/github/setup")
def github_setup_info(request: Request, ctx: AppContext = Depends(require_auth)):
    """For the signed-in user: is GitHub sign-in on, and the manifest to create its GitHub App with one click."""
    s = ctx.settings
    saved = None
    if saved_path(s).is_file():
        try:
            d = json.loads(saved_path(s).read_text())
            saved = {"app_url": d.get("app_url"), "slug": d.get("slug"), "created_at": d.get("created_at")}
        except (OSError, ValueError):
            pass
    state, manifest = _new_setup(request, s, then_sign_in=False)
    from fastapi.responses import JSONResponse

    resp = JSONResponse({
        "enabled": github_enabled(s), "configured_in": "config.toml" if s.github_client_id and not saved else ("setup" if saved else None),
        "allowed_users": s.github_allowed_users, "saved": saved,
        "action": f"{s.github_oauth_base}/settings/apps/new?{urlencode({'state': state})}", "manifest": json.dumps(manifest),
    })
    resp.set_cookie(SETUP_COOKIE, state, max_age=900, httponly=True, samesite="lax",
                    secure=s.cookie_secure or request.url.scheme == "https", path="/api/v1/auth/github/setup")
    return resp


def _new_setup(request: Request, s: Settings, *, then_sign_in: bool) -> tuple[str, dict]:
    base = _base(request)
    state = secrets.token_urlsafe(24)
    now = time.time()
    for k, (t, _) in list(_setup_states.items()):
        if now - t > 900:
            del _setup_states[k]
    _setup_states[state] = (now, then_sign_in)
    manifest = {
        "name": f"Wiki Agent Workspace {secrets.token_hex(3)}",  # app names are unique across GitHub
        "url": base,
        "redirect_url": f"{base}/api/v1/auth/github/setup/callback",
        "callback_urls": [s.github_callback_url or f"{base}{CALLBACK_PATH}"],
        "description": "Sign-in for a self-hosted Wiki Agent Workspace. Reads only your public GitHub identity.",
        # public: other allowed GitHub accounts can sign in too (a private app only works for its owner). The app has
        # no permissions; who gets into this server is decided by allowed_users alone.
        "public": True,
        "default_permissions": {},
        "request_oauth_on_install": False,
    }
    return state, manifest


@router.get("/auth/github/setup/begin")
async def github_setup_begin(request: Request, ctx: AppContext = Depends(get_ctx)):
    """From the login page, before GitHub sign-in exists: a page that posts the app manifest to GitHub (GitHub only
    takes it as a form POST). Safe without a login: the result is accepted only if GitHub says the app was created by
    the server's owner (see server_owner), and that account is the only one allowed to sign in."""
    import asyncio
    import html

    from fastapi.responses import HTMLResponse

    s = ctx.settings
    if github_enabled(s):
        return RedirectResponse("/api/v1/auth/github/start", status_code=303)
    owner = await asyncio.to_thread(server_owner, s)
    if not owner:
        return _fail("github_disabled")
    state, manifest = _new_setup(request, s, then_sign_in=True)
    nonce = secrets.token_urlsafe(16)
    action = f"{s.github_oauth_base}/settings/apps/new?{urlencode({'state': state})}"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Setting up GitHub sign-in…</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#333}}
.c{{max-width:460px;text-align:center}}button{{font:inherit;padding:8px 16px;border-radius:8px;border:1px solid #24292f;background:#24292f;color:#fff;cursor:pointer}}</style></head>
<body><div class="c"><p>Taking you to GitHub to create this server's sign-in app (one time). Click <b>Create GitHub App</b> there.</p>
<form id="f" method="post" action="{html.escape(action)}"><input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">
<button type="submit">Continue to GitHub</button></form></div>
<script nonce="{nonce}">document.getElementById("f").submit();</script></body></html>"""
    resp = HTMLResponse(page)
    # only this page's own script may run, and the form may only go to GitHub (origin: a CSP source with a path would
    # match that exact path only)
    from urllib.parse import urlsplit

    o = urlsplit(s.github_oauth_base)
    resp.headers["Content-Security-Policy"] = (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
                                               f"form-action {o.scheme}://{o.netloc}; base-uri 'none'; frame-ancestors 'none'")
    resp.set_cookie(SETUP_COOKIE, state, max_age=900, httponly=True, samesite="lax",
                    secure=s.cookie_secure or request.url.scheme == "https", path="/api/v1/auth/github/setup")
    return resp


@router.get("/auth/github/setup/callback")
async def github_setup_callback(request: Request, code: str = Query(""), state: str = Query(""), ctx: AppContext = Depends(get_ctx)):
    """GitHub returns here after "Create GitHub App". The state was issued to a signed-in session and is bound to
    this browser by a cookie; the code is single-use and expires after an hour (GitHub)."""
    s = ctx.settings

    def done(result: str) -> RedirectResponse:
        r = RedirectResponse(f"/?{urlencode({'github_setup': result})}", status_code=303)
        r.delete_cookie(SETUP_COOKIE, path="/api/v1/auth/github/setup")
        return r

    cookie = request.cookies.get(SETUP_COOKIE, "")
    issued = _setup_states.pop(state, None) if state else None
    if not code or not state or not cookie or not hmac.compare_digest(state, cookie) or issued is None:
        return done("state")
    then_sign_in = issued[1]
    transport = ctx.extra.get("github_transport")
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport, headers={"Accept": "application/vnd.github+json", "User-Agent": "wiki-agent-workspace"}) as client:
            r = await client.post(f"{s.github_api_base}/app-manifests/{code}/conversions")
            data = r.json() if r.status_code in (200, 201) else {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("GitHub setup: cannot reach GitHub: %s", exc)
        return done("unreachable")
    owner = (data.get("owner") or {}).get("login") or ""
    if not data.get("client_id") or not data.get("client_secret") or not owner:
        log.warning("GitHub setup: manifest conversion failed (HTTP %s): %s", r.status_code, str(data)[:200])
        return done("failed")
    expected = await asyncio.to_thread(server_owner, s)
    if then_sign_in and (not expected or owner.lower() != expected.lower()):
        # started from the login page by someone who is not this server's owner: keep nothing
        log.warning("GitHub setup refused: the app was created by %s, but this server belongs to %s", owner, expected or "?")
        return RedirectResponse(f"/?{urlencode({'login_error': 'github_setup_not_owner'})}", status_code=303)
    record = {"client_id": data["client_id"], "client_secret": data["client_secret"], "allowed_users": [owner],
              "slug": data.get("slug"), "app_url": data.get("html_url"), "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    p = saved_path(s)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(record, fh, indent=2)
    os.replace(tmp, p)
    # live: the login page offers the button immediately (config.toml values, if any, still win)
    if not s.github_client_id or s.github_client_id == record["client_id"] or not s.github_client_secret:
        s.github_client_id, s.github_client_secret = record["client_id"], record["client_secret"]
        s.github_allowed_users = s.github_allowed_users or [owner]
    log.info("GitHub sign-in set up: app %s, allowed user %s", record["slug"], owner)
    if then_sign_in:  # from the login page: go on and sign in with the new app
        r = RedirectResponse("/api/v1/auth/github/start", status_code=303)
        r.delete_cookie(SETUP_COOKIE, path="/api/v1/auth/github/setup")
        return r
    return done("ok")

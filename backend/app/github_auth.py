""""Sign in with GitHub" (OAuth web flow of a GitHub OAuth app), as an alternative to the access token.

1. ``GET /auth/github/start`` sets a short-lived random ``state`` cookie and redirects to GitHub's authorize page
   (scope: none, i.e. public identity only).
2. GitHub redirects back to ``GET /auth/github/callback?code&state``. The state must match the cookie (CSRF), the
   code is exchanged server-side for a token, ``GET /user`` gives the GitHub login, and only logins listed in
   ``[auth.github] allowed_users`` (case-insensitive) get a normal login session (same cookie as a token login).
   The GitHub token is used for that single request and never stored.
3. Errors redirect to ``/?login_error=<code>``; the login page explains them.

Having *a* GitHub account is never enough: an empty ``allowed_users`` keeps GitHub sign-in disabled.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from .config import Settings
from .deps import AppContext, get_ctx

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


def _fail(code: str) -> RedirectResponse:
    resp = RedirectResponse(f"/?{urlencode({'login_error': code})}", status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/api/v1/auth/github")
    return resp


@router.get("/auth/options")
def login_options(ctx: AppContext = Depends(get_ctx)):
    """Unauthenticated: which sign-in methods the login page offers."""
    return {"token": True, "github": github_enabled(ctx.settings)}


@router.get("/auth/github/start")
def github_start(request: Request, ctx: AppContext = Depends(get_ctx)):
    s = ctx.settings
    if not github_enabled(s):
        return _fail("github_disabled")
    state = secrets.token_urlsafe(24)
    params = {"client_id": s.github_client_id, "redirect_uri": _callback_url(s, request), "state": state,
              "scope": "", "allow_signup": "false"}
    resp = RedirectResponse(f"{s.github_oauth_base}/login/oauth/authorize?{urlencode(params)}", status_code=302)
    # Lax (not Strict): it must come back on GitHub's cross-site redirect to the callback
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=s.cookie_secure or request.url.scheme == "https", path="/api/v1/auth/github")
    return resp


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str = Query(""), state: str = Query(""), error: str = Query(""),
                          ctx: AppContext = Depends(get_ctx)):
    s = ctx.settings
    if not github_enabled(s):
        return _fail("github_disabled")
    if error:  # e.g. access_denied: the user clicked Cancel on GitHub
        return _fail("github_cancelled" if error == "access_denied" else "github_error")
    expected = request.cookies.get(STATE_COOKIE, "")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
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

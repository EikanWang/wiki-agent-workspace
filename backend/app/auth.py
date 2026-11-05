"""Single-user token login, HttpOnly session cookie, HMAC-derived CSRF token, Origin checks.

DESIGN §15. Plaintext tokens are never stored: the cookie value is hashed with SHA-256,
the CSRF token is HMAC-SHA-256(server_secret, raw_cookie) and only its hash is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from datetime import timedelta
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request, Response, WebSocket
from starlette.datastructures import Headers

from .config import Settings
from .db import Database, new_id, utcnow
from .errors import forbidden, unauthorized
from .models import LoginSession

COOKIE_NAME = "waw_session"
CSRF_HEADER = "x-csrf-token"
WS_SUBPROTOCOL_PREFIX = "waw-csrf."


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class AuthManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    # ------------------------------------------------------------------ tokens
    def csrf_for_cookie(self, raw_cookie: str) -> str:
        return hmac.new(self.settings.server_secret.encode("utf-8"), raw_cookie.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_access_token(self, token: str) -> bool:
        expected = self.settings.access_token
        return bool(expected) and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))

    # ------------------------------------------------------------------ login
    def create_login(self, response: Response, request: Request) -> tuple[str, str]:
        raw = secrets.token_urlsafe(32)
        csrf = self.csrf_for_cookie(raw)
        expires = utcnow() + timedelta(days=self.settings.session_days)
        with self.db.write() as s:
            s.add(LoginSession(id=new_id(), token_hash=sha256_hex(raw), csrf_hash=sha256_hex(csrf), expires_at=expires))
        self._set_cookie(response, raw, request)
        return csrf, expires.isoformat().replace("+00:00", "Z")

    def _set_cookie(self, response: Response, raw: str, request: Request) -> None:
        secure = self.settings.cookie_secure or self._request_is_https(request)
        response.set_cookie(
            COOKIE_NAME,
            raw,
            max_age=self.settings.session_days * 86400,
            httponly=True,
            samesite="strict",
            secure=secure,
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/")

    def logout(self, request: Request, response: Response) -> None:
        raw = request.cookies.get(COOKIE_NAME)
        if raw:
            with self.db.write() as s:
                row = s.query(LoginSession).filter_by(token_hash=sha256_hex(raw)).one_or_none()
                if row:
                    s.delete(row)
        self.clear_cookie(response)

    # ------------------------------------------------------------------ lookup
    def _lookup(self, raw_cookie: str | None) -> Optional[LoginSession]:
        if not raw_cookie:
            return None
        with self.db.read() as s:
            row = s.query(LoginSession).filter_by(token_hash=sha256_hex(raw_cookie)).one_or_none()
            if row is None:
                return None
            exp = row.expires_at
            if exp.tzinfo is None:
                from datetime import timezone

                exp = exp.replace(tzinfo=timezone.utc)
            if exp < utcnow():
                return None
            return row

    def _touch(self, row: LoginSession, response: Response | None, raw: str, request: Request | None) -> None:
        """Renew while active: extend expiry once per hour of activity."""
        now = utcnow()
        last = row.last_seen_at
        if last.tzinfo is None:
            from datetime import timezone

            last = last.replace(tzinfo=timezone.utc)
        if (now - last) > timedelta(hours=1):
            with self.db.write() as s:
                db_row = s.get(LoginSession, row.id)
                if db_row:
                    db_row.last_seen_at = now
                    db_row.expires_at = now + timedelta(days=self.settings.session_days)
            if response is not None and request is not None:
                self._set_cookie(response, raw, request)

    # ------------------------------------------------------------------ gateway
    def _gateway_authenticated(self, headers: Headers, client_host: str | None) -> bool:
        s = self.settings
        if not s.gateway_auth or not s.gateway_user_header:
            return False
        if not self._from_trusted_proxy(client_host):
            return False
        return bool(headers.get(s.gateway_user_header))

    def _from_trusted_proxy(self, client_host: str | None) -> bool:
        if not client_host:
            return False
        try:
            addr = ipaddress.ip_address(client_host)
        except ValueError:
            return False
        for entry in self.settings.trusted_proxies:
            try:
                if "/" in entry:
                    if addr in ipaddress.ip_network(entry, strict=False):
                        return True
                elif addr == ipaddress.ip_address(entry):
                    return True
            except ValueError:
                continue
        return False

    # ------------------------------------------------------------------ origin
    def _request_is_https(self, request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        client = request.client.host if request.client else None
        if self._from_trusted_proxy(client):
            return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        return False

    def _expected_hosts(self, headers: Headers, client_host: str | None) -> set[str]:
        hosts: set[str] = set()
        host = headers.get("host")
        if host:
            hosts.add(host.lower())
        if self._from_trusted_proxy(client_host):
            fwd = headers.get("x-forwarded-host")
            if fwd:
                hosts.add(fwd.split(",")[0].strip().lower())
        return hosts

    def check_origin(self, headers: Headers, client_host: str | None, *, required: bool) -> None:
        origin = headers.get("origin")
        if not origin:
            if required:
                raise forbidden("Missing Origin header", "ORIGIN_REQUIRED")
            return
        if origin == "null":
            raise forbidden("Origin not allowed", "ORIGIN_NOT_ALLOWED")
        parsed = urlsplit(origin)
        origin_host = (parsed.netloc or "").lower()
        if origin_host and origin_host in self._expected_hosts(headers, client_host):
            return
        if origin.rstrip("/") in {o.rstrip("/") for o in self.settings.allowed_origins}:
            return
        raise forbidden("Origin not allowed", "ORIGIN_NOT_ALLOWED")

    # ------------------------------------------------------------------ dependencies
    def authenticate_request(self, request: Request, response: Response | None = None, *, mutating: bool = False) -> LoginSession | None:
        """Returns the login session, or None when the request is authenticated by a
        configured gateway. Raises 401/403 otherwise."""
        client_host = request.client.host if request.client else None
        if mutating:
            self.check_origin(request.headers, client_host, required=True)
        raw = request.cookies.get(COOKIE_NAME)
        row = self._lookup(raw)
        if row is None:
            if self._gateway_authenticated(request.headers, client_host):
                return None
            raise unauthorized()
        if mutating:
            token = request.headers.get(CSRF_HEADER, "")
            if not token or not hmac.compare_digest(sha256_hex(token), row.csrf_hash):
                raise forbidden("Invalid CSRF token", "CSRF_INVALID")
        assert raw is not None
        self._touch(row, response, raw, request)
        return row

    def csrf_token_for_request(self, request: Request) -> str | None:
        raw = request.cookies.get(COOKIE_NAME)
        if not raw or self._lookup(raw) is None:
            return None
        return self.csrf_for_cookie(raw)

    def authenticate_websocket(self, ws: WebSocket) -> str | None:
        """Cookie + CSRF subprotocol + Origin. Returns the accepted subprotocol."""
        client_host = ws.client.host if ws.client else None
        self.check_origin(ws.headers, client_host, required=True)
        raw = ws.cookies.get(COOKIE_NAME)
        row = self._lookup(raw)
        if row is None:
            if self._gateway_authenticated(ws.headers, client_host):
                return None
            raise unauthorized()
        protos = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        for p in protos:
            if p.startswith(WS_SUBPROTOCOL_PREFIX):
                token = p[len(WS_SUBPROTOCOL_PREFIX):]
                if hmac.compare_digest(sha256_hex(token), row.csrf_hash):
                    return p
        raise forbidden("WebSocket CSRF subprotocol missing or invalid", "CSRF_INVALID")

    def login_id_for_request(self, request: Request) -> str:
        raw = request.cookies.get(COOKIE_NAME)
        row = self._lookup(raw)
        return row.id if row else "gateway"

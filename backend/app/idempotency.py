"""Idempotency-Key support for mutating /api/v1 requests (DESIGN §13).

Pure ASGI middleware: buffers the request body, keys on (login, method, path, body sha256),
replays the stored response for an exact retry, and returns 409 IDEMPOTENCY_KEY_REUSED when
the same key is reused for a different request. Keys are kept for 24 hours.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from .auth import COOKIE_NAME, sha256_hex
from .db import Database, utcnow
from .models import IdempotencyKey, LoginSession

HEADER = b"idempotency-key"


class IdempotencyMiddleware:
    def __init__(self, app, db: Database):
        self.app = app
        self.db = db
        self._last_purge = utcnow()

    def _login_id(self, scope) -> str:
        cookies = {}
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    if "=" in part:
                        name, val = part.strip().split("=", 1)
                        cookies[name] = val
        raw = cookies.get(COOKIE_NAME)
        if not raw:
            return "anonymous"
        with self.db.read() as s:
            row = s.query(LoginSession).filter_by(token_hash=sha256_hex(raw)).one_or_none()
            return row.id if row else "anonymous"

    def _purge(self) -> None:
        now = utcnow()
        if (now - self._last_purge) < timedelta(minutes=10):
            return
        self._last_purge = now
        with self.db.write() as s:
            s.query(IdempotencyKey).filter(IdempotencyKey.created_at < now - timedelta(hours=24)).delete()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
            await self.app(scope, receive, send)
            return
        key = None
        for k, v in scope.get("headers", []):
            if k == HEADER:
                key = v.decode("latin-1")[:255]
        if not key or not scope["path"].startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return

        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        body_sha = hashlib.sha256(body).hexdigest()
        login_id = self._login_id(scope)
        method = scope["method"]
        path = scope["path"]
        self._purge()

        with self.db.read() as s:
            row = s.query(IdempotencyKey).filter_by(key=key, login_id=login_id).one_or_none()
        if row is not None:
            if row.method != method or row.path != path or row.body_sha256 != body_sha:
                payload = json.dumps({"error": {"code": "IDEMPOTENCY_KEY_REUSED", "message": "Idempotency-Key was already used for a different request", "details": {}}}).encode()
                await send({"type": "http.response.start", "status": 409, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
                await send({"type": "http.response.body", "body": payload})
                return
            payload = row.response_json.encode("utf-8")
            await send({"type": "http.response.start", "status": row.status_code, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), (b"idempotent-replayed", b"true")]})
            await send({"type": "http.response.body", "body": payload})
            return

        sent_body = False

        async def replay_receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        status_holder: dict[str, Any] = {"status": 500, "headers": [], "body": b""}

        async def capture_send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                status_holder["body"] += message.get("body", b"")
            await send(message)

        await self.app(scope, replay_receive, capture_send)
        status = status_holder["status"]
        ctype = dict(status_holder["headers"]).get(b"content-type", b"")
        if status < 500 and ctype.startswith(b"application/json") and login_id != "anonymous":
            with self.db.write() as s:
                if s.query(IdempotencyKey).filter_by(key=key, login_id=login_id).one_or_none() is None:
                    s.add(IdempotencyKey(key=key, login_id=login_id, method=method, path=path, body_sha256=body_sha,
                                         status_code=status, response_json=status_holder["body"].decode("utf-8", "replace")))

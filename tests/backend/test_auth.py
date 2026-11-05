"""Acceptance tests A3/D13: authentication, CSRF, origin, no secrets in responses."""

from __future__ import annotations

import httpx
import pytest

from conftest import ORIGIN, TOKEN, Api


async def test_unauthenticated_requests_are_rejected(running_app):
    h = await running_app(login=False)
    api = h.api
    r = await api.get("/api/v1/bootstrap")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    r = await api.get("/api/v1/files")
    assert r.status_code == 401
    r = await api.c.post("/api/v1/terminals", json={}, headers={"origin": ORIGIN})
    assert r.status_code == 401
    # health is public, process-only
    r = await api.get("/api/v1/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_wrong_token_rejected_and_token_not_echoed(running_app):
    h = await running_app(login=False)
    r = await h.api.login("wrong")
    assert r.status_code == 401
    assert TOKEN not in r.text


async def test_login_once_then_repeated_actions_without_prompts(api: Api):
    r = await api.get("/api/v1/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert body["csrf_token"] == api.csrf
    assert TOKEN not in r.text
    # several mutating actions with the same session: no re-authentication
    for _ in range(3):
        doc = await api.open_doc()
        r = await api.put(f"/api/v1/documents/{doc['document_id']}/reading-state",
                          json={"version_sha256": doc["sha256"], "heading": "Guide", "offset": 3, "scroll_fraction": 0.1})
        assert r.status_code == 200


async def test_csrf_and_origin_enforced_on_mutations(api: Api):
    doc = await api.open_doc()
    url = f"/api/v1/documents/{doc['document_id']}/reading-state"
    body = {"version_sha256": doc["sha256"], "offset": 1, "scroll_fraction": 0.0}
    # missing CSRF header
    r = await api.c.put(url, json=body, headers={"origin": ORIGIN})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_INVALID"
    # wrong origin
    r = await api.c.put(url, json=body, headers={"origin": "http://evil.example", "x-csrf-token": api.csrf})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    # missing origin
    r = await api.c.put(url, json=body, headers={"x-csrf-token": api.csrf})
    assert r.status_code == 403
    # correct
    r = await api.put(url, json=body)
    assert r.status_code == 200


async def test_logout_invalidates_cookie(api: Api):
    r = await api.post("/api/v1/auth/logout")
    assert r.status_code == 200
    r = await api.get("/api/v1/bootstrap")
    assert r.status_code == 401


async def test_error_shape_for_malformed_input(api: Api):
    r = await api.post("/api/v1/terminals", json={"cwd": 123})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "traceback" not in r.text.lower()

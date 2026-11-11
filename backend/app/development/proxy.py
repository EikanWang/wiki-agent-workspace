"""The editor through the app: `/dev-editor/<workspace id>/…` → the workspace's code-server Unix socket.

HTTP and WebSocket traffic is relayed with the app's login: the session cookie is required (and not forwarded),
state-changing requests and WebSockets need a same-origin Origin header. The editor's pages may be framed by the app
only (X-Frame-Options SAMEORIGIN) and keep their own Content-Security-Policy.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect

from ..auth import COOKIE_NAME
from ..errors import ApiError

log = logging.getLogger("waw.development.proxy")

PREFIX = "/dev-editor"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "host", "content-length", "x-frame-options"}
router = APIRouter()
_clients: dict[str, httpx.AsyncClient] = {}


def _client(sock: Path) -> httpx.AsyncClient:
    c = _clients.get(str(sock))
    if c is None or c.is_closed:
        c = _clients[str(sock)] = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(sock)),
                                                    timeout=httpx.Timeout(60, read=None), follow_redirects=False)
    return c


def _strip_cookie(value: str) -> str:
    keep = [p for p in value.split(";") if p.strip() and not p.strip().startswith(COOKIE_NAME + "=")]
    return ";".join(keep).strip()


def _upstream_headers(headers, extra_drop: set[str] = frozenset()) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP or lk in extra_drop or lk.startswith("sec-websocket"):
            continue
        if lk == "cookie":
            v = _strip_cookie(v)
            if not v:
                continue
        if lk == "origin":
            v = "http://localhost"  # already checked against this app's own origin
        if lk == "referer":
            continue
        out.append((k, v))
    out.append(("host", "localhost"))
    return out


def _editor(request_or_ws):
    ed = request_or_ws.app.state.ctx.extra.get("dev_editor")
    if ed is None:
        raise ApiError(503, "EDITOR_UNAVAILABLE", "PR Development is not available")
    return ed


@router.api_route(PREFIX + "/{dev_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def editor_root(dev_id: str, request: Request):
    q = request.url.query
    return RedirectResponse(f"{PREFIX}/{dev_id}/" + (f"?{q}" if q else ""), status_code=307)


@router.api_route(PREFIX + "/{dev_id}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                  include_in_schema=False)
async def editor_http(dev_id: str, path: str, request: Request):
    ctx = request.app.state.ctx
    ctx.auth.authenticate_request(request, None, mutating=False)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        ctx.auth.check_origin(request.headers, request.client.host if request.client else None, required=True)
    sock = _editor(request).require_running(dev_id)
    url = "http://localhost/" + path + (f"?{request.url.query}" if request.url.query else "")
    client = _client(sock)
    body = await request.body()
    req = client.build_request(request.method, url, headers=_upstream_headers(request.headers), content=body or None)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise ApiError(502, "EDITOR_UNREACHABLE", f"the editor did not answer: {exc.__class__.__name__}")
    raw: list[tuple[bytes, bytes]] = []
    for k, v in resp.headers.multi_items():
        lk = k.lower()
        if lk in HOP:
            continue
        if lk == "location" and v.startswith("/") and not v.startswith("//"):
            v = f"{PREFIX}/{dev_id}{v}"
        raw.append((lk.encode("latin-1"), v.encode("latin-1")))
    raw.append((b"x-frame-options", b"SAMEORIGIN"))
    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
    out.raw_headers = [h for h in out.raw_headers if h[0] not in (b"content-type",)] + raw
    return out


@router.websocket(PREFIX + "/{dev_id}/{path:path}")
async def editor_ws(ws: WebSocket, dev_id: str, path: str):
    from websockets.asyncio.client import unix_connect
    from websockets.exceptions import ConnectionClosed

    ctx = ws.app.state.ctx
    try:
        ctx.auth.authenticate_websocket_cookie(ws)
        sock = _editor(ws).require_running(dev_id)
    except ApiError:
        await ws.close(code=4401)
        return
    query = ws.url.query
    uri = "ws://localhost/" + path + (f"?{query}" if query else "")
    protos = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    headers = [(k, v) for k, v in _upstream_headers(ws.headers) if k.lower() != "host"]
    try:
        up = await unix_connect(str(sock), uri, additional_headers=headers, subprotocols=protos or None, max_size=None,
                                compression=None, open_timeout=20, ping_interval=None)
    except Exception as exc:
        log.info("editor websocket connect failed: %s", exc)
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol=up.subprotocol)

    async def to_upstream() -> None:
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    await up.send(m["bytes"])
                elif m.get("text") is not None:
                    await up.send(m["text"])
        except (WebSocketDisconnect, ConnectionClosed, RuntimeError):
            pass
        finally:
            await up.close()

    async def to_client() -> None:
        try:
            async for m in up:
                if isinstance(m, str):
                    await ws.send_text(m)
                else:
                    await ws.send_bytes(m)
        except (ConnectionClosed, RuntimeError, WebSocketDisconnect):
            pass
        finally:
            try:
                await ws.close()
            except RuntimeError:
                pass

    tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

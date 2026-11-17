"""Behind a corporate proxy: the app's own localhost connections never go through http_proxy (a no_proxy without
localhost would send them to the proxy, which cannot reach this machine's loopback)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import make_settings

from backend.app.agents.opencode import OpenCodeServerAdapter
from backend.app.config import ProviderConfig
from backend.app.doctor import run_checks

DEAD_PROXY = "http://127.0.0.1:9"  # nothing listens there: a request sent to the proxy fails


def test_doctor_reaches_the_local_server_despite_a_proxy(tmp_path, workspace, monkeypatch):
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.setenv(k, DEAD_PROXY)
        for k in ("no_proxy", "NO_PROXY"):
            monkeypatch.setenv(k, ".example.com")  # localhost not listed
        s = make_settings(tmp_path, workspace)
        s.state_dir.mkdir(parents=True)
        checks = {c.name: c for c in run_checks(s, quick=False, url=f"http://127.0.0.1:{srv.server_port}")}
        assert checks["Server"].level == "ok", checks["Server"].message
    finally:
        srv.shutdown()


def test_opencode_server_mode_on_loopback_ignores_the_proxy():
    local = OpenCodeServerAdapter(ProviderConfig(mode="server", server_url="http://127.0.0.1:4096"))
    assert local.client().trust_env is False
    named = OpenCodeServerAdapter(ProviderConfig(mode="server", server_url="http://localhost:4096"))
    assert named.client().trust_env is False
    remote = OpenCodeServerAdapter(ProviderConfig(mode="server", server_url="http://opencode.example.internal:4096"))
    assert remote.client().trust_env is True  # another machine: the proxy settings apply as usual

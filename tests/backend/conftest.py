from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.config import ProviderConfig, Settings  # noqa: E402
from backend.app.main import create_app  # noqa: E402

FAKES = ROOT / "tests" / "fakes"
TOKEN = "test-access-token-0123456789abcdef"
ORIGIN = "http://testserver"

logging.disable(logging.CRITICAL)


def make_workspace(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("# Repo\n\ncommitted file\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    (path / "docs").mkdir(exist_ok=True)
    (path / "docs" / "guide.md").write_text(
        "# Guide\n\n"
        "torch.compile hands the computation to a compiler pipeline.\n\n"
        "## Capture\n\n"
        "TorchDynamo captures **PyTorch** operations while running Python code and forms a graph.\n"
        "AOTAutograd handles forward and backward graphs.\n\n"
        "## Lowering\n\n"
        "TorchInductor converts the graph into device code.\n\n"
        "- item one\n- item two\n\n"
        "```py\nprint('hi')\n```\n",
        encoding="utf-8",
    )


def make_settings(tmp: Path, workspace: Path, *, default_provider: str | None = "opencode", opencode_mode: str = "cli") -> Settings:
    state = tmp / "state"
    return Settings(
        config_path=tmp / "config.toml",
        state_dir=state,
        workspace_root=workspace,
        default_provider=default_provider,
        providers={
            "opencode": ProviderConfig(bin=str(FAKES / "fake-opencode"), mode=opencode_mode),
            "codex": ProviderConfig(bin=str(FAKES / "fake-codex"), sandbox="workspace-write"),
        },
        access_token=TOKEN,
        server_secret="unit-test-server-secret",
        tmux_history_limit=10000,
        shell="/bin/bash" if Path("/bin/bash").exists() else "/bin/sh",
    )


@pytest.fixture(autouse=True)
def fake_env(tmp_path, monkeypatch):
    state = tmp_path / "fake-agent-state"
    state.mkdir()
    monkeypatch.setenv("FAKE_AGENT_STATE", str(state))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    yield state


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "wiki"
    make_workspace(ws)
    return ws


@pytest.fixture
def settings(tmp_path, workspace) -> Settings:
    return make_settings(tmp_path, workspace)


class Api:
    """Thin client wrapper: login, CSRF, Origin."""

    def __init__(self, client: httpx.AsyncClient):
        self.c = client
        self.csrf: str | None = None

    async def login(self, token: str = TOKEN):
        r = await self.c.post("/api/v1/auth/login", json={"token": token}, headers={"origin": ORIGIN})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
        return r

    @property
    def mut(self) -> dict[str, str]:
        return {"origin": ORIGIN, "x-csrf-token": self.csrf or ""}

    async def get(self, url, **kw):
        return await self.c.get(url, **kw)

    async def post(self, url, json=None, headers=None, **kw):
        h = {**self.mut, **(headers or {})}
        return await self.c.post(url, json=json, headers=h, **kw)

    async def put(self, url, json=None, headers=None, **kw):
        h = {**self.mut, **(headers or {})}
        return await self.c.put(url, json=json, headers=h, **kw)

    async def delete(self, url, headers=None, **kw):
        h = {**self.mut, **(headers or {})}
        return await self.c.delete(url, headers=h, **kw)

    async def open_doc(self, path="docs/guide.md"):
        r = await self.c.get("/api/v1/documents/open", params={"path": path})
        assert r.status_code == 200, r.text
        return r.json()

    async def wait_job(self, job_id: str, timeout: float = 30) -> list[dict]:
        events = []
        async with self.c.stream("GET", f"/api/v1/jobs/{job_id}/events", params={"after": 0}, timeout=timeout) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
        return events

    async def threads(self, doc_id: str):
        r = await self.c.get(f"/api/v1/documents/{doc_id}/threads")
        assert r.status_code == 200, r.text
        return r.json()


def anchor_for(text: str, quote: str, occurrence: int = 0) -> dict:
    from backend.app.textmodel import build_text_model

    model = build_text_model(text).text
    idx = -1
    for _ in range(occurrence + 1):
        idx = model.index(quote, idx + 1)
    return {"quote": quote, "prefix": model[max(0, idx - 40):idx], "suffix": model[idx + len(quote):idx + len(quote) + 40],
            "start": idx, "end": idx + len(quote), "heading": None}


class AppHandle:
    def __init__(self, app, api: Api):
        self.app = app
        self.api = api
        self.ctx = app.state.ctx


@pytest.fixture
async def running_app(settings):
    """Factory: start the ASGI app (with lifespan) and return a logged-in Api."""
    stack: list = []

    async def start(s: Settings | None = None, login: bool = True) -> AppHandle:
        s = s or settings
        app = create_app(s)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url=ORIGIN, timeout=60)
        api = Api(client)
        if login:
            r = await api.login()
            assert r.status_code == 200, r.text
        handle = AppHandle(app, api)
        stack.append((lifespan, client))
        return handle

    yield start
    for lifespan, client in reversed(stack):
        await client.aclose()
        try:
            await lifespan.__aexit__(None, None, None)
        except Exception:
            pass


@pytest.fixture
async def api(running_app) -> Api:
    h = await running_app()
    return h.api


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


requires_tmux = pytest.mark.skipif(not tmux_available(), reason="tmux not installed")

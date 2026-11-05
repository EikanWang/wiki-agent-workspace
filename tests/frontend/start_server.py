"""Start a backend for browser tests: temp workspace, fake agents, fixed token, port 8799."""
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "backend"))
from conftest import make_settings, make_workspace  # noqa: E402

import uvicorn  # noqa: E402

from backend.app.main import create_app  # noqa: E402


def main() -> None:
    logging.disable(logging.NOTSET)  # conftest silences logging; re-enable for the e2e server log
    tmp = Path(tempfile.mkdtemp(prefix="waw-e2e-"))
    state = tmp / "fake-agent-state"
    state.mkdir()
    os.environ["FAKE_AGENT_STATE"] = str(state)
    codex_home = tmp / "codex-home"
    codex_home.mkdir()
    os.environ["CODEX_HOME"] = str(codex_home)
    ws = tmp / "wiki"
    make_workspace(ws)
    settings = make_settings(tmp, ws)
    settings.port = int(os.environ.get("WAW_E2E_PORT", "8799"))
    settings.frontend_dist = ROOT / "frontend" / "dist"
    app = create_app(settings)
    print(f"E2E workspace: {ws}", flush=True)
    (tmp / "WORKSPACE").write_text(str(ws))
    Path("/tmp/waw-e2e-workspace").write_text(str(ws))
    try:
        uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="info")
    finally:
        # test isolation: close the tmux sessions this test server created
        try:
            from backend.app.models import Terminal

            ctx = app.state.ctx
            with ctx.db.read() as s:
                names = [t.tmux_session for t in s.query(Terminal).all()]
            for n in names:
                try:
                    ctx.terminals.tmux.kill_session(n)
                except Exception:
                    pass
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)



if __name__ == "__main__":  # guard: the watcher child process re-imports this module
    main()

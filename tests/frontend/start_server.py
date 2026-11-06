"""Start a backend for browser tests: temp workspace, fake agents, fixed token, port 8799."""
import logging
import os
import shutil
import signal
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


PREFIX = "waw-e2e-"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_stale_runs() -> None:
    """Remove what earlier runs left behind when they were killed (SIGKILL skips the cleanup below): their tmux
    servers (one per run, each on its own socket) and their temp folders. A run still alive (its OWNER pid
    exists) is left alone, so two e2e servers on different ports do not disturb each other."""
    base = Path(tempfile.gettempdir())
    stale = []
    for d in base.glob(PREFIX + "*"):
        if not d.is_dir():
            continue
        try:
            owner = int((d / "OWNER").read_text().strip())
        except (OSError, ValueError):
            owner = 0
        if owner and _pid_alive(owner):
            continue
        stale.append(d)
    stale_set = {str(d) for d in stale}
    tmux = shutil.which("tmux")
    if tmux:
        # find tmux servers by their -f <tmp>/waw-e2e-XXXX/state/tmux.conf argument
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            args = [a.decode(errors="replace") for a in args if a]
            if not args or Path(args[0]).name != "tmux" or "-L" not in args or "-f" not in args:
                continue
            conf = args[args.index("-f") + 1] if args.index("-f") + 1 < len(args) else ""
            run_dir = str(Path(conf).parent.parent)
            if not Path(run_dir).name.startswith(PREFIX):
                continue
            if run_dir in stale_set or not Path(run_dir).exists():
                sock = args[args.index("-L") + 1]
                subprocess.run([tmux, "-L", sock, "kill-server"], capture_output=True, timeout=5, check=False)
    for d in stale:
        shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    logging.disable(logging.NOTSET)  # conftest silences logging; re-enable for the e2e server log
    reap_stale_runs()
    tmp = Path(tempfile.mkdtemp(prefix=PREFIX))
    (tmp / "OWNER").write_text(str(os.getpid()))
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
    # uvicorn shuts down gracefully on SIGTERM, then re-raises the signal with the handler that was installed
    # before it started. With the default handler that kills the process before `finally` runs; this one exits
    # normally, so the cleanup below always happens.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="info")
    finally:
        # test isolation: stop this run's own tmux server (all terminals the tests created) and drop the temp folder
        try:
            app.state.ctx.terminals.tmux.kill_server()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":  # guard: the watcher child process re-imports this module
    main()

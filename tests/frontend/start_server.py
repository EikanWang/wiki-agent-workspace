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


def add_fake_github(app) -> None:
    """Minimal GitHub OAuth endpoints. The account it signs in as comes from the `fake_gh_user` cookie the test sets
    (default: the allowed `e2e-user`); `fake_gh_cancel=1` simulates clicking Cancel on GitHub."""
    from urllib.parse import urlencode

    from fastapi import Request
    from fastapi.responses import JSONResponse, RedirectResponse
    from starlette.routing import Route

    async def authorize(request: Request):
        q = request.query_params
        if request.cookies.get("fake_gh_cancel") == "1":
            return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'error': 'access_denied', 'state': q['state']})}", status_code=302)
        user = request.cookies.get("fake_gh_user", "e2e-user")
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': user, 'state': q['state']})}", status_code=302)

    async def token(request: Request):
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        if form.get("client_secret") != "e2e-secret":
            return JSONResponse({"error": "incorrect_client_credentials"})
        return JSONResponse({"access_token": f"tok-{form.get('code')}"})

    async def user(request: Request):
        return JSONResponse({"login": request.headers.get("authorization", "").removeprefix("Bearer tok-")})

    # before the SPA catch-all route
    for r in reversed([Route("/fake-github/login/oauth/authorize", authorize), Route("/fake-github/login/oauth/access_token", token, methods=["POST"]),
                       Route("/fake-github/user", user)]):
        app.router.routes.insert(0, r)


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
    # "Sign in with GitHub" against a fake GitHub served by this same server (routes below)
    base = f"http://127.0.0.1:{settings.port}/fake-github"
    settings.github_client_id, settings.github_client_secret = "e2e-client", "e2e-secret"
    settings.github_allowed_users = ["e2e-user"]
    settings.github_oauth_base, settings.github_api_base = base, base
    os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    app = create_app(settings)
    add_fake_github(app)
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

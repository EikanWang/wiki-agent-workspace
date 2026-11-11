"""tmux integration on a dedicated app-owned socket (DESIGN §11).

The user's global tmux configuration is never touched: the app server uses `tmux -L waw-<hash of state dir>`
with its own configuration file written into the state directory.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from ..config import Settings

SESSION_PREFIX = "waw-"


class TmuxError(Exception):
    pass


class Tmux:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.exe = shutil.which("tmux")
        self.socket = settings.tmux_socket_name
        self.conf = settings.tmux_conf_path
        self.inner_term = self._pick_inner_term()
        self.env = self._build_env()
        if self.exe:
            self._write_conf()
            self._reload_conf()

    # ------------------------------------------------------------------ setup
    @property
    def available(self) -> bool:
        return self.exe is not None

    def version(self) -> str | None:
        if not self.exe:
            return None
        try:
            return subprocess.run([self.exe, "-V"], capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _term_exists(name: str) -> bool:
        infocmp = shutil.which("infocmp")
        if not infocmp:
            return name == "screen-256color"  # assume the common default exists
        try:
            return subprocess.run([infocmp, name], capture_output=True, timeout=5, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _pick_inner_term(self) -> str:
        for cand in ("tmux-256color", "screen-256color", "xterm-256color", "screen"):
            if self._term_exists(cand):
                return cand
        return "screen"

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"  # the outer terminal is xterm.js: 256 colours + RGB
        env["COLORTERM"] = "truecolor"
        self.locale_warning: str | None = None
        current = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "").lower()
        if "utf-8" not in current and "utf8" not in current:
            chosen = self._find_utf8_locale()
            if chosen:
                env["LANG"] = chosen
                env.pop("LC_ALL", None)
                self.locale_warning = f"server locale was not UTF-8; terminals use {chosen}"
            else:
                self.locale_warning = "no UTF-8 locale found on the host; non-ASCII output may render incorrectly"
        env.pop("TMUX", None)
        return env

    @staticmethod
    def _find_utf8_locale() -> str | None:
        try:
            out = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=5, check=False).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            return None
        for cand in ("C.UTF-8", "C.utf8", "en_US.UTF-8", "en_US.utf8"):
            if cand in out:
                return cand
        for name in out:
            if "utf8" in name.lower() or "utf-8" in name.lower():
                return name
        return None

    def _write_conf(self) -> None:
        shell = self.settings.shell or os.environ.get("SHELL") or "/bin/sh"
        lines = [
            f"# Written by wiki-agent-workspace. Applies only to the app-owned tmux server (-L {self.socket}).",
            f"set -g history-limit {int(self.settings.tmux_history_limit)}",
            f'set -g default-terminal "{self.inner_term}"',
            # xterm.js advertises true colour; enable RGB for the outer xterm-256color only here.
            'set -as terminal-overrides ",xterm-256color:RGB"',
            f'set -g default-shell "{shell}"',
            "set -g escape-time 10",
            "set -g focus-events on",
            "set -g mouse on",
            # Copy: text selected in tmux (mouse drag / copy mode) and OSC 52 from programs reach the browser
            # clipboard through xterm.js (the frontend handles OSC 52).
            "set -g set-clipboard on",
            'set -as terminal-features ",xterm-256color:clipboard"',
            "set -g window-size latest",
            "setw -g aggressive-resize on",
            "set -g status off",
            "set -g set-titles off",
            "set -g destroy-unattached off",
            "set -g exit-unattached off",
            "set -g detach-on-destroy on",
        ]
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _reload_conf(self) -> None:
        """Apply the config to an already running app tmux server (it outlives app restarts)."""
        try:
            subprocess.run([self.exe, "-L", self.socket, "source-file", str(self.conf)],
                           capture_output=True, timeout=5, env=self.env)
        except (OSError, subprocess.SubprocessError):
            pass  # no server yet: it reads the file when it starts

    # ------------------------------------------------------------------ commands
    def base_args(self) -> list[str]:
        assert self.exe
        return [self.exe, "-L", self.socket, "-f", str(self.conf)]

    def _run(self, args: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
        if not self.exe:
            raise TmuxError("tmux is not installed; install tmux 3.2+ (e.g. `sudo apt install tmux`) to use terminals")
        return subprocess.run(self.base_args() + args, capture_output=True, text=True, timeout=timeout, check=False, env=self.env)

    def list_sessions(self) -> list[str]:
        try:
            p = self._run(["list-sessions", "-F", "#{session_name}"])
        except subprocess.TimeoutExpired:
            raise TmuxError("tmux did not respond")
        if p.returncode != 0:
            if "no server running" in p.stderr or "No such file" in p.stderr or "error connecting" in p.stderr:
                return []
            raise TmuxError(p.stderr.strip() or "tmux list-sessions failed")
        return [ln.strip() for ln in p.stdout.splitlines() if ln.strip().startswith(SESSION_PREFIX)]

    def has_session(self, name: str) -> bool:
        p = self._run(["has-session", "-t", f"={name}"])
        return p.returncode == 0

    def new_session(self, name: str, cwd: Path, cols: int = 120, rows: int = 40, command: list[str] | None = None) -> None:
        """`command` (an argv, e.g. an ssh login to a development host) instead of the default shell."""
        if not cwd.is_dir():
            raise TmuxError(f"initial directory does not exist: {cwd}")
        import shlex

        p = self._run(["new-session", "-d", "-s", name, "-c", str(cwd), "-x", str(cols), "-y", str(rows),
                       *([shlex.join(command)] if command else [])])
        if p.returncode != 0:
            raise TmuxError(p.stderr.strip() or "tmux new-session failed")

    def kill_session(self, name: str) -> None:
        p = self._run(["kill-session", "-t", f"={name}"])
        if p.returncode != 0 and "can't find session" not in p.stderr and "no server running" not in p.stderr:
            raise TmuxError(p.stderr.strip() or "tmux kill-session failed")

    def kill_server(self) -> None:
        """Stop the whole app-owned tmux server (all its sessions). Used by tests and throwaway servers only;
        the app itself never does this: terminals must survive app restarts."""
        if not self.exe:
            return
        try:
            self._run(["kill-server"], timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def capture_history(self, name: str, lines: int = 2000) -> str:
        lines = max(1, min(int(lines), self.settings.tmux_history_limit))
        p = self._run(["capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", f"{name}:"], timeout=20)
        if p.returncode != 0:
            raise TmuxError(p.stderr.strip() or "tmux capture-pane failed")
        # -J keeps trailing spaces of unwrapped lines; drop them (and the empty screen rows) so copies are clean
        return "\n".join(ln.rstrip() for ln in p.stdout.splitlines()).rstrip("\n") + "\n"

    def attach_args(self, name: str) -> list[str]:
        return self.base_args() + ["attach-session", "-t", f"={name}"]

    async def list_sessions_async(self) -> list[str]:
        return await asyncio.to_thread(self.list_sessions)

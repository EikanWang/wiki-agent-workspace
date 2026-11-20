"""tmux on a machine: terminals and commands that outlive the ssh connection and app restarts.

The app runs its own tmux server on the machine (`tmux -L <socket>`; the socket name is this server's own tmux socket
name + "-r", so two app servers using one account never see each other's sessions), with a config file written under
the machine's work_dir. The user's own tmux and its config are never touched.

* A **terminal** is a session there; each browser tab attaches with ``ssh -tt <machine> tmux attach`` over the
  machine's shared connection. When the connection drops, the shell keeps running and Reconnect attaches again.
* A **command run** (build, test) is a session of its own that writes a log file next to it (see development.tasks);
  the app follows the file and reads the exit code when it appears.

Machines without tmux (or with one older than 2.1) keep the previous behaviour: the ssh login itself runs in this
server's tmux, so it ends with the connection.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass

from ..development.targets import SshTarget, TargetError

MIN_VERSION = (2, 1)
RECHECK_MISSING = 300.0  # seconds before a machine without tmux is asked again (it may have been installed)
_INFO: dict[str, tuple[float, "RemoteInfo"]] = {}

_DETECT = r'''
w=$1
case "$w" in "~") w=$HOME;; "~/"*) w="$HOME/${w#"~/"}";; esac
t=$(command -v tmux 2>/dev/null)
if [ -z "$t" ] && [ -x "$HOME/.local/bin/tmux" ]; then t="$HOME/.local/bin/tmux"; fi
if [ -z "$t" ]; then echo "@@none"; exit 0; fi
echo "@@exe $t"
echo "@@version $("$t" -V 2>&1 | head -n 1)"
for x in tmux-256color screen-256color xterm-256color; do
  if infocmp "$x" >/dev/null 2>&1; then echo "@@term $x"; break; fi
done
echo "@@dir $w/tmux"
'''


@dataclass
class RemoteInfo:
    available: bool
    exe: str = ""
    version: str = ""
    conf: str = ""
    reason: str = ""


def remote_socket(local_socket: str) -> str:
    return f"{local_socket}-r"


def parse_version(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def conf_text(socket: str, version: tuple[int, int], term: str, history_limit: int) -> str:
    """The same settings as this server's tmux (terminal.tmux), limited to what the machine's tmux knows."""
    lines = [
        f"# Written by wiki-agent-workspace. Applies only to the app-owned tmux server (-L {socket}).",
        f"set -g history-limit {int(history_limit)}",
        f'set -g default-terminal "{term}"',
        'set -as terminal-overrides ",xterm-256color:RGB"',
        "set -g escape-time 10",
        "set -g mouse on",
        "set -g set-clipboard on",
        "setw -g aggressive-resize on",
        "set -g status off",
        "set -g set-titles off",
        "set -g destroy-unattached off",
        "set -g exit-unattached off",
        "set -g detach-on-destroy on",
    ]
    if version >= (2, 2):
        lines.append("set -g focus-events on")
    if version >= (2, 9):
        lines.append("set -g window-size latest")
    if version >= (3, 2):
        lines.append('set -as terminal-features ",xterm-256color:clipboard"')
    if version >= (3, 3):
        lines.append("set -g allow-passthrough on")  # OSC 52 that programs wrap for tmux (see terminal.tmux)
    return "\n".join(lines) + "\n"


class RemoteTmux:
    """The app's tmux server on one machine. Calls block (ssh round trips): use them from a worker thread."""

    def __init__(self, target: SshTarget, socket: str, history_limit: int, work_dir: str):
        self.target = target
        self.socket = socket
        self.history_limit = history_limit
        self.work_dir = work_dir
        self._info: RemoteInfo | None = None

    @property
    def name(self) -> str:
        return self.target.name

    # ------------------------------------------------------------------ ssh
    def _ssh(self, script_args: list[str], *, timeout: float = 30, stdin: bytes | None = None) -> tuple[int, str, str]:
        argv, _, _ = self.target.spawn_argv(script_args, None, None)
        try:
            p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False,
                               stdin=None if stdin is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise TargetError(f"{self.name} did not answer within {int(timeout)} s", "DEV_HOST_UNREACHABLE")
        except OSError as exc:
            raise TargetError(f"cannot run ssh: {exc}", "DEV_HOST_UNREACHABLE")
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        if p.returncode == 255:
            raise TargetError(f"ssh to {self.target.host.ssh} failed: {err.strip()[-300:] or 'exit 255'}", "DEV_HOST_UNREACHABLE")
        return p.returncode, out, err

    def _key(self) -> str:
        return f"{self.target._home_key()}|{self.socket}|{self.work_dir}"

    # ------------------------------------------------------------------ setup
    def info(self, refresh: bool = False) -> RemoteInfo:
        """Whether the machine has a usable tmux; on first use writes the app's config there. Cached per process (a
        machine without tmux is asked again after a few minutes). Raises TargetError when the machine is unreachable."""
        if self._info is not None and not refresh:
            return self._info
        cached = _INFO.get(self._key())
        if cached and not refresh and (cached[1].available or time.time() - cached[0] < RECHECK_MISSING):
            self._info = cached[1]
            return self._info
        rc, out, err = self._ssh(["sh", "-c", _DETECT, "_", self.work_dir])
        if rc != 0:
            raise TargetError(f"cannot look for tmux on {self.name}: {err.strip()[:300]}")
        found = {}
        for ln in out.splitlines():
            if ln.startswith("@@"):
                k, _, v = ln[2:].partition(" ")
                found[k] = v.strip()
        if "none" in found or "exe" not in found:
            info = RemoteInfo(False, reason=f"tmux is not installed on {self.name}")
        else:
            ver = parse_version(found.get("version", ""))
            if ver is None or ver < MIN_VERSION:
                info = RemoteInfo(False, exe=found["exe"], version=found.get("version", ""),
                                  reason=f"tmux on {self.name} is too old ({found.get('version') or 'unknown version'}; 2.1 or later needed)")
            else:
                conf = f"{found['dir']}/{self.socket}.conf"
                text = conf_text(self.socket, ver, found.get("term") or "screen", self.history_limit)
                q = shlex.quote
                write = (f"mkdir -p {q(found['dir'])} && chmod 700 {q(found['dir'])} && umask 077 && cat > {q(conf + '.tmp')} "
                         f"&& mv -f {q(conf + '.tmp')} {q(conf)} && "
                         # an app tmux server already running there (it outlives app restarts) re-reads it
                         f"{{ {q(found['exe'])} -L {q(self.socket)} source-file {q(conf)} >/dev/null 2>&1 || true; }}")
                rc, _, err = self._ssh(["sh", "-c", write], stdin=text.encode())
                if rc != 0:
                    raise TargetError(f"cannot write the tmux config on {self.name}: {err.strip()[:300]}")
                info = RemoteInfo(True, exe=found["exe"], version=found.get("version", ""), conf=conf)
        _INFO[self._key()] = (time.time(), info)
        self._info = info
        return info

    def available(self) -> bool:
        return self.info().available

    def base(self) -> list[str]:
        i = self.info()
        if not i.available:
            raise TargetError(i.reason, "TMUX_UNAVAILABLE")
        return [i.exe, "-u", "-L", self.socket, "-f", i.conf]

    def _tmux(self, args: list[str], timeout: float = 30) -> tuple[int, str, str]:
        return self._ssh([*self.base(), *args], timeout=timeout)

    # ------------------------------------------------------------------ sessions
    def new_session(self, name: str, cwd: str, command: str | None = None, cols: int = 120, rows: int = 40) -> None:
        rc, _, err = self._tmux(["new-session", "-d", "-s", name, "-c", cwd, "-x", str(cols), "-y", str(rows),
                                 *([command] if command else [])])
        if rc != 0:
            raise TargetError(f"tmux on {self.name}: {err.strip()[:300] or 'new-session failed'}", "TMUX_ERROR")

    def has_session(self, name: str) -> bool:
        rc, _, _ = self._tmux(["has-session", "-t", f"={name}"])
        return rc == 0

    def list_sessions(self) -> list[str]:
        rc, out, err = self._tmux(["list-sessions", "-F", "#{session_name}"])
        if rc != 0:
            if any(w in err for w in ("no server running", "No such file", "error connecting", "server exited")):
                return []
            raise TargetError(f"tmux on {self.name}: {err.strip()[:300]}", "TMUX_ERROR")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def kill_session(self, name: str) -> None:
        rc, _, err = self._tmux(["kill-session", "-t", f"={name}"])
        if rc != 0 and "can't find session" not in err and "no server running" not in err and "error connecting" not in err:
            raise TargetError(f"tmux on {self.name}: {err.strip()[:300]}", "TMUX_ERROR")

    def capture_history(self, name: str, lines: int) -> str:
        lines = max(1, min(int(lines), self.history_limit))
        rc, out, err = self._tmux(["capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", f"{name}:"], timeout=60)
        if rc != 0:
            raise TargetError(f"tmux on {self.name}: {err.strip()[:300] or 'capture-pane failed'}", "TMUX_ERROR")
        return "\n".join(ln.rstrip() for ln in out.splitlines()).rstrip("\n") + "\n"

    def attach_argv(self, name: str) -> list[str]:
        """The local command a browser tab runs (in a pty) to attach to the session on the machine."""
        script = "exec " + shlex.join([*self.base(), "attach-session", "-t", f"={name}"])
        return [*self.target.ssh_prefix(["-tt"]), "sh -c " + shlex.quote(script)]


def for_machine(dev, name: str, local_socket: str, history_limit: int) -> RemoteTmux:
    """The machine's app tmux (raises TargetError for an unknown machine)."""
    from ..development.targets import target_for

    t = target_for(dev, name)
    if not isinstance(t, SshTarget):
        raise TargetError("this server uses its own tmux", "MACHINE_IS_LOCAL")
    return RemoteTmux(t, remote_socket(local_socket), history_limit, t.host.work_dir)

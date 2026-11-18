"""Where a development workspace runs: the app server itself, or a machine reached over SSH (machines.registry).

Everything that touches the worktree — git, the editor (code-server), builds/tests and agent turns — goes through a
Target so it runs next to the files: on the server for `local`, through `ssh <host> sh -c '<script>'` otherwise.
Commands are always argument arrays; for SSH they are quoted with shlex into one POSIX `sh` script (the remote login
shell only ever sees `sh -c <quoted script>`).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DevHost, DevSettings

LOCAL = "local"
_HOMES: dict[str, str] = {}  # $HOME per host (asked once per process)


def sh_path(work_dir: str, sub: str = "") -> str:
    """`work_dir`/`sub` as a shell word on the host (a leading ~ becomes "$HOME")."""
    rest = "/".join(x for x in (work_dir, sub) if x).rstrip("/")
    if work_dir == "~" or work_dir.startswith("~/"):
        tail = rest[1:].lstrip("/")
        return '"$HOME"' + ("/" + shlex.quote(tail) if tail else "")
    return shlex.quote(rest or "/")


def path_prefix(work_dir: str) -> str:
    """Shell that puts the tools machine setup installed (<work_dir>/bin) first on PATH and loads the machine's
    environment (<work_dir>/env.sh: what its setup recipes set up, machines.recipes)."""
    env = sh_path(work_dir, "env.sh")
    return f'PATH={sh_path(work_dir, "bin")}:"$PATH"; export PATH; if [ -f {env} ]; then . {env}; fi; '


class TargetError(Exception):
    def __init__(self, message: str, code: str = "DEV_HOST_FAILED"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RemoteExec:
    """How an agent turn's command line is run on an SSH host instead of locally (see agents.base.SubprocessTurn)."""

    ssh_prefix: list[str]                 # ssh … <destination>
    bins: dict[str, str] = field(default_factory=dict)  # provider name -> executable on the host
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)  # set for the provider on the host (e.g. OPENCODE_CONFIG_CONTENT)
    pre: str = ""        # shell run first (PATH with <work_dir>/bin)
    env_file: str = ""   # a file on the host with forwarded variables, sourced then deleted (machines.envfwd)
    versions: dict[str, str] = field(default_factory=dict)  # provider -> its `--version` there (the last probe)

    def wrap(self, args: list[str], provider: str | None = None) -> list[str]:
        argv = list(args)
        if provider and provider in self.bins:
            argv[0] = self.bins[provider]
        envs = "".join(f"export {k}={shlex.quote(v)}; " for k, v in self.env.items() if k.isidentifier())
        if self.env_file:
            q = shlex.quote(self.env_file)
            envs = f". {q}; rm -f {q}; " + envs
        script = self.pre + envs + (f"cd {shlex.quote(self.cwd)} && " if self.cwd else "") + "exec " + shlex.join(argv)
        return [*self.ssh_prefix, "sh -c " + shlex.quote(script)]


def _git_env() -> dict[str, str]:
    return {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


class Target:
    name: str = LOCAL
    is_local: bool = True

    async def run(self, args: list[str], cwd: str | None = None, timeout: float = 60, stdin: bytes | None = None,
                  env: dict[str, str] | None = None, env_file: str | None = None) -> tuple[int, str, str]:
        argv, lcwd, lenv = self.spawn_argv(args, cwd, env, env_file=env_file)
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=lcwd, env=lenv,
                                                        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            return 127, "", f"{exc.filename or argv[0]}: not found"
        except NotADirectoryError:
            return 127, "", f"{cwd} is not a directory"
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"{args[0]} timed out after {int(timeout)} s"
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def git(self, cwd: str, args: list[str], timeout: float = 120, stdin: bytes | None = None) -> tuple[int, str, str]:
        return await self.run(["git", "-C", cwd, *args], timeout=timeout, env=_git_env(), stdin=stdin)

    def spawn_argv(self, args: list[str], cwd: str | None, env: dict[str, str] | None,
                   ssh_opts: list[str] | None = None,
                   env_file: str | None = None) -> tuple[list[str], str | None, dict[str, str] | None]:
        """(argv, local cwd, local env) that runs `args` in `cwd` on this target (for long-running processes).
        `env_file` (SSH): a file on the host that is sourced and deleted first (machines.envfwd)."""
        raise NotImplementedError

    async def expand(self, path: str) -> str:
        """An absolute path on the host (~ expanded)."""
        raise NotImplementedError

    async def write_text(self, path: str, text: str, mode: int = 0o600) -> None:
        raise NotImplementedError

    async def is_dir(self, path: str) -> bool:
        rc, _, _ = await self.run(["test", "-d", path], timeout=30)
        return rc == 0

    async def mkdir(self, path: str) -> None:
        rc, _, err = await self.run(["mkdir", "-p", "-m", "700", path], timeout=30)
        if rc != 0:
            raise TargetError(f"cannot create {path} on {self.name}: {err.strip()[:300]}")

    def remote_exec(self, cwd: str) -> RemoteExec | None:
        return None

    def label(self) -> str:
        return "this server" if self.is_local else self.name


class LocalTarget(Target):
    name = LOCAL
    is_local = True

    def spawn_argv(self, args, cwd, env, ssh_opts=None, env_file=None):
        return list(args), cwd, ({**os.environ, **env} if env else None)

    async def expand(self, path: str) -> str:
        return str(Path(path).expanduser().resolve())

    async def write_text(self, path: str, text: str, mode: int = 0o600) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, p)


class SshTarget(Target):
    is_local = False

    def __init__(self, host: DevHost, ssh_bin: str = "ssh", dev: DevSettings | None = None, via: list[DevHost] | None = None):
        self.host = host
        self.name = host.name
        self.ssh_bin = ssh_bin
        self.dev = dev if dev is not None else DevSettings(ssh=ssh_bin)
        self.via = via  # None: the host's own bastion chain
        self._home: str | None = _HOMES.get(self._home_key())

    def _home_key(self) -> str:
        return f"{self.ssh_bin}|{self.host.name}|{self.host.ssh}|{self.host.port}|{self.host.user}"

    def ssh_prefix(self, opts: list[str] | None = None) -> list[str]:
        from ..machines.conn import ssh_argv

        return ssh_argv(self.dev, self.host, opts, via=self.via)

    def spawn_argv(self, args, cwd, env, ssh_opts=None, env_file=None):
        script = path_prefix(self.host.work_dir)
        if env_file:
            q = shlex.quote(env_file)
            script += f". {q}; rm -f {q}; "
        if cwd:
            script += f"cd {shlex.quote(cwd)} && "
        script += "exec "
        if env:
            script += "env " + " ".join(shlex.quote(f"{k}={v}") for k, v in env.items()) + " "
        script += shlex.join(list(args))
        return [*self.ssh_prefix(ssh_opts), "sh -c " + shlex.quote(script)], None, None

    async def run(self, args, cwd=None, timeout=60, stdin=None, env=None, env_file=None):
        rc, out, err = await super().run(args, cwd, timeout, stdin, env, env_file)
        if rc == 255:  # ssh's own failure (unreachable, auth)
            raise TargetError(f"ssh to {self.host.ssh} failed: {err.strip()[-300:] or 'exit 255'}", "DEV_HOST_UNREACHABLE")
        return rc, out, err

    async def expand(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            if self._home is None:
                rc, out, err = await self.run(["sh", "-c", 'printf %s "$HOME"'], timeout=30)
                if rc != 0 or not out.startswith("/"):
                    raise TargetError(f"cannot read $HOME on {self.name}: {err.strip()[:200]}")
                self._home = _HOMES[self._home_key()] = out.strip()
            path = self._home + path[1:]
        if not path.startswith("/"):
            raise TargetError(f"give an absolute path on {self.name}", "INVALID_PATH")
        return path.rstrip("/") or "/"

    async def write_text(self, path: str, text: str, mode: int = 0o600) -> None:
        parent = path.rsplit("/", 1)[0] or "/"
        script = f"mkdir -p {shlex.quote(parent)} && umask 077 && cat > {shlex.quote(path + '.tmp')} && chmod {mode:o} {shlex.quote(path + '.tmp')} && mv -f {shlex.quote(path + '.tmp')} {shlex.quote(path)}"
        rc, _, err = await self.run(["sh", "-c", script], timeout=60, stdin=text.encode("utf-8"))
        if rc != 0:
            raise TargetError(f"cannot write {path} on {self.name}: {err.strip()[:300]}")

    def remote_exec(self, cwd: str) -> RemoteExec:
        return RemoteExec(self.ssh_prefix(), {"opencode": self.host.opencode, "codex": self.host.codex}, cwd,
                          pre=path_prefix(self.host.work_dir))


def target_for(dev: DevSettings, host: str | None) -> Target:
    if not host or host == LOCAL:
        return LocalTarget()
    h = dev.host(host)
    if h is None or h.kind == "bastion":
        raise TargetError(f"unknown machine {host!r}; add it in the Machines view or under [[development.hosts]]", "DEV_HOST_UNKNOWN")
    return SshTarget(h, dev.ssh, dev)

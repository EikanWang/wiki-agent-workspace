"""ssh command lines for a host: its own options, the bastion chain in front of it, one shared connection per host.

* **Shared connection** — OpenSSH ControlMaster (`ControlMaster=auto`, `ControlPersist`): the first command to a host
  opens a background master connection that later commands reuse, so a file listing or a `git status` no longer pays
  for a new login. Commands that set up port forwards (`-L`/`-R`, the editor) get a connection of their own: forwards
  added through a shared master would outlive the command.
* **Bastions** — `via = ["b1", "b2"]`: b1 is reached directly, b2 through b1, the host through b2. Each hop is a
  `ProxyCommand` the app builds itself (``ssh … -W %h:%p <bastion>``, nested for longer chains), so every bastion keeps
  its own shared connection too: many machines behind one bastion use a single login to it.
* **Host keys** — hosts added in the app accept only keys confirmed there (`StrictHostKeyChecking=yes`; the app's
  known_hosts file after the user's own ``~/.ssh/known_hosts``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import tempfile
from pathlib import Path

from ..config import DevHost, DevSettings

BASE_OPTS = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=20"]
CONTROL_PERSIST = "600"  # seconds an idle shared connection stays open
FORWARD_FLAGS = ("-L", "-R", "-D", "-W")
USER_KNOWN_HOSTS = "~/.ssh/known_hosts"


def control_dir_for(state_dir: Path) -> Path:
    """Where the shared-connection sockets live. Unix socket paths are short (~104 bytes, and ssh appends a temporary
    suffix while it creates one): a deep data folder falls back to a private folder under the system temp dir."""
    d = Path(state_dir) / "ssh" / "cm"
    if len(str(d)) > 60:
        tag = hashlib.sha1(str(Path(state_dir).resolve()).encode()).hexdigest()[:10]
        d = Path(tempfile.gettempdir()) / f"waw-ssh-{os.getuid()}-{tag}"
    return d


def ensure_dirs(dev: DevSettings) -> None:
    for d in (dev.control_dir, dev.known_hosts.parent if dev.known_hosts else None):
        if d is None:
            continue
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = d.stat()
        if st.st_uid != os.getuid():
            raise RuntimeError(f"{d} belongs to another user")
        os.chmod(d, 0o700)


def destination(h: DevHost) -> str:
    return h.ssh


def chain(dev: DevSettings, h: DevHost) -> list[DevHost]:
    """The bastions in front of `h`, first hop first (unknown names are skipped; bastions have no chain of their own)."""
    if h.kind == "bastion":
        return []
    out = []
    for name in h.via:
        b = dev.host(name)
        if b is not None and b.kind == "bastion":
            out.append(b)
    return out


def _identity(dev: DevSettings, h: DevHost, via: list[DevHost]) -> str:
    return json.dumps([h.ssh, h.port, h.user, h.identity_file, sorted(h.options.items()), h.ssh_args, h.strict_host_keys,
                       [(b.ssh, b.port, b.user, b.identity_file, sorted(b.options.items()), b.ssh_args) for b in via]])


def control_path(dev: DevSettings, h: DevHost, via: list[DevHost] | None = None) -> str | None:
    if dev.control_dir is None:
        return None
    via = chain(dev, h) if via is None else via
    return str(dev.control_dir / hashlib.sha1(_identity(dev, h, via).encode()).hexdigest()[:16])


def _value(v: str) -> str:
    return f'"{v}"' if any(c.isspace() for c in v) else v


def host_opts(dev: DevSettings, h: DevHost) -> list[str]:
    """The host's own options (port, user, key, allowlisted -o, host-key policy, free-form ssh_args of the config)."""
    out: list[str] = []
    if h.port:
        out += ["-p", str(h.port)]
    if h.user:
        out += ["-l", h.user]
    if h.identity_file:
        out += ["-i", h.identity_file, "-o", "IdentitiesOnly=yes"]
    for k, v in h.options.items():
        out += ["-o", f"{k}={v}"]
    if h.strict_host_keys and dev.known_hosts is not None:
        out += ["-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={USER_KNOWN_HOSTS} {_value(str(dev.known_hosts))}"]
    out += list(h.ssh_args)
    return out


def mux_opts(dev: DevSettings, h: DevHost, via: list[DevHost], mux: bool) -> list[str]:
    path = control_path(dev, h, via) if mux else None
    if path is None:
        return ["-o", "ControlPath=none"] if dev.control_dir is not None else []
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={path}", "-o", f"ControlPersist={CONTROL_PERSIST}"]


def proxy_command(dev: DevSettings, via: list[DevHost]) -> str | None:
    """The ProxyCommand that reaches the last bastion of `via` (through the ones before it), or None for a direct hop.
    Tokens (%h %p) of an inner command are escaped once per nesting level, so each level's ssh expands only its own."""
    if not via:
        return None
    last, before = via[-1], via[:-1]
    argv = [dev.ssh, *BASE_OPTS, *host_opts(dev, last), *mux_opts(dev, last, before, True)]
    inner = proxy_command(dev, before)
    if inner:
        argv += ["-o", "ProxyCommand=" + inner.replace("%", "%%")]
    argv += ["-W", "%h:%p", "--", destination(last)]
    return shlex.join(argv)


def ssh_argv(dev: DevSettings, h: DevHost, extra: list[str] | None = None, *, batch: bool = True, mux: bool | None = None,
             via: list[DevHost] | None = None) -> list[str]:
    """`ssh <options> -- <destination>` for `h`; append the remote command. `via` overrides the bastion chain (checks
    of one hop at a time)."""
    extra = list(extra or [])
    via = chain(dev, h) if via is None else via
    if mux is None:
        mux = not any(o in FORWARD_FLAGS for o in extra)
    argv = [dev.ssh, *(BASE_OPTS if batch else BASE_OPTS[2:]), *host_opts(dev, h), *mux_opts(dev, h, via, mux)]
    pc = proxy_command(dev, via)
    if pc:
        argv += ["-o", "ProxyCommand=" + pc]
    return [*argv, *extra, "--", destination(h)]


def preview(dev: DevSettings, h: DevHost) -> str:
    """The command line a user could paste to log in the same way (shared-connection options left out)."""
    argv = [dev.ssh, *host_opts(dev, h)]
    pc = proxy_command(dev, chain(dev, h))
    if pc:
        argv += ["-o", "ProxyCommand=" + pc]
    return shlex.join([*argv, destination(h)])


async def close_master(dev: DevSettings, h: DevHost) -> None:
    """Close the host's shared connection (after its settings changed or it was removed); best effort."""
    path = control_path(dev, h)
    if not path or not os.path.exists(path):
        return
    try:
        proc = await asyncio.create_subprocess_exec(dev.ssh, "-o", f"ControlPath={path}", "-O", "exit", "--", destination(h),
                                                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (OSError, TimeoutError):
        pass

"""This server's SSH keys (the ones the app logs in with) and installing one on a machine or bastion.

Installing runs `ssh-copy-id` in one of the app's terminals: the user types the password (and any one-time code)
straight into that terminal — the app never sees, stores or logs it. It only offers the command and checks the
connection again afterwards.
"""

from __future__ import annotations

import asyncio
import getpass
import shlex
import socket
from pathlib import Path
from typing import Any

from ..config import DevHost, DevSettings
from ..errors import ApiError, bad_request, conflict
from . import conn
from .hostkeys import fingerprint

KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")


def ssh_dir() -> Path:
    return Path.home() / ".ssh"


def list_keys() -> list[dict[str, Any]]:
    out = []
    d = ssh_dir()
    if not d.is_dir():
        return out
    for pub in sorted(d.glob("*.pub")):
        try:
            parts = pub.read_text("utf-8").strip().split(None, 2)
        except (OSError, UnicodeDecodeError):
            continue
        if len(parts) < 2 or parts[0] not in KEY_TYPES:
            continue
        priv = pub.with_suffix("")
        out.append({"public_key_file": str(pub), "identity_file": str(priv) if priv.is_file() else None, "type": parts[0],
                    "fingerprint": fingerprint(parts[1]), "comment": parts[2] if len(parts) > 2 else "",
                    "public_key": " ".join(parts[:2])})
    out.sort(key=lambda k: (k["type"] != "ssh-ed25519", k["public_key_file"]))
    return out


async def generate_key(dev: DevSettings) -> dict[str, Any]:
    d = ssh_dir()
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / "id_ed25519"
    if path.exists() or path.with_suffix(".pub").exists():
        raise conflict("SSH_KEY_EXISTS", f"{path} exists already")
    comment = f"{getpass.getuser()}@{socket.gethostname()} (wiki-agent-workspace)"
    proc = await asyncio.create_subprocess_exec(dev.ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path),
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise ApiError(500, "SSH_KEYGEN_FAILED", f"ssh-keygen failed: {err.decode(errors='replace').strip()[:300]}")
    return next(k for k in list_keys() if k["public_key_file"] == str(path.with_suffix(".pub")))


def _copy_id_opts(dev: DevSettings, h: DevHost, via: list[DevHost]) -> list[str]:
    """ssh options ssh-copy-id understands (-p and -o only), for a login that may ask for a password."""
    out: list[str] = []
    if h.port:
        out += ["-p", str(h.port)]
    if h.identity_file:
        out += ["-o", f"IdentityFile={h.identity_file}"]
    for k, v in h.options.items():
        out += ["-o", f"{k}={v}"]
    if h.strict_host_keys and dev.known_hosts is not None:
        out += ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={conn.USER_KNOWN_HOSTS} {dev.known_hosts}"]
    args = list(h.ssh_args)
    i = 0
    while i + 1 < len(args) and args[i] in ("-o", "-p"):
        out += args[i:i + 2]
        i += 2
    out += ["-o", "ControlPath=none"]
    pc = conn.proxy_command(dev, via)
    if pc:
        out += ["-o", "ProxyCommand=" + pc]
    return out


def copy_id_argv(dev: DevSettings, h: DevHost, via: list[DevHost], public_key_file: str) -> list[str]:
    dest = conn.destination(h)
    if h.user and "@" not in dest:
        dest = f"{h.user}@{dest}"
    return [dev.ssh_copy_id, "-i", public_key_file, *_copy_id_opts(dev, h, via), dest]


def install_command(dev: DevSettings, h: DevHost, via: list[DevHost], public_key_file: str) -> list[str]:
    """The terminal's command: ssh-copy-id, then the result stays on screen until Enter."""
    pub = Path(public_key_file).expanduser()
    if pub.suffix != ".pub" or not pub.is_file() or pub.parent.resolve() != ssh_dir().resolve():
        raise bad_request("pick one of the public keys in ~/.ssh", "INVALID_KEY")
    argv = copy_id_argv(dev, h, via, str(pub))
    script = (f"echo {shlex.quote('Installing ' + pub.name + ' on ' + h.name + ' — type the password when asked.')}; echo; "
              f"{shlex.join(argv)}; rc=$?; echo; "
              "if [ $rc -eq 0 ]; then echo 'Done. Go back to the machine and check it again.'; "
              "else echo \"ssh-copy-id failed (exit $rc).\"; fi; echo 'Press Enter to close this terminal.'; read _")
    return ["sh", "-c", script]


def default_public_key() -> str | None:
    keys = list_keys()
    return keys[0]["public_key_file"] if keys else None

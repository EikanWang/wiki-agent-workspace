"""What a machine gets from this server's environment (opt-in per machine) and how it gets there.

* **Variables** (`forward_env`): those `[agents] env_script` sets — API tokens such as AWS_BEARER_TOKEN_BEDROCK — or only
  the names in `env_vars` (taken from the script, else from this server's environment).
* **Proxy** (`forward_proxy`): http(s)_proxy, all_proxy, no_proxy of this server (no_proxy gains localhost).
* **CA bundle** (`ca_bundle`, a file on this server): copied to <work_dir>/ca-extra.pem; <work_dir>/ca.pem is the
  machine's own system bundle plus it, so SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / GIT_SSL_CAINFO keep
  trusting public sites; NODE_EXTRA_CA_CERTS gets the extra file only.

Values never appear on a command line (other users of the machine could read them in the process list): they are
written over ssh's stdin into a 0600 file under <work_dir>/run, which the command sources and deletes before it
starts. Files left behind by a command that never started are removed after a few minutes. Forwarded values of 8 or
more characters are shown as **** in the logs the app keeps of command runs.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets as _secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DevHost
from ..development.targets import SshTarget, TargetError, sh_path

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
LOCAL_NO_PROXY = ("localhost", "127.0.0.1", "::1")
CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")
# set by env_script for this server, meaningless (or harmful) elsewhere: only sent when named in env_vars
LOCAL_ONLY = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|TERM|PWD|OLDPWD|SHLVL|_|TMPDIR|DISPLAY|WAYLAND_DISPLAY|"
                        r"XDG_[A-Z_]+|SSH_[A-Z_]+|DBUS_[A-Z_]+|LD_[A-Z_]+|PYTHONPATH|VIRTUAL_ENV|CONDA_[A-Z_]+)$")
MIN_SECRET = 8
STALE_MINUTES = 10
_CA_SENT: dict[str, str] = {}  # host key -> sha256 of the CA file last copied there (this process)


@dataclass
class Forwarded:
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)  # values to mask in logs
    ca: Path | None = None

    def __bool__(self) -> bool:
        return bool(self.env or self.ca)

    def names(self) -> list[str]:
        return sorted(self.env) + ([*CA_VARS, "NODE_EXTRA_CA_CERTS"] if self.ca else [])


def collect(h: DevHost) -> Forwarded:
    """What `h` gets now (the env script is re-read when it changed, so rotated tokens apply)."""
    from ..agents.base import _script_vars

    f = Forwarded()
    if h.forward_env:
        script = _script_vars()
        if h.env_vars:
            for n in h.env_vars:
                v = script.get(n, os.environ.get(n))
                if v is not None:
                    f.env[n] = v
        else:
            f.env.update({k: v for k, v in script.items() if not LOCAL_ONLY.match(k)})
        f.secrets += [v for v in f.env.values() if len(v) >= MIN_SECRET]
    if h.forward_proxy:
        for n in PROXY_VARS:
            v = os.environ.get(n)
            if v:
                f.env[n] = v
                if re.search(r"://[^/@\s]+@", v):  # credentials in the URL
                    f.secrets.append(v)
        for n in ("no_proxy", "NO_PROXY"):
            if n in f.env or any(p in f.env for p in PROXY_VARS):
                have = [x.strip() for x in f.env.get(n, "").split(",") if x.strip()]
                f.env[n] = ",".join(have + [x for x in LOCAL_NO_PROXY if x not in have])
    if h.ca_bundle:
        p = Path(h.ca_bundle).expanduser()
        if p.is_file():
            f.ca = p
    return f


def file_text(h: DevHost, f: Forwarded) -> str:
    lines = ["# written by wiki-agent-workspace; sourced and deleted by the command it was made for"]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in f.env.items() if k.isidentifier()]
    if f.ca:
        ca, extra = sh_path(h.work_dir, "ca.pem"), sh_path(h.work_dir, "ca-extra.pem")
        lines.append(f"if [ -f {ca} ]; then")
        lines += [f"  export {v}={ca}" for v in CA_VARS]
        lines.append(f"  export NODE_EXTRA_CA_CERTS={extra}")
        lines.append("fi")
    return "\n".join(lines) + "\n"


def login_shell(h: DevHost, env_file: str | None, argv: list[str] | None = None) -> str:
    """The command of a terminal on the machine: its login shell with <work_dir>/bin first on PATH and the forwarded
    variables loaded. With `argv`: that runs first (in the login shell), then the shell."""
    from ..development.targets import path_prefix

    pre = path_prefix(h.work_dir) + (source_snippet(env_file) if env_file else "")
    if argv:
        run = path_prefix(h.work_dir) + shlex.join(argv) + '; exec "${SHELL:-sh}" -l'
        return shlex.join(["sh", "-c", pre + 'exec "${SHELL:-sh}" -l -c "$0"', run])
    return shlex.join(["sh", "-c", pre + 'exec "${SHELL:-sh}" -l'])


def source_snippet(path: str) -> str:
    """Shell that loads the file and removes it (before anything else runs)."""
    q = shlex.quote(path)
    return f". {q}; rm -f {q}; "


def _push_script(h: DevHost, name: str) -> str:
    d = sh_path(h.work_dir, "run")
    return (f'd={d}; mkdir -p "$d" && chmod 700 "$d" && umask 077 && '
            f'find "$d" -maxdepth 1 -name "env-*.sh" -mmin +{STALE_MINUTES} -exec rm -f {{}} + 2>/dev/null; '
            f'cat > "$d/{name}" && chmod 600 "$d/{name}" && printf %s "$d/{name}"')


_CA_SCRIPT = r'''mkdir -p "$w" && chmod 700 "$w" && umask 022 && cat > "$w/ca-extra.pem.tmp" && mv -f "$w/ca-extra.pem.tmp" "$w/ca-extra.pem" || exit 1
sys=; for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/ca-bundle.pem /etc/ssl/cert.pem /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; do
  if [ -r "$f" ]; then sys=$f; break; fi
done
{ if [ -n "$sys" ]; then cat "$sys"; echo; fi; cat "$w/ca-extra.pem"; } > "$w/ca.pem.tmp" && mv -f "$w/ca.pem.tmp" "$w/ca.pem"
'''


def _ca_key(t: SshTarget) -> str:
    return f"{t._home_key()}|{t.host.work_dir}"


def _ssh_sync(t: SshTarget, args: list[str], stdin: bytes, timeout: float = 60) -> str:
    argv, _, _ = t.spawn_argv(args, None, None)
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TargetError(f"cannot reach {t.name}: {exc}", "DEV_HOST_UNREACHABLE")
    if p.returncode == 255:
        raise TargetError(f"ssh to {t.host.ssh} failed: {p.stderr.decode('utf-8', 'replace').strip()[-300:]}", "DEV_HOST_UNREACHABLE")
    if p.returncode != 0:
        raise TargetError(f"cannot write the environment file on {t.name}: {p.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return p.stdout.decode("utf-8", "replace").strip()


async def _ssh(t: SshTarget, args: list[str], stdin: bytes, timeout: float = 60) -> str:
    rc, out, err = await t.run(args, timeout=timeout, stdin=stdin)
    if rc != 0:
        raise TargetError(f"cannot write the environment file on {t.name}: {err.strip()[:300]}")
    return out.strip()


def _ca_args(h: DevHost) -> list[str]:
    return ["sh", "-c", f"w={sh_path(h.work_dir)}; " + _CA_SCRIPT]


async def _send_ca(t: SshTarget, f: Forwarded) -> None:
    if not f.ca:
        return
    data = f.ca.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if _CA_SENT.get(_ca_key(t)) != sha:
        rc, _, err = await t.run(_ca_args(t.host), timeout=60, stdin=data)
        if rc != 0:
            raise TargetError(f"cannot copy the CA bundle to {t.name}: {err.strip()[:300]}")
        _CA_SENT[_ca_key(t)] = sha


async def prepare(t: SshTarget, f: Forwarded | None = None) -> str | None:
    """The CA bundle copied there (if any) and the text of the environment file (None: nothing to forward), for
    callers that write the file themselves (a run folder)."""
    f = collect(t.host) if f is None else f
    if not f:
        return None
    await _send_ca(t, f)
    return file_text(t.host, f)


async def push(t: SshTarget, f: Forwarded | None = None) -> str | None:
    """Write the machine's forwarded environment there; the path of the file (None: nothing to forward)."""
    f = collect(t.host) if f is None else f
    if not f:
        return None
    await _send_ca(t, f)
    name = f"env-{_secrets.token_hex(8)}.sh"
    return await _ssh(t, ["sh", "-c", _push_script(t.host, name)], file_text(t.host, f).encode())


def push_sync(t: SshTarget, f: Forwarded | None = None) -> str | None:
    """`push` for worker threads (remote tmux)."""
    f = collect(t.host) if f is None else f
    if not f:
        return None
    if f.ca:
        data = f.ca.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if _CA_SENT.get(_ca_key(t)) != sha:
            _ssh_sync(t, _ca_args(t.host), data)
            _CA_SENT[_ca_key(t)] = sha
    name = f"env-{_secrets.token_hex(8)}.sh"
    return _ssh_sync(t, ["sh", "-c", _push_script(t.host, name)], file_text(t.host, f).encode())


# ---------------------------------------------------------------------------------------------- masking
def secrets_for(h: DevHost | None) -> list[str]:
    """Values to mask in the logs of commands run on `h` (what it would get now)."""
    if h is None or not (h.forward_env or h.forward_proxy):
        return []
    return collect(h).secrets


def mask(text: str, secrets: list[str]) -> str:
    for s in sorted(set(secrets), key=len, reverse=True):
        if s:
            text = text.replace(s, "****")
    return text


def mask_bytes(data: bytes, secrets: list[str], *, final: bool) -> tuple[bytes, int]:
    """(masked data, bytes consumed). Unless `final`, a tail that could be the start of a secret is held back (the
    next read, with more output, masks it whole)."""
    keep = len(data)
    if not final:
        for s in secrets:
            b = s.encode()
            for k in range(min(len(b) - 1, len(data)), 0, -1):
                if data.endswith(b[:k]):
                    keep = min(keep, len(data) - k)
                    break
    out = data[:keep]
    for s in sorted(set(secrets), key=len, reverse=True):
        if s:
            out = out.replace(s.encode(), b"****")
    return out, keep

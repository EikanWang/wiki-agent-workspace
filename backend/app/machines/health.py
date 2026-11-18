"""Connection checks, one hop at a time: each bastion of the chain, then the machine through them. The first failing
hop is reported with the reason ssh gave (unreachable, login refused, unknown or changed host key, DNS, timeout), so a
broken path says where it broke."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..config import DevHost, DevSettings
from ..development.targets import SshTarget, Target
from . import conn

TIMEOUT = 45

# (status, substrings of ssh's error output, explanation), checked in order
CLASSES: list[tuple[str, tuple[str, ...], str]] = [
    ("hostkey_changed", ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key for", "has changed and you have requested strict"),
     "the host key changed since it was trusted: confirm the new key only if you know why it changed"),
    ("hostkey_unknown", ("Host key verification failed", "No ED25519 host key is known", "No RSA host key is known",
                         "No ECDSA host key is known", "host key is known for"),
     "the host key is not trusted yet: check its fingerprint and trust it"),
    ("auth_failed", ("Permission denied", "Too many authentication failures", "no matching host key type",
                     "Authentication failed"),
     "the login was refused: install your public key on it (or pick the right key)"),
    ("dns", ("Could not resolve hostname", "Name or service not known", "nodename nor servname", "Temporary failure in name resolution"),
     "the host name does not resolve from here"),
    ("timeout", ("Connection timed out", "Operation timed out", "timed out during banner exchange", "Connection timed out during"),
     "no answer: the host is down, or a firewall drops the connection"),
    ("refused", ("Connection refused",), "nothing listens on the ssh port"),
    ("unreachable", ("No route to host", "Network is unreachable", "Connection closed by", "Connection reset by",
                     "kex_exchange_identification", "UNKNOWN port"),
     "the connection could not be made"),
]


def classify(rc: int, err: str) -> tuple[str, str]:
    for status, needles, why in CLASSES:
        if any(n.lower() in err.lower() for n in needles):
            return status, why
    if rc == 124:
        return "timeout", "no answer in time"
    return "failed", "ssh failed"


def _last_lines(err: str, n: int = 4) -> str:
    lines = [ln for ln in err.strip().splitlines() if ln.strip() and not ln.startswith(("Warning: Permanently added",))]
    return "\n".join(lines[-n:])[-600:]


async def check_hop(dev: DevSettings, h: DevHost, via: list[DevHost]) -> dict[str, Any]:
    t = SshTarget(h, dev.ssh, dev, via=via)
    started = time.monotonic()
    rc, out, err = await Target.run(t, ["sh", "-c", "echo waw-ok"], timeout=TIMEOUT)
    ms = int((time.monotonic() - started) * 1000)
    hop = {"name": h.name, "kind": h.kind, "ssh": h.ssh, "ms": ms}
    if rc == 0 and "waw-ok" in out:
        return {**hop, "ok": True, "status": "online", "message": None}
    if rc != 255 and rc != 124:
        # ssh logged in, but the remote shell could not run a command
        return {**hop, "ok": False, "status": "shell_failed", "message": f"logged in, but the remote shell failed (exit {rc}): {_last_lines(err or out)}"}
    status, why = classify(rc, err)
    return {**hop, "ok": False, "status": status, "message": why, "error": _last_lines(err)}


async def check(dev: DevSettings, h: DevHost) -> dict[str, Any]:
    """Every hop of the path to `h` in order; hops after the first failure are skipped."""
    path = [*conn.chain(dev, h), h]
    missing = [v for v in h.via if v not in {b.name for b in path}]
    hops: list[dict[str, Any]] = []
    failed: dict[str, Any] | None = None
    if missing:
        failed = {"name": missing[0], "kind": "bastion", "ssh": None, "ok": False, "status": "unknown_bastion",
                  "message": f"bastion {missing[0]} is not defined", "ms": 0}
        hops.append(failed)
    for i, hop in enumerate(path):
        if failed is not None:
            hops.append({"name": hop.name, "kind": hop.kind, "ssh": hop.ssh, "ok": False, "status": "skipped", "message": None, "ms": 0})
            continue
        r = await check_hop(dev, hop, path[:i])
        hops.append(r)
        if not r["ok"]:
            failed = r
    status = "online" if failed is None else failed["status"]
    where = None if failed is None else ("the machine" if failed["name"] == h.name else f"bastion {failed['name']}")
    return {"ok": failed is None, "status": status, "failed_at": failed["name"] if failed else None,
            "summary": "reachable" if failed is None else f"{where}: {failed['message']}", "hops": hops,
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

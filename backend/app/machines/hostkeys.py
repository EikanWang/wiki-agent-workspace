"""Host keys of each hop: scan them (a machine behind bastions is scanned from the last bastion, where its host name
resolves), show their SHA256 fingerprints and whether they are trusted already, and trust the ones the user confirmed
— written to the app's own known_hosts file, which machines added in the app check strictly."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from ..config import DevHost, DevSettings
from ..development.targets import SshTarget, Target
from ..errors import ApiError, conflict
from . import conn

KEYSCAN_TIMEOUT = 10
# ssh-keyscan makes one connection per key type, none of which logs in; OpenSSH >= 9.8 (PerSourcePenalties) shuts
# out an address after ~15 of those. Only the types ssh uses for host keys, and the keys the user was shown are
# reused when they confirm them (within SCAN_KEEP seconds) instead of scanning again.
KEYSCAN_TYPES = "ed25519,ecdsa,rsa"
SCAN_KEEP = 600
_SCANS: dict[tuple, tuple[float, list[tuple[str, str]], str]] = {}


async def _local(argv: list[str], timeout: float = 30, stdin: bytes | None = None) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"{argv[0]} timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def resolve(dev: DevSettings, h: DevHost) -> dict[str, Any]:
    """Host name, port, user and host-key alias ssh will use for `h` (`ssh -G`, so ~/.ssh/config aliases count)."""
    rc, out, _ = await _local([dev.ssh, "-G", *conn.host_opts(dev, h), "--", conn.destination(h)])
    cfg: dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            k, _, v = line.partition(" ")
            cfg.setdefault(k.lower(), v.strip())
    dest = conn.destination(h)
    user, _, host = dest.rpartition("@")
    hostname = cfg.get("hostname") or host
    port = int(cfg.get("port") or h.port or 22)
    alias = cfg.get("hostkeyalias") or h.options.get("HostKeyAlias") or ""
    if alias.lower() == "none":
        alias = ""
    return {"hostname": hostname, "port": port, "user": cfg.get("user") or h.user or user or None,
            "proxyjump": (cfg.get("proxyjump") or "") if (cfg.get("proxyjump") or "").lower() != "none" else "",
            "known_name": alias or known_name(hostname, port)}  # (ssh looks a HostKeyAlias up as it is, without the port)


def known_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def fingerprint(key_b64: str) -> str:
    try:
        raw = base64.b64decode(key_b64.encode(), validate=True)
    except ValueError:
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def parse_keys(text: str) -> list[tuple[str, str]]:
    """(type, base64 key) of known_hosts / ssh-keyscan lines (comments, markers and duplicates skipped)."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0].startswith("@"):
            if parts[0] != "@cert-authority":
                continue
            parts = parts[1:]
        if len(parts) >= 3 and (parts[1], parts[2]) not in out and fingerprint(parts[2]):
            out.append((parts[1], parts[2]))
    return out


def known_files(dev: DevSettings, h: DevHost) -> list[tuple[str, Path]]:
    files = [("user", Path(conn.USER_KNOWN_HOSTS).expanduser())]
    if h.strict_host_keys and dev.known_hosts is not None:
        files.append(("app", dev.known_hosts))
    return files


async def known_keys(dev: DevSettings, h: DevHost, name: str) -> list[dict[str, str]]:
    out = []
    for where, f in known_files(dev, h):
        if not f.is_file():
            continue
        rc, text, _ = await _local([dev.ssh_keygen, "-F", name, "-f", str(f)])
        if rc == 0:
            out += [{"type": t, "key": k, "file": where} for t, k in parse_keys(text)]
    return out


async def scan(dev: DevSettings, h: DevHost, via: list[DevHost], reuse: bool = False) -> dict[str, Any]:
    """The hop's host keys and how each compares with the trusted ones (`reuse`: the keys scanned last, if recent)."""
    r = await resolve(dev, h)
    ck = (tuple(x.name for x in via), r["hostname"], r["port"])
    kept = _SCANS.get(ck)
    if reuse and kept and time.monotonic() - kept[0] < SCAN_KEEP:
        keys, where = kept[1], kept[2]
    else:
        argv = [dev.ssh_keyscan, "-T", str(KEYSCAN_TIMEOUT), "-t", KEYSCAN_TYPES, "-p", str(r["port"]), r["hostname"]]
        if via:
            t = SshTarget(via[-1], dev.ssh, dev, via=via[:-1])
            rc, out, err = await Target.run(t, argv, timeout=KEYSCAN_TIMEOUT * 4)
            where = f"from bastion {via[-1].name}"
        else:
            rc, out, err = await _local(argv, timeout=KEYSCAN_TIMEOUT * 4)
            where = "from this server"
        keys = parse_keys(out)
        if not keys:
            msg = (err.strip().splitlines() or [f"exit {rc}"])[-1][:300]
            raise ApiError(502, "HOSTKEY_SCAN_FAILED", f"could not read the host keys of {r['hostname']}:{r['port']} {where}: {msg}")
        _SCANS[ck] = (time.monotonic(), keys, where)
    known = await known_keys(dev, h, r["known_name"])
    rows = []
    for typ, key in keys:
        same = [k for k in known if k["type"] == typ]
        state = "trusted" if any(k["key"] == key for k in same) else ("changed" if same else "new")
        rows.append({"type": typ, "fingerprint": fingerprint(key), "state": state,
                     "files": sorted({k["file"] for k in same})})
    if any(x["state"] == "trusted" for x in rows):
        status = "trusted"
    elif any(x["state"] == "changed" for x in rows):
        status = "changed"
    else:
        status = "unknown"
    return {"name": h.name, "hostname": r["hostname"], "port": r["port"], "known_name": r["known_name"], "scanned": where,
            "status": status, "keys": rows, "can_trust": h.strict_host_keys and dev.known_hosts is not None, "_raw": keys}


def hop_path(dev: DevSettings, h: DevHost) -> list[DevHost]:
    return [*conn.chain(dev, h), h]


async def scan_path(dev: DevSettings, h: DevHost) -> list[dict[str, Any]]:
    path = hop_path(dev, h)
    out = []
    for i, hop in enumerate(path):
        try:
            r = await scan(dev, hop, path[:i])
            r.pop("_raw", None)
        except ApiError as exc:
            r = {"name": hop.name, "status": "scan_failed", "error": exc.message, "keys": [],
                 "can_trust": hop.strict_host_keys and dev.known_hosts is not None}
        out.append(r)
    return out


async def trust(dev: DevSettings, h: DevHost, hop_name: str, fingerprints: list[str]) -> dict[str, Any]:
    """Trust the hop's scanned keys whose fingerprints the user confirmed (the keys they were shown, or scanned
    again when that was long ago: they must match)."""
    path = hop_path(dev, h)
    idx = next((i for i, x in enumerate(path) if x.name == hop_name), None)
    if idx is None:
        raise ApiError(400, "INVALID_HOP", f"{hop_name} is not on the path to {h.name}")
    hop = path[idx]
    if not hop.strict_host_keys or dev.known_hosts is None:
        raise ApiError(409, "MACHINE_READ_ONLY", f"{hop.name} comes from the config file and uses your own ssh settings: "
                                                 f"log in once with `ssh {hop.ssh}` in a terminal to accept its key")
    r = await scan(dev, hop, path[:idx], reuse=True)
    chosen = [(t, k) for t, k in r["_raw"] if fingerprint(k) in set(fingerprints)]
    if not chosen or len(chosen) != len(set(fingerprints)):
        raise conflict("HOSTKEY_MISMATCH", "the host now presents other keys than the ones you confirmed; check again")
    # (a different key of the same type in ~/.ssh/known_hosts does not block the app: ssh accepts a host when any of
    # its known_hosts files has the presented key)
    f = dev.known_hosts
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if f.is_file() and any(x["state"] == "changed" for x in r["keys"]):
        await _local([dev.ssh_keygen, "-R", r["known_name"], "-f", str(f)])
        Path(str(f) + ".old").unlink(missing_ok=True)
    existing = f.read_text("utf-8") if f.is_file() else ""
    lines = [f"{r['known_name']} {t} {k}" for t, k in chosen if f"{r['known_name']} {t} {k}" not in existing.splitlines()]
    if lines:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(lines) + "\n")
    os.chmod(f, 0o600)
    r = await scan(dev, hop, path[:idx], reuse=True)
    r.pop("_raw", None)
    return r

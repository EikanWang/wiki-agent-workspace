"""The list of machines and bastions: ``[[development.hosts]]`` of the config file (read-only here) plus those added in
the Machines view (stored in the `machines` table). Both are rows of that table — config hosts are mirrored at
startup so their status and probe results are kept too — and both reach the rest of the app as `DevHost`s
(`DevSettings.host(name)`), so PR Development, terminals and agent turns use them alike.

Machines added in the app can only use ssh options from an allowlist: nothing that runs a local command
(ProxyCommand, LocalCommand, KnownHostsCommand, Match exec…) or changes how host keys are checked.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import DevHost, DevSettings
from ..db import iso
from ..errors import ApiError, bad_request, conflict, not_found
from ..models import DevWorkspace, Machine
from . import conn

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
DEST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:%\[\]-]{0,252}$")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
VALUE_RE = re.compile(r"^[A-Za-z0-9_.,:@+/=~%-]{1,512}$")
MAX_VIA = 4
DEFAULT_WORK_DIR = "~/.cache/wiki-agent-workspace"

# ssh -o options a machine added in the app may set (case-insensitive; stored with this spelling)
ALLOWED_OPTIONS = {k.lower(): k for k in (
    "AddressFamily", "CertificateFile", "Ciphers", "Compression", "ConnectionAttempts", "ConnectTimeout", "ForwardAgent",
    "HostKeyAlgorithms", "HostKeyAlias", "HostName", "IdentitiesOnly", "IPQoS", "KexAlgorithms", "LogLevel", "MACs",
    "PreferredAuthentications", "PubkeyAcceptedAlgorithms", "PubkeyAcceptedKeyTypes", "RekeyLimit", "ServerAliveCountMax",
    "ServerAliveInterval", "TCPKeepAlive",
)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def to_host(row: Machine) -> DevHost:
    return DevHost(name=row.name, ssh=row.ssh, work_dir=row.work_dir or DEFAULT_WORK_DIR, code_server=row.code_server or "code-server",
                   opencode=row.opencode or "opencode", codex=row.codex or "codex", kind=row.kind,
                   via=[] if row.kind == "bastion" else [str(v) for v in _loads(row.via_json, [])],
                   port=row.port, user=row.user, identity_file=row.identity_file,
                   options={str(k): str(v) for k, v in _loads(row.options_json, {}).items()}, source="ui", strict_host_keys=True)


def _clean_text(v: Any, what: str, max_len: int = 4096) -> str:
    s = str(v or "").strip()
    if len(s) > max_len or any(c in s for c in "\n\r\0"):
        raise bad_request(f"{what}: invalid value", "INVALID_MACHINE")
    return s


def validate(body: dict[str, Any], dev: DevSettings, *, existing: Machine | None = None) -> dict[str, Any]:
    """Checked, normalized fields of a machine added or edited in the app."""
    out: dict[str, Any] = {}
    kind = body.get("kind", existing.kind if existing else "machine")
    if kind not in ("machine", "bastion"):
        raise bad_request("kind must be machine or bastion", "INVALID_MACHINE")
    out["kind"] = kind
    if existing is None:
        name = _clean_text(body.get("name"), "name", 63)
        if not NAME_RE.match(name) or name == "local":
            raise bad_request("name: letters, digits, '.', '_' and '-' (not 'local')", "INVALID_MACHINE")
        out["name"] = name
    if "ssh" in body or existing is None:
        dest = _clean_text(body.get("ssh"), "ssh", 253)
        if not DEST_RE.match(dest):
            raise bad_request("ssh: give a host, user@host or an alias of ~/.ssh/config", "INVALID_MACHINE")
        out["ssh"] = dest
    if "port" in body:
        p = body.get("port")
        if p in (None, ""):
            out["port"] = None
        else:
            try:
                p = int(p)
            except (TypeError, ValueError):
                raise bad_request("port must be a number", "INVALID_MACHINE")
            if not 1 <= p <= 65535:
                raise bad_request("port must be 1–65535", "INVALID_MACHINE")
            out["port"] = p
    if "user" in body:
        u = _clean_text(body.get("user"), "user", 64)
        if u and not USER_RE.match(u):
            raise bad_request("user: letters, digits, '.', '_' and '-'", "INVALID_MACHINE")
        out["user"] = u or None
    if "identity_file" in body:
        f = _clean_text(body.get("identity_file"), "identity_file")
        if f:
            if not (f.startswith("/") or f.startswith("~/")):
                raise bad_request("identity_file: an absolute path or one starting with ~/", "INVALID_MACHINE")
            if not Path(f).expanduser().is_file():
                raise bad_request(f"identity_file: {f} does not exist on this server", "INVALID_MACHINE")
        out["identity_file"] = f or None
    if "options" in body:
        opts = body.get("options") or {}
        if not isinstance(opts, dict):
            raise bad_request("options must be an object of ssh -o options", "INVALID_MACHINE")
        clean: dict[str, str] = {}
        for k, v in opts.items():
            key = ALLOWED_OPTIONS.get(str(k).lower())
            if key is None:
                raise bad_request(f"ssh option {k} is not allowed here (allowed: {', '.join(sorted(ALLOWED_OPTIONS.values()))})",
                                  "SSH_OPTION_NOT_ALLOWED")
            val = str(v).strip()
            if not VALUE_RE.match(val):
                raise bad_request(f"ssh option {key}: invalid value", "INVALID_MACHINE")
            clean[key] = val
        out["options"] = clean
    if "via" in body or existing is None:
        via = [str(v) for v in (body.get("via") or [])]
        if kind == "bastion" and via:
            raise bad_request("a bastion is reached directly (no via)", "INVALID_MACHINE")
        if len(via) > MAX_VIA or len(set(via)) != len(via):
            raise bad_request(f"via: up to {MAX_VIA} different bastions", "INVALID_MACHINE")
        for v in via:
            b = dev.host(v)
            if b is None or b.kind != "bastion":
                raise bad_request(f"via: {v} is not a bastion", "INVALID_MACHINE")
        out["via"] = via
    if "work_dir" in body:
        w = _clean_text(body.get("work_dir"), "work_dir") or DEFAULT_WORK_DIR
        if not (w.startswith("/") or w == "~" or w.startswith("~/")):
            raise bad_request("work_dir: an absolute path or one starting with ~/ (on the machine)", "INVALID_MACHINE")
        out["work_dir"] = w
    for b in ("code_server", "opencode", "codex"):
        if b in body:
            v = _clean_text(body.get(b), b)
            if v.startswith("-"):
                raise bad_request(f"{b}: invalid executable", "INVALID_MACHINE")
            out[b] = v or b.replace("_", "-")
    return out


class MachineRegistry:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.dev: DevSettings = ctx.settings.development
        state = Path(ctx.settings.state_dir)
        if self.dev.control_dir is None:
            self.dev.control_dir = conn.control_dir_for(state)
        if self.dev.known_hosts is None:
            self.dev.known_hosts = state / "ssh" / "known_hosts"
        conn.ensure_dirs(self.dev)

    # ------------------------------------------------------------------ loading
    def load(self) -> None:
        """Mirror the config file's hosts into the table and expose the app's own machines to `DevSettings`."""
        cfg = {h.name: h for h in self.dev.hosts}
        with self.ctx.db.write() as s:
            rows = {r.name: r for r in s.query(Machine).all()}
            for name, h in cfg.items():
                r = rows.get(name)
                if r is None:
                    r = Machine(name=name, source="config", ssh=h.ssh, created_at=_now())
                    s.add(r)
                elif r.source != "config":
                    continue  # an app machine of the same name: the config host wins (see apply)
                r.kind, r.ssh, r.port, r.user, r.identity_file = h.kind, h.ssh, h.port, h.user, h.identity_file
                r.via_json, r.options_json, r.work_dir = json.dumps(h.via), "{}", h.work_dir
                r.code_server, r.opencode, r.codex = h.code_server, h.opencode, h.codex
            for name, r in rows.items():
                if r.source == "config" and name not in cfg:
                    s.delete(r)
        self.apply()

    def apply(self) -> None:
        cfg = {h.name for h in self.dev.hosts}
        with self.ctx.db.read() as s:
            rows = s.query(Machine).filter(Machine.source == "ui").order_by(Machine.created_at).all()
        self.dev.ui_hosts = [to_host(r) for r in rows if r.name not in cfg]

    # ------------------------------------------------------------------ reading
    def row(self, name: str) -> Machine:
        with self.ctx.db.read() as s:
            r = s.get(Machine, name)
        if r is None:
            raise not_found(f"no machine {name!r}", "MACHINE_NOT_FOUND")
        return r

    def host(self, name: str) -> DevHost:
        h = self.dev.host(name)
        if h is None:
            raise not_found(f"no machine {name!r}", "MACHINE_NOT_FOUND")
        return h

    def users(self, name: str) -> dict[str, list[str]]:
        """Who depends on `name`: development workspaces on it, machines reached through it."""
        with self.ctx.db.read() as s:
            ws = [r.name for r in s.query(DevWorkspace).filter(DevWorkspace.host == name, DevWorkspace.status != "archived").all()]
        through = [h.name for h in self.dev.all_hosts() if name in h.via]
        return {"workspaces": ws, "machines": through}

    def to_dict(self, r: Machine) -> dict[str, Any]:
        h = self.dev.host(r.name)
        shadowed = r.source == "ui" and any(c.name == r.name for c in self.dev.hosts)
        used = self.users(r.name)
        return {
            "name": r.name, "kind": r.kind, "source": r.source, "editable": r.source == "ui" and not shadowed, "shadowed": shadowed,
            "ssh": r.ssh, "port": r.port, "user": r.user, "identity_file": r.identity_file, "via": _loads(r.via_json, []),
            "options": _loads(r.options_json, {}), "ssh_args": list(h.ssh_args) if h else [],
            "work_dir": r.work_dir, "code_server": r.code_server, "opencode": r.opencode, "codex": r.codex,
            "strict_host_keys": bool(h and h.strict_host_keys),
            "status": r.status, "check": _loads(r.status_detail, None), "checked_at": iso(r.checked_at),
            "capabilities": _loads(r.capabilities_json, None), "probed_at": iso(r.probed_at),
            "ssh_command": conn.preview(self.dev, h) if h else None,
            "used_by": used, "created_at": iso(r.created_at), "updated_at": iso(r.updated_at),
        }

    def list(self) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            rows = s.query(Machine).all()
        order = {h.name: i for i, h in enumerate(self.dev.all_hosts())}
        rows.sort(key=lambda r: (r.kind != "machine", order.get(r.name, 10_000), r.name))
        return [self.to_dict(r) for r in rows]

    # ------------------------------------------------------------------ changes (machines added in the app)
    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        f = validate(body, self.dev)
        if self.dev.host(f["name"]) is not None:
            raise conflict("MACHINE_EXISTS", f"a machine named {f['name']} exists already")
        with self.ctx.db.write() as s:
            if s.get(Machine, f["name"]) is not None:
                raise conflict("MACHINE_EXISTS", f"a machine named {f['name']} exists already")
            r = Machine(name=f["name"], kind=f["kind"], source="ui", ssh=f["ssh"], port=f.get("port"), user=f.get("user"),
                        identity_file=f.get("identity_file"), via_json=json.dumps(f.get("via", [])),
                        options_json=json.dumps(f.get("options", {})), work_dir=f.get("work_dir", DEFAULT_WORK_DIR),
                        code_server=f.get("code_server", "code-server"), opencode=f.get("opencode", "opencode"),
                        codex=f.get("codex", "codex"), status="unknown", created_at=_now(), updated_at=_now())
            s.add(r)
        self.apply()
        self._changed(f["name"])
        return self.to_dict(self.row(f["name"]))

    def _editable(self, name: str) -> Machine:
        r = self.row(name)
        if r.source != "ui" or any(c.name == name for c in self.dev.hosts):
            raise ApiError(409, "MACHINE_READ_ONLY", f"{name} is defined in the config file ([[development.hosts]]); edit it there")
        return r

    async def update(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._editable(name)
        if "name" in body and body["name"] != name:
            raise bad_request("a machine cannot be renamed (workspaces refer to it by name)", "INVALID_MACHINE")
        if "kind" in body and body["kind"] != r.kind:
            used = self.users(name)
            if used["machines"] or used["workspaces"]:
                raise conflict("MACHINE_IN_USE", f"{name} is in use; its kind cannot change")
        old = self.dev.host(name)
        f = validate({k: v for k, v in body.items() if k != "name"}, self.dev, existing=r)
        with self.ctx.db.write() as s:
            row = s.get(Machine, name)
            assert row is not None
            for k, v in f.items():
                if k == "via":
                    row.via_json = json.dumps(v)
                elif k == "options":
                    row.options_json = json.dumps(v)
                else:
                    setattr(row, k, v)
            row.status, row.status_detail, row.updated_at = "unknown", None, _now()
        self.apply()
        if old is not None:
            await conn.close_master(self.dev, old)
        self._changed(name)
        return self.to_dict(self.row(name))

    async def delete(self, name: str) -> None:
        self._editable(name)
        used = self.users(name)
        if used["workspaces"] or used["machines"]:
            what = ", ".join([*(f"workspace {w}" for w in used["workspaces"]), *(f"machine {m}" for m in used["machines"])])
            raise conflict("MACHINE_IN_USE", f"{name} is still used by {what}", **used)
        old = self.dev.host(name)
        with self.ctx.db.write() as s:
            row = s.get(Machine, name)
            if row is not None:
                s.delete(row)
        self.apply()
        if old is not None:
            await conn.close_master(self.dev, old)
        self._changed(name)

    # ------------------------------------------------------------------ results of checks and probes
    def save_check(self, name: str, status: str, detail: dict[str, Any]) -> None:
        with self.ctx.db.write() as s:
            row = s.get(Machine, name)
            if row is not None:
                row.status, row.status_detail, row.checked_at = status, json.dumps(detail), _now()
        self._changed(name)

    def save_probe(self, name: str, caps: dict[str, Any]) -> None:
        with self.ctx.db.write() as s:
            row = s.get(Machine, name)
            if row is not None:
                row.capabilities_json, row.probed_at = json.dumps(caps), _now()
        self._changed(name)

    def _changed(self, name: str) -> None:
        if self.ctx.events is not None:
            self.ctx.events.publish("global", {"type": "machine_changed", "name": name})

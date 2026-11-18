"""Machines API (/api/v1/machines…, /api/v1/ssh-keys): list, add, edit and remove machines and bastions; check the
path to one hop by hop; probe what it has; show and trust host keys; install this server's key with ssh-copy-id in a
terminal; open a terminal on a machine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import DevHost
from ..deps import AppContext, require_auth, require_auth_mutating
from ..development.targets import LOCAL, LocalTarget, TargetError, target_for
from ..errors import ApiError, bad_request
from ..kv import kv_get, kv_put
from . import bootstrap, conn, envfwd, health, hostkeys, keys, probe
from .registry import ALLOWED_OPTIONS, MachineRegistry, validate

router = APIRouter()
LOCAL_KEY = "machine_local"


def reg(ctx: AppContext) -> MachineRegistry:
    return ctx.extra["machines"]


class MachineIn(BaseModel):
    name: str | None = Field(None, max_length=63)
    kind: str | None = Field(None, pattern="^(machine|bastion)$")
    ssh: str | None = Field(None, max_length=253)
    port: int | None = None
    user: str | None = Field(None, max_length=64)
    identity_file: str | None = Field(None, max_length=4096)
    via: list[str] | None = Field(None, max_length=8)
    options: dict[str, str] | None = None
    work_dir: str | None = Field(None, max_length=4096)
    code_server: str | None = Field(None, max_length=4096)
    opencode: str | None = Field(None, max_length=4096)
    codex: str | None = Field(None, max_length=4096)
    env: dict[str, Any] | None = None  # {forward_env, env_vars, forward_proxy, ca_bundle}


class TrustIn(BaseModel):
    hop: str = Field(min_length=1, max_length=63)
    fingerprints: list[str] = Field(min_length=1, max_length=16)


class InstallKeyIn(BaseModel):
    hop: str | None = Field(None, max_length=63)  # default: the machine itself
    public_key_file: str | None = Field(None, max_length=4096)  # default: the first key of ~/.ssh (ed25519 first)


class BootstrapIn(BaseModel):
    tools: list[str] | None = Field(None, max_length=8)  # default: opencode, code-server and gh where missing
    system_packages: bool = True
    agent_settings: bool = True
    update: bool = False  # install the latest release again over setup's own copies


class TerminalIn(BaseModel):
    cwd: str | None = Field(None, max_length=4096)  # on the machine; default: the home folder
    label: str | None = Field(None, max_length=80)


def _fields(body: MachineIn) -> dict[str, Any]:
    return body.model_dump(exclude_unset=True)


def _local_entry(ctx: AppContext) -> dict[str, Any]:
    saved = kv_get(ctx, LOCAL_KEY, {}) or {}
    return {"name": LOCAL, "kind": "machine", "source": "server", "editable": False, "label": "This server", "ssh": None,
            "status": "online", "capabilities": saved.get("capabilities"), "probed_at": saved.get("probed_at"), "via": [],
            "used_by": {"workspaces": [], "machines": []}}


def _not_local(name: str) -> None:
    if name == LOCAL:
        raise bad_request("this server needs no connection", "MACHINE_IS_LOCAL")


@router.get("/machines")
def list_machines(ctx: AppContext = Depends(require_auth)):
    return {"machines": [_local_entry(ctx), *reg(ctx).list()], "allowed_options": sorted(ALLOWED_OPTIONS.values())}


@router.post("/machines", status_code=201)
def create_machine(body: MachineIn, ctx: AppContext = Depends(require_auth_mutating)):
    return reg(ctx).create(_fields(body))


@router.get("/machines/{name}")
def get_machine(name: str, ctx: AppContext = Depends(require_auth)):
    if name == LOCAL:
        return _local_entry(ctx)
    r = reg(ctx)
    return r.to_dict(r.row(name))


@router.patch("/machines/{name}")
async def update_machine(name: str, body: MachineIn, ctx: AppContext = Depends(require_auth_mutating)):
    _not_local(name)
    return await reg(ctx).update(name, _fields(body))


@router.delete("/machines/{name}")
async def delete_machine(name: str, ctx: AppContext = Depends(require_auth_mutating)):
    _not_local(name)
    await reg(ctx).delete(name)
    return {"ok": True}


@router.post("/machines/{name}/check")
async def check_machine(name: str, ctx: AppContext = Depends(require_auth_mutating)):
    if name == LOCAL:
        return {"ok": True, "status": "online", "summary": "this server", "hops": [], "failed_at": None,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    r = reg(ctx)
    h = r.host(name)
    res = await health.check(r.dev, h)
    r.save_check(name, res["status"], res)
    return res


@router.post("/machines/{name}/probe")
async def probe_machine(name: str, ctx: AppContext = Depends(require_auth_mutating)):
    if name == LOCAL:
        h = DevHost(name=LOCAL, ssh="", work_dir=str(ctx.settings.state_dir))
        caps = await probe.probe(LocalTarget(), h)
        kv_put(ctx, LOCAL_KEY, {"capabilities": caps, "probed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
        return caps
    r = reg(ctx)
    h = r.host(name)
    if h.kind == "bastion":
        raise bad_request("bastions are only passed through; probe the machines behind them", "MACHINE_IS_BASTION")
    try:
        caps = await probe.probe(target_for(r.dev, name), h)
    except TargetError as exc:
        raise ApiError(502, exc.code, exc.message)
    r.save_probe(name, caps)
    return caps


def _setup_host(ctx: AppContext, name: str) -> DevHost:
    _not_local(name)
    h = reg(ctx).host(name)
    if h.kind == "bastion":
        raise bad_request("bastions are only passed through; set up the machines behind them", "MACHINE_IS_BASTION")
    return h


@router.get("/machines/{name}/bootstrap")
def bootstrap_plan(name: str, ctx: AppContext = Depends(require_auth)):
    """What setup would do, from the last probe."""
    r = reg(ctx)
    h = _setup_host(ctx, name)
    caps = r.to_dict(r.row(name)).get("capabilities")
    jobs = ctx.extra["machine_jobs"]
    running = jobs.running(name)
    return {**bootstrap.plan(h, caps, r.dev.release_base), "running": running.id if running else None}


@router.post("/machines/{name}/bootstrap", status_code=202)
async def bootstrap_machine(body: BootstrapIn, name: str, ctx: AppContext = Depends(require_auth_mutating)):
    """Start setup (a machine job; follow its log)."""
    r = reg(ctx)
    h = _setup_host(ctx, name)
    bad = [x for x in body.tools or [] if x not in bootstrap.APP_TOOLS]
    if bad:
        raise bad_request(f"unknown tools {bad}; choose from {list(bootstrap.APP_TOOLS)}", "INVALID_TOOLS")
    params = body.model_dump()
    t = target_for(r.dev, name)
    cache = ctx.settings.state_dir / "cache" / "tools"

    async def work(jl):
        return await bootstrap.run(t, h, params, jl, release_base=r.dev.release_base, cache=cache,
                                   save_probe=lambda caps: r.save_probe(name, caps))

    return ctx.extra["machine_jobs"].start(name, "bootstrap", f"Set up {name}", params, work,
                                           secrets=envfwd.secrets_for(h))


@router.get("/machines/{name}/jobs")
def machine_jobs(name: str, ctx: AppContext = Depends(require_auth)):
    return {"jobs": ctx.extra["machine_jobs"].list(name)}


@router.get("/machine-jobs/{job_id}/log")
def machine_job_log(job_id: str, offset: int = 0, ctx: AppContext = Depends(require_auth)):
    return ctx.extra["machine_jobs"].read_log(job_id, offset)


@router.post("/machine-jobs/{job_id}/cancel")
async def machine_job_cancel(job_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await ctx.extra["machine_jobs"].cancel(job_id)


@router.get("/machines/{name}/hostkeys")
async def machine_hostkeys(name: str, ctx: AppContext = Depends(require_auth)):
    _not_local(name)
    r = reg(ctx)
    return {"hops": await hostkeys.scan_path(r.dev, r.host(name))}


@router.post("/machines/{name}/hostkeys")
async def trust_hostkeys(body: TrustIn, name: str, ctx: AppContext = Depends(require_auth_mutating)):
    _not_local(name)
    r = reg(ctx)
    res = await hostkeys.trust(r.dev, r.host(name), body.hop, body.fingerprints)
    for h in [r.host(name), *conn.chain(r.dev, r.host(name))]:
        await conn.close_master(r.dev, h)
    return res


@router.get("/machines/{name}/resolve")
async def resolve_machine(name: str, ctx: AppContext = Depends(require_auth)):
    """What ssh makes of the destination (~/.ssh/config aliases, ProxyJump set there)."""
    _not_local(name)
    r = reg(ctx)
    return await hostkeys.resolve(r.dev, r.host(name))


@router.post("/machines/resolve")
async def resolve_draft(body: MachineIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Resolve a destination before it is saved (the Add machine dialog)."""
    r = reg(ctx)
    f = validate({**_fields(body), "name": body.name or "draft"}, r.dev)
    h = DevHost(name=f["name"], ssh=f["ssh"], port=f.get("port"), user=f.get("user"), identity_file=f.get("identity_file"),
                options=f.get("options", {}), kind=f["kind"], via=f.get("via", []), strict_host_keys=True)
    return await hostkeys.resolve(r.dev, h)


@router.post("/machines/{name}/install-key", status_code=201)
async def install_key(body: InstallKeyIn, name: str, ctx: AppContext = Depends(require_auth_mutating)):
    """A terminal running ssh-copy-id for the machine (or a bastion on its path): the password is typed there."""
    _not_local(name)
    r = reg(ctx)
    h = r.host(name)
    path = hostkeys.hop_path(r.dev, h)
    hop_name = body.hop or name
    idx = next((i for i, x in enumerate(path) if x.name == hop_name), None)
    if idx is None:
        raise bad_request(f"{hop_name} is not on the path to {name}", "INVALID_HOP")
    pub = body.public_key_file or keys.default_public_key()
    if not pub:
        raise ApiError(409, "NO_SSH_KEY", "this server has no SSH key yet: create one first")
    argv = keys.install_command(r.dev, path[idx], path[:idx], pub)
    assert ctx.terminals is not None
    term = await asyncio.to_thread(ctx.terminals.create, str(Path.home()), f"Install key on {hop_name}", argv)
    return {"terminal": term, "command": " ".join(keys.copy_id_argv(r.dev, path[idx], path[:idx], pub))}


@router.post("/machines/{name}/terminals", status_code=201)
async def machine_terminal(body: TerminalIn, name: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await open_terminal(ctx, name, body.cwd, body.label)


async def open_terminal(ctx: AppContext, name: str, cwd: str | None, label: str | None) -> dict[str, Any]:
    """A shell on the machine (an ssh login that starts in `cwd`), run by the app's tmux like every terminal."""
    assert ctx.terminals is not None
    if name == LOCAL:
        return await asyncio.to_thread(ctx.terminals.create, cwd or "~", label)
    r = reg(ctx)
    try:
        t = target_for(r.dev, name)
        path = await t.expand(cwd or "~")
        if not await t.is_dir(path):
            raise bad_request(f"{path} is not a folder on {name}", "CWD_NOT_FOUND")
    except TargetError as exc:
        raise ApiError(502 if exc.code == "DEV_HOST_UNREACHABLE" else 400, exc.code, exc.message)
    if await asyncio.to_thread(ctx.terminals.remote_ready, name):  # in the machine's own tmux: survives disconnects
        return await asyncio.to_thread(ctx.terminals.create_remote, name, path, label or name)
    # no tmux there: the ssh login runs in this server's tmux (it ends with the connection)
    try:
        env_file = await envfwd.push(t)
    except TargetError as exc:
        raise ApiError(502 if exc.code == "DEV_HOST_UNREACHABLE" else 400, exc.code, exc.message)
    argv, _, _ = t.spawn_argv(["sh", "-c", 'exec "${SHELL:-sh}" -l'], path, None, ssh_opts=["-tt"], env_file=env_file)
    return await asyncio.to_thread(ctx.terminals.create, str(Path.home()), label or name, argv)


@router.get("/ssh-keys")
def ssh_keys(ctx: AppContext = Depends(require_auth)):
    return {"keys": keys.list_keys(), "ssh_dir": str(keys.ssh_dir())}


@router.post("/ssh-keys", status_code=201)
async def create_ssh_key(ctx: AppContext = Depends(require_auth_mutating)):
    return await keys.generate_key(reg(ctx).dev)

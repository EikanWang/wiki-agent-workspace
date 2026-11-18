"""Machines API (/api/v1/machines…, /api/v1/ssh-keys): list, add, edit and remove machines and bastions; check the
path to one hop by hop; probe what it has; show and trust host keys; install this server's key with ssh-copy-id in a
terminal; open a terminal on a machine."""

from __future__ import annotations

import asyncio
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import DevHost
from ..deps import AppContext, require_auth, require_auth_mutating
from ..development.targets import LOCAL, LocalTarget, TargetError, target_for
from ..errors import ApiError, bad_request, not_found
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


class SetupIn(BaseModel):
    bootstrap: BootstrapIn | None = Field(default_factory=BootstrapIn)  # None: only the recipes
    recipes: list[str] | None = Field(None, max_length=200)  # default: all assigned, in order
    force: bool = False  # run recipes that succeeded before (unchanged) again


class RecipeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    run: str = Field(min_length=1, max_length=200_000)
    check: str | None = Field(None, max_length=200_000)
    env_out: str | None = Field(None, max_length=200_000)


class RecipeImportIn(BaseModel):
    folder: str = Field(min_length=1, max_length=4096)


class AssignIn(BaseModel):
    recipes: list[str] = Field(max_length=200)


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
    ctx.extra["recipes"].forget_machine(name)
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


@router.post("/machines/{name}/setup", status_code=202)
async def setup_machine(body: SetupIn, name: str, ctx: AppContext = Depends(require_auth_mutating)):
    """Set up machine: the app's tools (bootstrap, unless `bootstrap` is null), then its recipes in order (a machine
    job; it stops at the first recipe that fails)."""
    r = reg(ctx)
    h = _setup_host(ctx, name)
    rec = ctx.extra["recipes"]
    if body.bootstrap is not None:
        bad = [x for x in body.bootstrap.tools or [] if x not in bootstrap.APP_TOOLS]
        if bad:
            raise bad_request(f"unknown tools {bad}; choose from {list(bootstrap.APP_TOOLS)}", "INVALID_TOOLS")
    assigned = {x["id"] for x in rec.for_machine(name)}
    if body.recipes is not None and (missing := [x for x in body.recipes if x not in assigned]):
        raise bad_request(f"not assigned to {name}: {missing}", "INVALID_RECIPES")
    params = body.model_dump()
    t = target_for(r.dev, name)
    cache = ctx.settings.state_dir / "cache" / "tools"
    jobs = ctx.extra["machine_jobs"]
    holder: dict[str, str] = {}

    async def work(jl):
        out: dict[str, Any] = {}
        if body.bootstrap is not None:
            out["bootstrap"] = await bootstrap.run(t, h, body.bootstrap.model_dump(), jl, release_base=r.dev.release_base,
                                                   cache=cache, save_probe=lambda caps: r.save_probe(name, caps))
        out["recipes"] = await rec.run_all(t, name, jl, only=body.recipes, force=body.force, job_id=holder["id"])
        return out

    title = f"Set up {name}" if body.bootstrap is not None else (
        f"Recipes on {name}" if body.recipes is None or len(body.recipes) != 1
        else f"Recipe {next(x['name'] for x in rec.for_machine(name) if x['id'] == body.recipes[0])} on {name}")
    j = jobs.start(name, "setup", title, params, work, secrets=envfwd.secrets_for(h))
    holder["id"] = j["id"]
    return j


@router.get("/machines/{name}/recipes")
def machine_recipes(name: str, ctx: AppContext = Depends(require_auth)):
    reg(ctx).host(name)
    return {"recipes": ctx.extra["recipes"].for_machine(name)}


@router.put("/machines/{name}/recipes")
async def assign_recipes(body: AssignIn, name: str, ctx: AppContext = Depends(require_auth_mutating)):
    """The recipes of a machine, in order. Its env.sh is rewritten (best effort: also at the next setup)."""
    r = reg(ctx)
    _setup_host(ctx, name)
    rec = ctx.extra["recipes"]
    rec.assign(name, body.recipes)
    warning = None
    try:
        await rec.write_env(target_for(r.dev, name), name)
    except TargetError as exc:
        warning = f"could not update the machine's env.sh now ({exc.message}); the next setup does"
    return {"recipes": rec.for_machine(name), "warning": warning}


@router.get("/recipes")
def list_recipes(ctx: AppContext = Depends(require_auth)):
    return {"recipes": ctx.extra["recipes"].list()}


@router.post("/recipes", status_code=201)
def create_recipe(body: RecipeIn, ctx: AppContext = Depends(require_auth_mutating)):
    return ctx.extra["recipes"].create(body.model_dump())


@router.put("/recipes/{recipe_id}")
def update_recipe(recipe_id: str, body: RecipeIn, ctx: AppContext = Depends(require_auth_mutating)):
    return ctx.extra["recipes"].update(recipe_id, body.model_dump())


@router.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    ctx.extra["recipes"].delete(recipe_id)
    return {"ok": True}


@router.post("/recipes/import")
def import_recipes(body: RecipeImportIn, ctx: AppContext = Depends(require_auth_mutating)):
    """The *.sh files of a folder on this server (e.g. in a wiki, kept in Git) as recipes."""
    return ctx.extra["recipes"].import_folder(body.folder)


_LIST_DIRS = r'''p=$1; hidden=$2
case "$p" in "~") p=$HOME;; "~/"*) p="$HOME/${p#"~/"}";; esac
[ -e "$p" ] || { echo "@@notfound"; exit 0; }
[ -d "$p" ] || { echo "@@notdir"; exit 0; }
cd "$p" 2>/dev/null || { echo "@@denied"; exit 0; }
pwd -P
if [ -e .git ]; then echo git; else echo nogit; fi
n=0
for e in * .*; do
  case "$e" in .|..) continue;; .*) [ "$hidden" = 1 ] || continue;; esac
  [ -d "$e" ] || continue
  if [ -e "$e/.git" ]; then g=1; else g=0; fi
  printf '%s/%s\n' "$g" "$e"
  n=$((n+1)); [ $n -ge 2000 ] && break
done
exit 0
'''


@router.get("/machines/{name}/fs/dirs")
async def machine_dirs(name: str, path: str = "~", hidden: bool = False, ctx: AppContext = Depends(require_auth)):
    """The folder picker on a machine: subfolders of a path there (same answer as /fs/dirs)."""
    if name == LOCAL:
        from ..workspaces import list_server_dirs

        return list_server_dirs(path=path, hidden=hidden, ctx=ctx)
    r = reg(ctx)
    h = r.host(name)
    if h.kind == "bastion":
        raise bad_request("bastions are only passed through; browse the machines behind them", "MACHINE_IS_BASTION")
    if not (path == "~" or path.startswith("~/") or path.startswith("/")):
        raise bad_request("absolute path required", "INVALID_PATH")
    t = target_for(r.dev, name)
    rc, out, err = await t.run(["sh", "-c", _LIST_DIRS, "_", path, "1" if hidden else "0"], timeout=30)
    if rc != 0:
        raise ApiError(502 if rc == 255 else 400, "DEV_HOST_FAILED", f"cannot list {path} on {name}: {err.strip()[:300]}")
    lines = out.split("\n")
    if lines[0] == "@@notfound":
        raise not_found(f"{path} does not exist on {name}", "FOLDER_NOT_FOUND")
    if lines[0] == "@@notdir":
        raise bad_request(f"{path} is not a directory on {name}", "NOT_A_DIRECTORY")
    if lines[0] == "@@denied":
        raise ApiError(403, "PERMISSION_DENIED", f"cannot list {path} on {name}")
    p = lines[0]
    entries = sorted(({"name": ln[2:], "path": f"{p.rstrip('/')}/{ln[2:]}", "is_git": ln[0] == "1"}
                      for ln in lines[2:] if len(ln) > 2 and ln[1] == "/"), key=lambda e: e["name"].lower())
    parent = (p.rsplit("/", 1)[0] or "/") if p != "/" else None
    return {"path": p, "parent": parent, "is_git": lines[1] == "git", "open_as": None, "entries": entries[:500],
            "listed_at": datetime.now(timezone.utc).isoformat()}


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


async def open_terminal(ctx: AppContext, name: str, cwd: str | None, label: str | None,
                        argv: list[str] | None = None) -> dict[str, Any]:
    """A shell on the machine (an ssh login that starts in `cwd`), run by the app's tmux like every terminal; with
    `argv`, that runs in it first."""
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
        return await asyncio.to_thread(ctx.terminals.create_remote, name, path, label or name, argv)
    # no tmux there: the ssh login runs in this server's tmux (it ends with the connection)
    try:
        env_file = await envfwd.push(t)
    except TargetError as exc:
        raise ApiError(502 if exc.code == "DEV_HOST_UNREACHABLE" else 400, exc.code, exc.message)
    shell = ["sh", "-c", 'exec "${SHELL:-sh}" -l'] if not argv else \
        ["sh", "-c", 'exec "${SHELL:-sh}" -l -c "$0"', shlex.join(argv) + '; exec "${SHELL:-sh}" -l']
    ssh, _, _ = t.spawn_argv(shell, path, None, ssh_opts=["-tt"], env_file=env_file)
    return await asyncio.to_thread(ctx.terminals.create, str(Path.home()), label or name, ssh)


@router.get("/ssh-keys")
def ssh_keys(ctx: AppContext = Depends(require_auth)):
    return {"keys": keys.list_keys(), "ssh_dir": str(keys.ssh_dir())}


@router.post("/ssh-keys", status_code=201)
async def create_ssh_key(ctx: AppContext = Depends(require_auth_mutating)):
    return await keys.generate_key(reg(ctx).dev)

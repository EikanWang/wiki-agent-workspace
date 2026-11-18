"""Setup recipes: the user's own steps for a machine, after the app's tools (machines.bootstrap).

A recipe is a shell script (`run`) with an optional `check` (exit 0: already done, `run` is skipped) and an optional
`env_out`: a snippet sourced on the machine (`conda activate x`, `. /opt/intel/oneapi/setvars.sh`) whose effect on
the environment is recorded. Recipes are assigned to machines in order; "Set up machine" runs them one after the
other and stops at the first failure. What the successful ones' env_out set becomes <work_dir>/env.sh, which every
command the app runs there sources (development.targets.path_prefix), so terminals, builds, the editor and agent turns
all see it.

Status per machine: not run, ok, failed, or outdated (changed since it last succeeded there). Recipes can be kept as
*.sh files (e.g. in a wiki folder, under Git) and imported; the first comment lines may hold `# name: …`,
`# check: …` and `# env: …` (both may repeat; their lines are joined).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shlex
from pathlib import Path
from typing import Any

from ..db import iso, new_id, utcnow
from ..development.targets import SshTarget, TargetError
from ..errors import bad_request, conflict, not_found
from ..models import MachineRecipe, MachineRecipeState
from .jobs import JobFailed, JobLog

MAX_SCRIPT = 200_000
RECIPE_TIMEOUT = 4 * 3600
# never recorded from env_out: per-shell or per-process values
VOLATILE = re.compile(r"^(_|PWD|OLDPWD|SHLVL|RANDOM|LINENO|SECONDS|PPID|BASHPID|BASH_.*|FUNCNAME|PIPESTATUS|"
                      r"OPTIND|OPTARG|IFS|PS[0-9]|HIST.*|COLUMNS|LINES|TERM|SSH_.*|TMUX.*|WAW_RUN_.*)$")


def version_of(run: str, check: str | None, env_out: str | None) -> str:
    h = hashlib.sha256()
    for part in (run, check or "", env_out or ""):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


def recipe_dict(r: MachineRecipe, machines: list[str] | None = None) -> dict[str, Any]:
    return {"id": r.id, "name": r.name, "run": r.run, "check": r.check, "env_out": r.env_out, "version": r.version,
            "source_path": r.source_path, "machines": machines or [], "created_at": iso(r.created_at),
            "updated_at": iso(r.updated_at)}


def state_status(st: MachineRecipeState, r: MachineRecipe) -> str:
    if st.status == "ok" and st.version_ok != r.version:
        return "outdated"
    return st.status


def _clean(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 120 or not re.fullmatch(r"[\w .+@()/:-]+", name):
        raise bad_request("a recipe needs a name (letters, digits, spaces and - _ . + @ ( ) / :)", "INVALID_RECIPE")
    run = str(body.get("run") or "")
    if not run.strip():
        raise bad_request("a recipe needs a script to run", "INVALID_RECIPE")
    out = {"name": name, "run": run.replace("\r\n", "\n")}
    for k in ("check", "env_out"):
        v = body.get(k)
        out[k] = str(v).replace("\r\n", "\n") if v is not None and str(v).strip() else None
    if any(len(v or "") > MAX_SCRIPT for v in out.values()):
        raise bad_request("a recipe script is too long", "INVALID_RECIPE")
    return out


def parse_file(path: Path) -> dict[str, Any]:
    """A recipe from a *.sh file: its header comments `# name:`, `# check:`, `# env:`; the whole file is `run`."""
    text = path.read_text(encoding="utf-8", errors="replace")
    name, check, env = path.stem, [], []
    for ln in text.splitlines():
        if ln.startswith("#!") or not ln.strip():
            continue
        if not ln.startswith("#"):
            break
        m = re.match(r"^#\s*(name|check|env)\s*:\s?(.*)$", ln)
        if m:
            k, v = m.group(1), m.group(2).rstrip()
            if k == "name" and v.strip():
                name = v.strip()
            elif k == "check":
                check.append(v)
            elif k == "env":
                env.append(v)
    return {"name": name, "run": text, "check": "\n".join(check) or None, "env_out": "\n".join(env) or None}


class Recipes:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------ the library
    def _assigned(self, s: Any) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for st in s.query(MachineRecipeState).order_by(MachineRecipeState.machine).all():
            out.setdefault(st.recipe_id, []).append(st.machine)
        return out

    def list(self) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            used = self._assigned(s)
            rows = s.query(MachineRecipe).order_by(MachineRecipe.name).all()
            return [recipe_dict(r, used.get(r.id)) for r in rows]

    def get(self, recipe_id: str) -> MachineRecipe:
        with self.ctx.db.read() as s:
            r = s.get(MachineRecipe, recipe_id)
        if r is None:
            raise not_found("recipe not found", "RECIPE_NOT_FOUND")
        return r

    def _unique(self, s: Any, name: str, own: str | None) -> None:
        other = s.query(MachineRecipe).filter_by(name=name).first()
        if other is not None and other.id != own:
            raise conflict("RECIPE_EXISTS", f"there is a recipe named {name!r} already")

    def create(self, body: dict[str, Any], source_path: str | None = None) -> dict[str, Any]:
        b = _clean(body)
        with self.ctx.db.write() as s:
            self._unique(s, b["name"], None)
            r = MachineRecipe(id=new_id(), version=version_of(b["run"], b["check"], b["env_out"]), source_path=source_path, **b)
            s.add(r)
        self._event(None)
        return recipe_dict(self.get(r.id))

    def update(self, recipe_id: str, body: dict[str, Any]) -> dict[str, Any]:
        b = _clean(body)
        with self.ctx.db.write() as s:
            r = s.get(MachineRecipe, recipe_id)
            if r is None:
                raise not_found("recipe not found", "RECIPE_NOT_FOUND")
            self._unique(s, b["name"], recipe_id)
            for k, v in b.items():
                setattr(r, k, v)
            r.version = version_of(b["run"], b["check"], b["env_out"])
            r.updated_at = utcnow()
            used = self._assigned(s).get(recipe_id)
        self._event(None)
        return recipe_dict(self.get(recipe_id), used)

    def delete(self, recipe_id: str) -> None:
        with self.ctx.db.write() as s:
            r = s.get(MachineRecipe, recipe_id)
            if r is None:
                raise not_found("recipe not found", "RECIPE_NOT_FOUND")
            used = self._assigned(s).get(recipe_id)
            if used:
                raise conflict("RECIPE_IN_USE", f"{r.name} is assigned to {', '.join(used)}; remove it there first")
            s.delete(r)
        self._event(None)

    def import_folder(self, folder: str) -> dict[str, Any]:
        """The *.sh files of a folder on this server: new ones are added, changed ones updated (matched by file, then
        by name)."""
        p = Path(folder).expanduser()
        if not p.is_absolute() or not p.is_dir():
            raise bad_request(f"{folder} is not a folder on this server", "INVALID_PATH")
        added, updated, unchanged, errors = [], [], [], []
        for f in sorted(p.glob("*.sh")):
            try:
                b = _clean(parse_file(f))
            except Exception as exc:
                errors.append({"file": str(f), "error": getattr(exc, "message", str(exc))})
                continue
            with self.ctx.db.read() as s:
                cur = (s.query(MachineRecipe).filter_by(source_path=str(f)).first()
                       or s.query(MachineRecipe).filter_by(name=b["name"]).first())
            if cur is None:
                self.create(b, source_path=str(f))
                added.append(b["name"])
            elif version_of(b["run"], b["check"], b["env_out"]) == cur.version and cur.name == b["name"]:
                unchanged.append(b["name"])
            else:
                try:
                    self.update(cur.id, b)
                except Exception as exc:
                    errors.append({"file": str(f), "error": getattr(exc, "message", str(exc))})
                    continue
                with self.ctx.db.write() as s:
                    r = s.get(MachineRecipe, cur.id)
                    if r is not None:
                        r.source_path = str(f)
                updated.append(b["name"])
        return {"added": added, "updated": updated, "unchanged": unchanged, "errors": errors, "recipes": self.list()}

    # ------------------------------------------------------------------ per machine
    def for_machine(self, machine: str) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            rows = (s.query(MachineRecipeState, MachineRecipe).join(MachineRecipe, MachineRecipe.id == MachineRecipeState.recipe_id)
                    .filter(MachineRecipeState.machine == machine).order_by(MachineRecipeState.position).all())
        return [{**recipe_dict(r), "position": st.position, "status": state_status(st, r), "version_ok": st.version_ok,
                 "env_vars": env_names(st.env_text), "error": st.error, "last_job_id": st.last_job_id,
                 "last_run_at": iso(st.last_run_at)} for st, r in rows]

    def assign(self, machine: str, ids: list[str]) -> None:
        if len(set(ids)) != len(ids):
            raise bad_request("a recipe can be assigned once", "INVALID_RECIPES")
        with self.ctx.db.write() as s:
            for rid in ids:
                if s.get(MachineRecipe, rid) is None:
                    raise not_found(f"recipe {rid} not found", "RECIPE_NOT_FOUND")
            cur = {st.recipe_id: st for st in s.query(MachineRecipeState).filter_by(machine=machine).all()}
            for rid, st in cur.items():
                if rid not in ids:
                    s.delete(st)
            for i, rid in enumerate(ids):
                st = cur.get(rid)
                if st is None:
                    s.add(MachineRecipeState(machine=machine, recipe_id=rid, position=i, status="not_run"))
                else:
                    st.position = i
        self._event(machine)

    def forget_machine(self, machine: str) -> None:
        with self.ctx.db.write() as s:
            s.query(MachineRecipeState).filter_by(machine=machine).delete()

    def env_file_text(self, machine: str, before: str | None = None) -> str:
        """<work_dir>/env.sh: the env_out of the recipes that succeeded there, in order (with `before`: only those
        assigned before that recipe)."""
        parts = ["# written by wiki-agent-workspace (machine setup recipes); changes here are overwritten\n"]
        for x in self.for_machine(machine):
            if x["id"] == before:
                break
            st_text = self._env_text(machine, x["id"])
            if x["status"] in ("ok", "outdated") and st_text:
                parts.append(f"# {x['name']}\n{st_text.rstrip()}\n")
        return "".join(parts)

    def _env_text(self, machine: str, recipe_id: str) -> str | None:
        with self.ctx.db.read() as s:
            st = s.get(MachineRecipeState, (machine, recipe_id))
            return st.env_text if st else None

    def _save(self, machine: str, recipe_id: str, **fields: Any) -> None:
        with self.ctx.db.write() as s:
            st = s.get(MachineRecipeState, (machine, recipe_id))
            if st is not None:
                for k, v in fields.items():
                    setattr(st, k, v)
        self._event(machine)

    def _event(self, machine: str | None) -> None:
        if self.ctx.events:
            self.ctx.events.publish("global", {"type": "recipes_changed", "machine": machine})

    async def write_env(self, t: SshTarget, machine: str, before: str | None = None) -> None:
        w = await t.expand(t.host.work_dir)
        await t.write_text(f"{w}/env.sh", self.env_file_text(machine, before), 0o600)

    # ------------------------------------------------------------------ running them (a machine job)
    async def run_all(self, t: SshTarget, machine: str, jl: JobLog, *, only: list[str] | None, force: bool,
                      job_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": [], "skipped": [], "failed": None}
        todo = [x for x in self.for_machine(machine) if only is None or x["id"] in only]
        if not todo:
            jl.line("no recipes assigned to this machine")
            return result
        for x in todo:
            jl.step(f"Recipe: {x['name']}")
            if not force and x["status"] == "ok":
                jl.line("done before (unchanged since); skipped")
                result["skipped"].append(x["name"])
                continue
            try:
                await self.write_env(t, machine, before=x["id"])  # what the recipes before this one set up
                ok = await self._run_one(t, x, jl)
            except TargetError as exc:
                raise JobFailed(exc.message)
            now = utcnow()
            if not ok[0]:
                self._save(machine, x["id"], status="failed", error=ok[1], last_job_id=job_id, last_run_at=now)
                result["failed"] = x["name"]
                await self.write_env(t, machine)
                raise JobFailed(f"recipe {x['name']} failed: {ok[1]}")
            self._save(machine, x["id"], status="ok", version_ok=x["version"], env_text=ok[2], error=None,
                       last_job_id=job_id, last_run_at=now)
            result["ok"].append(x["name"])
        await self.write_env(t, machine)
        jl.step("Machine environment")
        names = sorted({n for x in self.for_machine(machine) for n in x["env_vars"]})
        jl.line(f"{t.host.work_dir}/env.sh sets {', '.join(names)}" if names else "no recipe sets variables")
        return result

    async def _run_one(self, t: SshTarget, x: dict[str, Any], jl: JobLog) -> tuple[bool, str | None, str | None]:
        """(ok, why not, the env_out export lines)."""
        from .bootstrap import run_logged

        w = await t.expand(t.host.work_dir)
        d = f"{w}/recipes"
        await t.mkdir(d)
        base = f"{d}/{x['id'][:12]}"
        await t.write_text(f"{base}.sh", x["run"], 0o700)
        if x["check"]:
            await t.write_text(f"{base}.check.sh", x["check"], 0o700)
            jl.line("$ (check)")
            rc = await run_logged(t, _RUNNER, [f"{base}.check.sh", w], jl, timeout=600)
            if rc == 0:
                jl.line("the check passed: already done")
            else:
                jl.line(f"the check says not done (exit {rc}): running it")
        else:
            rc = 1
        if rc != 0:
            rc = await run_logged(t, _RUNNER, [f"{base}.sh", w], jl, timeout=RECIPE_TIMEOUT)
            if rc != 0:
                return False, f"exit {rc}", None
        env_text = None
        if x["env_out"]:
            await t.write_text(f"{base}.env.sh", x["env_out"], 0o600)
            env_text, err = await capture_env(t, f"{base}.env.sh")
            if env_text is None:
                return False, f"its environment snippet failed: {err}", None
            names = env_names(env_text)
            jl.line(f"sets {', '.join(names)}" if names else "its environment snippet changes nothing")
        return True, None, env_text


# runs a recipe file: with its own interpreter when it has a #! line, else sh; in $HOME, WAW_WORK_DIR set
_RUNNER = r'''f=$1; WAW_WORK_DIR=$2; export WAW_WORK_DIR; cd || exit 1
if head -c 2 "$f" | grep -q '^#!'; then exec "$f"; else exec sh "$f"; fi
'''

# sources an env_out snippet (bash when there is one: setvars.sh and conda need it) between two dumps of the environment
_CAPTURE = r'''f=$1
sh_=sh; command -v bash >/dev/null 2>&1 && sh_=bash
exec "$sh_" -c '
dump() { if env -0 >/dev/null 2>&1; then env -0; else env | tr "\n" "\0"; fi; }
printf "@@WAW-A\0"; dump; printf "@@WAW-B\0"
{ . "$1"; } </dev/null >&2 || { printf "@@WAW-FAIL\0"; exit 3; }
dump; printf "@@WAW-C\0"' _ "$f"
'''


def _parse_dump(chunk: bytes) -> dict[str, str]:
    out = {}
    for item in chunk.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep and re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_]*", k):
            out[k.decode()] = v.decode("utf-8", "replace")
    return out


def diff_env(before: dict[str, str], after: dict[str, str]) -> str:
    """Export lines that turn `before` into `after`; a PATH-like variable that only gained a prefix or suffix keeps
    whatever it is when the file is sourced."""
    lines = []
    for k in sorted(set(before) | set(after)):
        if VOLATILE.match(k):
            continue
        a, b = before.get(k), after.get(k)
        if a == b:
            continue
        if b is None:
            lines.append(f"unset {k}")
        elif a and b.endswith(":" + a) and len(b) > len(a) + 1:
            lines.append(f"export {k}={shlex.quote(b[:-len(a) - 1])}\"${{{k}:+:${k}}}\"")
        elif a and b.startswith(a + ":") and len(b) > len(a) + 1:
            lines.append(f"export {k}=\"${{{k}:+${k}:}}\"{shlex.quote(b[len(a) + 1:])}")
        else:
            lines.append(f"export {k}={shlex.quote(b)}")
    return "\n".join(lines) + ("\n" if lines else "")


def env_names(text: str | None) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^(?:export|unset) ([A-Za-z_][A-Za-z0-9_]*)", text or "", re.M)]


async def capture_env(t: SshTarget, snippet_path: str) -> tuple[str | None, str]:
    argv, _, _ = t.spawn_argv(["sh", "-c", _CAPTURE, "_", snippet_path], None, None)
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "timed out"
    if b"@@WAW-FAIL\0" in out or b"@@WAW-C\0" not in out:
        return None, (err.decode("utf-8", "replace").strip()[-300:] or f"exit {proc.returncode}")
    a = out.split(b"@@WAW-A\0", 1)[1]
    before, rest = a.split(b"@@WAW-B\0", 1)
    after = rest.split(b"@@WAW-C\0", 1)[0]
    return diff_env(_parse_dump(before), _parse_dump(after)), ""


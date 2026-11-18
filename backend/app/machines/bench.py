"""Benchmarks: definitions, runs with their metrics, comparison against a baseline, and A/B runs of two commits.

A definition is a `[[bench]]` table in the repository's `.waw/bench.toml` (it wins by name) or one kept in the app for a
repository or a single development workspace:

    [[bench]]
    name = "matmul-xpu"
    setup = "python setup.py develop"      # optional; skipped while the commit, the changes and the setup are the same
    command = "python bench/matmul.py --json"
    warmup = 1
    repeat = 5
    timeout = 1800                         # per command (seconds)
    metrics = [{ name = "latency_ms", json = "$.latency_ms", better = "lower" },
               { name = "tflops", regex = "TFLOPS: ([0-9.]+)", better = "higher" }]
    env = { ZE_AFFINITY_MASK = "0" }
    exclusive = true                       # the whole machine: no other benchmark, build or test meanwhile
    gpus = [0]                             # with exclusive = false: only benchmarks using one of these GPUs wait
    threshold = 5                          # % change that counts as a regression (at least 2 standard deviations)

A run is a development task run of kind `bench` (development.tasks): one generated script in the worktree that marks
each part of its output (`@@waw-bench begin|end <variant> <phase> <i>`), so it survives restarts and dropped
connections like a build. When the task ends its log is parsed: each repeat's metrics (plus `wall_s`, the time the
command took) and their median / mean / stdev / min / max. An A/B run puts the baseline (and the candidate, unless it is
the workspace itself) in temporary worktrees and alternates them repeat by repeat (A B, B A, …) to cancel drift.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import shlex
import statistics
import tomllib
from typing import Any

from ..db import iso, new_id, utcnow
from ..errors import ApiError, bad_request, conflict, not_found
from ..models import BenchDef, BenchRun, DevTaskRun, DevWorkspace, Machine

log = logging.getLogger("waw.machines.bench")
FILE = ".waw/bench.toml"
MARK = "@@waw-bench"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
WALL = "wall_s"


# ---------------------------------------------------------------------- definitions
def _int(d: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{key} must be a whole number from {lo} to {hi}")
    return v


def _str(d: dict[str, Any], key: str, max_len: int, required: bool = False) -> str | None:
    v = d.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(v, str) or len(v) > max_len or "\0" in v:
        raise ValueError(f"{key} must be text of at most {max_len} characters")
    return v.strip()


def parse_spec(d: Any) -> dict[str, Any]:
    """A definition checked and completed with its defaults; ValueError says what is wrong."""
    if not isinstance(d, dict):
        raise ValueError("a benchmark is a table of settings")
    known = {"name", "setup", "command", "warmup", "repeat", "timeout", "metrics", "env", "exclusive", "gpus", "threshold"}
    extra = sorted(set(d) - known)
    if extra:
        raise ValueError("unknown setting: " + ", ".join(extra))
    name = _str(d, "name", 64, True)
    assert name is not None
    if not NAME_RE.match(name):
        raise ValueError("name: letters, digits, '.', '_' and '-' only (at most 64)")
    out: dict[str, Any] = {"name": name, "setup": _str(d, "setup", 8000), "command": _str(d, "command", 8000, True),
                           "warmup": _int(d, "warmup", 0, 0, 20), "repeat": _int(d, "repeat", 5, 1, 100),
                           "timeout": _int(d, "timeout", 1800, 1, 7 * 86400)}
    metrics = d.get("metrics", [])
    if not isinstance(metrics, list) or len(metrics) > 50:
        raise ValueError("metrics must be a list (at most 50)")
    ms: list[dict[str, Any]] = []
    for m in metrics:
        if not isinstance(m, dict):
            raise ValueError("each metric is a table: { name = …, json = … | regex = …, better = … }")
        mn = _str(m, "name", 64, True)
        assert mn is not None
        if not NAME_RE.match(mn) or mn == WALL or any(x["name"] == mn for x in ms):
            raise ValueError(f"metric name {mn!r}: letters, digits, '.', '_' and '-', unique, not {WALL}")
        jp, rx = _str(m, "json", 500), _str(m, "regex", 500)
        if bool(jp) == bool(rx):
            raise ValueError(f"metric {mn}: give either json (a path like $.latency_ms) or regex")
        if jp and not re.match(r"^\$(\.[A-Za-z0-9_-]+|\[\d+\])*$", jp):
            raise ValueError(f"metric {mn}: json is a path like $.result.latency_ms or $.runs[0].time")
        if rx:
            try:
                re.compile(rx)
            except re.error as exc:
                raise ValueError(f"metric {mn}: invalid regex: {exc}") from None
        better = m.get("better", "lower")
        if better not in ("lower", "higher"):
            raise ValueError(f"metric {mn}: better is lower or higher")
        ms.append({"name": mn, "json": jp, "regex": rx, "better": better, "unit": _str(m, "unit", 20)})
    out["metrics"] = ms
    env = d.get("env", {})
    if not isinstance(env, dict) or len(env) > 100:
        raise ValueError("env is a table of NAME = \"value\"")
    for k, v in env.items():
        if not ENV_RE.match(str(k)) or not isinstance(v, (str, int, float)) or isinstance(v, bool) or "\0" in str(v):
            raise ValueError(f"env {k}: a variable name and a text value")
    out["env"] = {str(k): str(v) for k, v in env.items()}
    exclusive = d.get("exclusive", True)
    if not isinstance(exclusive, bool):
        raise ValueError("exclusive is true or false")
    out["exclusive"] = exclusive
    gpus = d.get("gpus", [])
    if not isinstance(gpus, list) or any(isinstance(g, bool) or not isinstance(g, int) or not 0 <= g < 1024 for g in gpus):
        raise ValueError("gpus is a list of GPU numbers")
    out["gpus"] = sorted(set(gpus))
    th = d.get("threshold", 5)
    if isinstance(th, bool) or not isinstance(th, (int, float)) or not 0 <= th <= 1000:
        raise ValueError("threshold is a percentage from 0 to 1000")
    out["threshold"] = float(th)
    return out


def parse_file(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The definitions of a bench.toml and what is wrong with the others."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], [f"{FILE}: {exc}"]
    benches = data.get("bench", [])
    if not isinstance(benches, list):
        return [], [f"{FILE}: use [[bench]] tables"]
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, b in enumerate(benches):
        try:
            spec = parse_spec(b)
        except ValueError as exc:
            label = b.get("name") if isinstance(b, dict) and isinstance(b.get("name"), str) else f"#{i + 1}"
            errors.append(f"{FILE} [[bench]] {label}: {exc}")
            continue
        if any(x["name"] == spec["name"] for x in out):
            errors.append(f"{FILE}: {spec['name']} is defined twice (the first one is used)")
            continue
        out.append(spec)
    return out, errors


def to_toml(spec: dict[str, Any]) -> str:
    """One definition as a [[bench]] table (to copy into .waw/bench.toml)."""
    def q(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)

    lines = ["[[bench]]", f"name = {q(spec['name'])}"]
    if spec.get("setup"):
        lines.append(f"setup = {q(spec['setup'])}")
    lines.append(f"command = {q(spec['command'])}")
    lines.extend(f"{k} = {spec[k]}" for k in ("warmup", "repeat", "timeout"))
    if spec.get("metrics"):
        lines.append("metrics = [")
        for m in spec["metrics"]:
            parts = [f"name = {q(m['name'])}", f"json = {q(m['json'])}" if m.get("json") else f"regex = {q(m['regex'])}",
                     f"better = {q(m['better'])}"] + ([f"unit = {q(m['unit'])}"] if m.get("unit") else [])
            lines.append("  { " + ", ".join(parts) + " },")
        lines.append("]")
    if spec.get("env"):
        lines.append("env = { " + ", ".join(f"{k} = {q(v)}" for k, v in spec["env"].items()) + " }")
    lines.append(f"exclusive = {'true' if spec['exclusive'] else 'false'}")
    if spec.get("gpus"):
        lines.append(f"gpus = {spec['gpus']}")
    lines.append(f"threshold = {spec['threshold']:g}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- the script and its output
_PRELUDE = r'''_waw_t() { t=$(date +%s%N 2>/dev/null); case "$t" in *N*|"") t="$(date +%s)000000000";; esac; echo "$t"; }
if command -v timeout >/dev/null 2>&1; then _waw_tmo() { timeout "$@"; }; else _waw_tmo() { shift; "$@"; }; fi
_waw_run() {
  echo "@@waw-bench begin $1 $2 $3"
  t0=$(_waw_t)
  (cd "$4" && _waw_tmo "$5" sh -c "$6"); rc=$?
  t1=$(_waw_t)
  echo
  echo "@@waw-bench end $1 $2 $3 rc=$rc ns=$((t1 - t0))"
  return $rc
}
_waw_setup() {
  m=$(cd "$2" && git rev-parse --path-format=absolute --git-path "waw-bench-setup-$5" 2>/dev/null)
  if [ -n "$m" ] && [ "$(cat "$m" 2>/dev/null)" = "$3" ]; then echo "@@waw-bench skip $1 setup 0"; return 0; fi
  _waw_run "$1" setup 0 "$2" "$4" "$6" || return $?
  [ -z "$m" ] || echo "$3" > "$m"
}
'''


def setup_key(spec: dict[str, Any], commit: str | None, dirty: str | None) -> str:
    return hashlib.sha256(json.dumps([spec.get("setup"), spec.get("env"), commit, dirty]).encode()).hexdigest()[:16]


def build_script(spec: dict[str, Any], sides: list[dict[str, Any]]) -> str:
    """`sides`: [{variant, dir, key}] — one for a run, two (a, b) for an A/B run."""
    p = [_PRELUDE]
    for k, v in spec["env"].items():
        p.append(f"{k}={shlex.quote(v)}; export {k}")
    tmo, cmd = str(spec["timeout"]), shlex.quote(spec["command"])
    if spec.get("setup"):
        p.extend(f"_waw_setup {s['variant']} {shlex.quote(s['dir'])} {s['key']} {tmo} {shlex.quote(spec['name'])} "
                 f"{shlex.quote(spec['setup'])} || exit $?" for s in sides)
    p.extend(f"_waw_run {s['variant']} warmup {i} {shlex.quote(s['dir'])} {tmo} {cmd} || exit $?"
             for i in range(spec["warmup"]) for s in sides)
    p.extend(f"_waw_run {s['variant']} repeat {i} {shlex.quote(s['dir'])} {tmo} {cmd} || exit $?"
             for i in range(spec["repeat"]) for s in (sides if i % 2 == 0 else sides[::-1]))
    return "\n".join(p) + "\n"


def split_log(text: str) -> list[dict[str, Any]]:
    """The parts of a run's log: [{variant, phase, i, out, rc, ns}] (rc None: cut off)."""
    parts: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith(MARK + " "):
            f = line.split()
            if len(f) >= 5 and f[1] == "begin":
                cur, buf = {"variant": f[2], "phase": f[3], "i": int(f[4]) if f[4].isdigit() else 0, "rc": None, "ns": None}, []
                continue
            if len(f) >= 5 and f[1] == "end" and cur is not None:
                kv = dict(x.split("=", 1) for x in f[5:] if "=" in x)
                cur["rc"] = int(kv["rc"]) if kv.get("rc", "").lstrip("-").isdigit() else None
                cur["ns"] = int(kv["ns"]) if kv.get("ns", "").lstrip("-").isdigit() else None
                cur["out"] = "\n".join(buf[:-1] if buf and buf[-1] == "" else buf)
                parts.append(cur)
                cur = None
                continue
            if len(f) >= 5 and f[1] == "skip":
                parts.append({"variant": f[2], "phase": f[3], "i": 0, "rc": 0, "ns": None, "out": "", "skipped": True})
                continue
        if cur is not None:
            buf.append(line)
    if cur is not None:
        cur["out"] = "\n".join(buf)
        parts.append(cur)
    return parts


def _json_values(out: str) -> list[Any]:
    """JSON documents in a command's output: the whole of it, or else each line that is one (the last wins)."""
    s = out.strip()
    try:
        return [json.loads(s)]
    except ValueError:
        pass
    vals = []
    for line in s.splitlines():
        line = line.strip()
        if line[:1] in "{[":
            try:
                vals.append(json.loads(line))
            except ValueError:
                pass
    return vals


def _walk(doc: Any, path: str) -> Any:
    for key, idx in re.findall(r"\.([A-Za-z0-9_-]+)|\[(\d+)\]", path[1:]):
        if key:
            if not isinstance(doc, dict) or key not in doc:
                return None
            doc = doc[key]
        else:
            if not isinstance(doc, list) or int(idx) >= len(doc):
                return None
            doc = doc[int(idx)]
    return doc


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def extract(metric: dict[str, Any], out: str) -> float | None:
    if metric.get("json"):
        for doc in reversed(_json_values(out)):
            v = _num(_walk(doc, metric["json"]))
            if v is not None:
                return v
        return None
    found = re.findall(metric["regex"], out, re.MULTILINE)
    if not found:
        return None
    last = found[-1]
    return _num(last[0] if isinstance(last, tuple) else last)


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": statistics.median(values), "mean": statistics.fmean(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0, "min": min(values), "max": max(values)}


def results(spec: dict[str, Any], parts: list[dict[str, Any]], variant: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Each repeat's values, their summary per metric, and what could not be read."""
    samples: list[dict[str, Any]] = []
    problems: list[str] = []
    for p in parts:
        if p["variant"] != variant or p["phase"] != "repeat":
            continue
        vals: dict[str, float | None] = {WALL: round(p["ns"] / 1e9, 6) if p.get("ns") is not None else None}
        for m in spec["metrics"]:
            v = extract(m, p.get("out", "")) if p.get("rc") == 0 else None
            vals[m["name"]] = v
            if v is None and p.get("rc") == 0:
                problems.append(f"repeat {p['i'] + 1}: {m['name']} not found in the output")
        samples.append({"i": p["i"], "rc": p.get("rc"), "values": vals})
    samples.sort(key=lambda x: x["i"])
    summary: dict[str, Any] = {}
    for m in [{"name": WALL, "better": "lower", "unit": "s"}, *spec["metrics"]]:
        vs = [s["values"][m["name"]] for s in samples if s["rc"] == 0 and s["values"].get(m["name"]) is not None]
        summary[m["name"]] = {**summarize(vs), "better": m["better"], "unit": m.get("unit")}
    return samples, summary, problems


def compare(a: dict[str, Any], b: dict[str, Any], threshold: float) -> list[dict[str, Any]]:
    """b against the baseline a, per metric: change in %, and whether it is beyond the noise (2 stdev) and threshold."""
    out = []
    for name, sb in (b.get("summary") or {}).items():
        sa = (a.get("summary") or {}).get(name)
        row: dict[str, Any] = {"name": name, "better": sb.get("better"), "unit": sb.get("unit"), "a": sa, "b": sb,
                               "change_pct": None, "verdict": "missing"}
        if sa and sa.get("n") and sb.get("n") and sa.get("median"):
            change = (sb["median"] - sa["median"]) / abs(sa["median"]) * 100
            noise = 2 * max(sa.get("stdev") or 0, sb.get("stdev") or 0) / abs(sa["median"]) * 100
            limit = max(threshold, noise)
            worse = change > 0 if sb.get("better") == "lower" else change < 0
            row.update(change_pct=change, limit_pct=limit,
                       verdict="same" if abs(change) <= limit else ("regression" if worse else "improvement"))
        out.append(row)
    return out


def _gpus(caps: dict[str, Any] | None) -> list[tuple[Any, Any]]:
    return sorted(((g.get("name"), g.get("driver")) for g in (caps or {}).get("gpus") or []), key=str)


def warnings(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    w = []
    if a["machine"] != b["machine"]:
        w.append(f"different machines: {a['machine']} and {b['machine']}")
    elif _gpus(a.get("caps")) != _gpus(b.get("caps")):
        w.append("the GPUs or their drivers changed between the two runs")
    if a.get("env_hash") != b.get("env_hash"):
        w.append("the machine's setup (recipes) changed between the two runs")
    if (a.get("spec") or {}).get("command") != (b.get("spec") or {}).get("command"):
        w.append("the benchmark command is not the same")
    return w


# ---------------------------------------------------------------------- the service
def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def def_dict(r: BenchDef) -> dict[str, Any]:
    return {"id": r.id, "scope": r.scope, "scope_key": r.scope_key, "name": r.name, "spec": _loads(r.spec_json, {}),
            "updated_at": iso(r.updated_at)}


def run_dict(r: BenchRun, full: bool = False) -> dict[str, Any]:
    d = {"id": r.id, "dev_id": r.dev_id, "repo": r.repo, "name": r.def_name, "source": r.def_source, "machine": r.machine,
         "kind": r.kind, "variant": r.variant, "ab_id": r.ab_id, "label": r.label, "branch": r.branch, "commit": r.commit,
         "dirty": r.dirty_hash is not None, "dirty_hash": r.dirty_hash, "env_hash": r.env_hash, "status": r.status,
         "error": r.error, "task_run_id": r.task_run_id, "baseline_id": r.baseline_id, "summary": _loads(r.summary_json, {}),
         "lock": _loads(r.lock_json, None), "started_at": iso(r.started_at), "finished_at": iso(r.finished_at)}
    if full:
        d.update(spec=_loads(r.spec_json, {}), samples=_loads(r.samples_json, []), caps=_loads(r.caps_json, None))
    return d


class Bench:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.locks: dict[str, dict[str, dict[str, Any]]] = {}  # machine → task run id → {"machine": True} | {"gpus": [...]}
        self._mutex = asyncio.Lock()
        tasks = ctx.extra.get("dev_tasks")
        if tasks is not None:
            tasks.finish_hooks.append(self._on_task_end)

    # ---------------------------------------------------------------- definitions
    def _svc(self):
        from ..development.service import DevService

        return DevService(self.ctx)

    async def _file_defs(self, row: DevWorkspace) -> tuple[list[dict[str, Any]], list[str]]:
        if row.status != "ready" or not row.worktree_path:
            return [], []
        from ..development.targets import TargetError

        try:
            t = self._svc().target(row)
            rc, out, _ = await t.run(["sh", "-c", 'f="$1/' + FILE + '"; [ -f "$f" ] || exit 3; head -c 1048576 "$f"', "_",
                                      row.worktree_path], timeout=30)
        except (TargetError, ApiError) as exc:
            return [], [f"could not read {FILE}: {getattr(exc, 'message', exc)}"]
        if rc == 3:
            return [], []
        if rc != 0:
            return [], [f"could not read {FILE}"]
        return parse_file(out)

    def _app_defs(self, row: DevWorkspace) -> list[BenchDef]:
        with self.ctx.db.read() as s:
            q = s.query(BenchDef).filter(((BenchDef.scope == "workspace") & (BenchDef.scope_key == row.id))
                                         | ((BenchDef.scope == "repo") & (BenchDef.scope_key == (row.github_repo or "\0"))))
            return q.order_by(BenchDef.name).all()

    async def definitions(self, dev_id: str) -> dict[str, Any]:
        """What can run in this workspace: the repository file first, then the workspace's own, then the repository's."""
        row = self._svc().get(dev_id)
        file_defs, errors = await self._file_defs(row)
        out = [{"name": d["name"], "source": "file", "spec": d, "id": None, "shadowed": False, "toml": to_toml(d)} for d in file_defs]
        names = {d["name"] for d in file_defs}
        app = sorted(self._app_defs(row), key=lambda r: (r.scope != "workspace", r.name))
        for r in app:
            spec = _loads(r.spec_json, {})
            out.append({"name": r.name, "source": r.scope, "spec": spec, "id": r.id, "shadowed": r.name in names, "toml": to_toml(spec)})
            names.add(r.name)
        return {"defs": out, "errors": errors, "file": FILE, "repo": row.github_repo}

    async def resolve(self, dev_id: str, name: str) -> tuple[dict[str, Any], str]:
        for d in (await self.definitions(dev_id))["defs"]:
            if d["name"] == name and not d["shadowed"]:
                return d["spec"], "file" if d["source"] == "file" else "app"
        raise not_found(f"no benchmark named {name}", "BENCH_NOT_FOUND")

    def save_def(self, body: dict[str, Any], def_id: str | None = None) -> dict[str, Any]:
        if body.get("toml"):  # the same [[bench]] table as in .waw/bench.toml
            found, errors = parse_file(body["toml"])
            if errors or len(found) != 1:
                raise bad_request(errors[0] if errors else "give exactly one [[bench]] table", "INVALID_BENCH")
            spec = found[0]
        else:
            try:
                spec = parse_spec(body.get("spec"))
            except ValueError as exc:
                raise bad_request(str(exc), "INVALID_BENCH")
        with self.ctx.db.write() as s:
            if def_id:
                r = s.get(BenchDef, def_id)
                if r is None:
                    raise not_found("benchmark not found", "BENCH_NOT_FOUND")
            else:
                scope, key = body.get("scope"), body.get("scope_key")
                if scope not in ("repo", "workspace") or not isinstance(key, str) or not key:
                    raise bad_request("scope is repo (with owner/name) or workspace (with its id)", "INVALID_BENCH")
                r = BenchDef(id=new_id(), scope=scope, scope_key=key, name=spec["name"], spec_json="{}")
            with s.no_autoflush:
                clash = s.query(BenchDef).filter(BenchDef.scope == r.scope, BenchDef.scope_key == r.scope_key,
                                                 BenchDef.name == spec["name"], BenchDef.id != r.id).first()
            if clash is not None:
                raise conflict("BENCH_EXISTS", f"a benchmark named {spec['name']} already exists there")
            if not def_id:
                s.add(r)
            r.name, r.spec_json, r.updated_at = spec["name"], json.dumps(spec), utcnow()
            s.flush()
            out = def_dict(r)
        self._event({"type": "bench_defs_changed"})
        return out

    def delete_def(self, def_id: str) -> None:
        with self.ctx.db.write() as s:
            r = s.get(BenchDef, def_id)
            if r is None:
                raise not_found("benchmark not found", "BENCH_NOT_FOUND")
            s.delete(r)
        self._event({"type": "bench_defs_changed"})

    # ---------------------------------------------------------------- locks
    def _busy(self, machine: str, lock: dict[str, Any] | None) -> str | None:
        held = self.locks.get(machine, {})
        for other in held.values():
            if other.get("machine"):
                return "a benchmark has the whole machine"
            if lock and lock.get("machine"):
                return "another benchmark is running there"
            if lock and set(lock.get("gpus", [])) & set(other.get("gpus", [])):
                return "another benchmark uses one of its GPUs"
        if lock and lock.get("machine"):
            with self.ctx.db.read() as s:
                n = (s.query(DevTaskRun).join(DevWorkspace, DevWorkspace.id == DevTaskRun.dev_id)
                     .filter(DevTaskRun.status == "running", DevTaskRun.kind != "bench", DevWorkspace.host == machine).count())
            if n:
                return f"{n} build or test command{'s are' if n > 1 else ' is'} running there"
        return None

    def check_task_allowed(self, machine: str) -> None:
        if any(x.get("machine") for x in self.locks.get(machine, {}).values()):
            raise conflict("MACHINE_BUSY", f"a benchmark has {'this server' if machine == 'local' else machine} to itself; "
                                           "wait for it to end or cancel it")

    @staticmethod
    def lock_for(spec: dict[str, Any]) -> dict[str, Any] | None:
        if spec["exclusive"]:
            return {"machine": True}
        return {"gpus": spec["gpus"]} if spec["gpus"] else None

    def _release(self, machine: str, task_run_id: str) -> None:
        held = self.locks.get(machine)
        if held is not None:
            held.pop(task_run_id, None)
            if not held:
                self.locks.pop(machine, None)

    # ---------------------------------------------------------------- runs
    def _caps(self, machine: str) -> dict[str, Any] | None:
        if machine == "local":
            from ..kv import kv_get

            return (kv_get(self.ctx, "machine_local", {}) or {}).get("capabilities")
        with self.ctx.db.read() as s:
            m = s.get(Machine, machine)
            return _loads(m.capabilities_json, None) if m else None

    def _env_hash(self, machine: str) -> str | None:
        recipes = self.ctx.extra.get("recipes")
        if machine == "local" or recipes is None:
            return None
        rs = [(r["id"], r.get("version_ok"), r["status"]) for r in recipes.for_machine(machine)]
        return hashlib.sha256(json.dumps(rs).encode()).hexdigest()[:16] if rs else None

    @staticmethod
    async def _state(t: Any, wt: str) -> tuple[str | None, str | None]:
        """The commit checked out and a hash of the uncommitted changes (None: none)."""
        rc, head, _ = await t.git(wt, ["rev-parse", "HEAD"])
        commit = head.strip() if rc == 0 else None
        rc, st, _ = await t.git(wt, ["status", "--porcelain=v1", "--untracked-files=all"])
        if rc != 0 or not st.strip():
            return commit, None
        _, diff, _ = await t.git(wt, ["diff", "HEAD"], timeout=120)
        return commit, hashlib.sha256((st + "\0" + diff).encode()).hexdigest()[:16]

    async def start(self, dev_id: str, name: str, label: str | None = None) -> dict[str, Any]:
        svc = self._svc()
        row, t = svc._ready(dev_id)
        spec, source = await self.resolve(dev_id, name)
        commit, dirty = await self._state(t, row.worktree_path)
        sides = [{"variant": "a", "dir": row.worktree_path, "key": setup_key(spec, commit, dirty)}]
        task_id = new_id()
        bench_id = new_id()
        async with self._mutex:
            lock = self.lock_for(spec)
            why = self._busy(row.host, lock)
            if why:
                raise conflict("MACHINE_BUSY", f"cannot start the benchmark now: {why}")
            with self.ctx.db.write() as s:
                s.add(BenchRun(id=bench_id, dev_id=dev_id, repo=row.github_repo, def_name=name, def_source=source,
                               spec_json=json.dumps(spec), machine=row.host, kind="run", label=label, branch=row.branch,
                               commit=commit, dirty_hash=dirty, env_hash=self._env_hash(row.host),
                               caps_json=json.dumps(self._caps(row.host)), lock_json=json.dumps(lock) if lock else None,
                               status="running", task_run_id=task_id))
            if lock:
                self.locks.setdefault(row.host, {})[task_id] = lock
        await self._launch(dev_id, spec, sides, task_id, [bench_id], f"benchmark {name}")
        self._event({"type": "bench_run", "dev_id": dev_id, "run_id": bench_id, "status": "running"})
        return self.get(bench_id)

    async def start_ab(self, dev_id: str, name: str, baseline: str, candidate: str | None) -> dict[str, Any]:
        """Baseline commit (a) against a candidate commit or, by default, the workspace as it is (b)."""
        svc = self._svc()
        row, t = svc._ready(dev_id)
        spec, source = await self.resolve(dev_id, name)
        repo = row.source_repo if row.mode != "folder" else row.worktree_path

        async def sha(ref: str) -> str:
            if not ref or ref.startswith("-") or len(ref) > 256:
                raise bad_request("give a branch, tag or commit", "INVALID_REF")
            rc, out, _ = await t.git(row.worktree_path, ["rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"])
            if rc != 0:
                raise bad_request(f"{ref} is not a commit here", "INVALID_REF")
            return out.strip()

        a_sha = await sha(baseline)
        b_sha = await sha(candidate) if candidate else None
        ab_id = new_id()
        if t.is_local:
            base = str(self.ctx.settings.state_dir / "bench" / ab_id)
        else:
            base = f"{(await t.expand(t.host.work_dir)).rstrip('/')}/bench/{ab_id}"
        made: list[str] = []
        sides = []
        try:
            for variant, ref_sha in (("a", a_sha), ("b", b_sha)):
                if ref_sha is None:
                    commit, dirty = await self._state(t, row.worktree_path)
                    sides.append({"variant": variant, "dir": row.worktree_path, "commit": commit, "dirty": dirty})
                    continue
                d = f"{base}/{variant}"
                await t.mkdir(base)
                rc, _, err = await t.git(repo, ["worktree", "add", "--detach", d, ref_sha], timeout=600)
                if rc != 0:
                    raise ApiError(400, "BENCH_WORKTREE_FAILED", f"could not check out {ref_sha[:12]}: {err.strip()[-300:]}")
                made.append(d)
                sides.append({"variant": variant, "dir": d, "commit": ref_sha, "dirty": None})
        except BaseException:
            await self._remove_worktrees(t, repo, made, base)
            raise
        for sd in sides:
            sd["key"] = setup_key(spec, sd["commit"], sd["dirty"])
        task_id = new_id()
        ids = {"a": new_id(), "b": new_id()}
        async with self._mutex:
            lock = self.lock_for(spec)
            why = self._busy(row.host, lock)
            if why:
                await self._remove_worktrees(t, repo, made, base)
                raise conflict("MACHINE_BUSY", f"cannot start the benchmark now: {why}")
            with self.ctx.db.write() as s:
                for sd in sides:
                    v = sd["variant"]
                    s.add(BenchRun(id=ids[v], dev_id=dev_id, repo=row.github_repo, def_name=name, def_source=source,
                                   spec_json=json.dumps(spec), machine=row.host, kind="ab", variant=v, ab_id=ab_id,
                                   label=(baseline if v == "a" else (candidate or "this workspace")),
                                   branch=baseline if v == "a" else (None if candidate else row.branch),
                                   commit=sd["commit"], dirty_hash=sd["dirty"], env_hash=self._env_hash(row.host),
                                   caps_json=json.dumps(self._caps(row.host)), lock_json=json.dumps(lock) if lock else None,
                                   status="running", task_run_id=task_id, baseline_id=ids["a"] if v == "b" else None,
                                   worktrees_json=json.dumps({"repo": repo, "dirs": made, "base": base}) if v == "a" else None))
            if lock:
                self.locks.setdefault(row.host, {})[task_id] = lock
        await self._launch(dev_id, spec, sides, task_id, list(ids.values()), f"A/B {name}: {baseline} vs {candidate or 'this workspace'}")
        self._event({"type": "bench_run", "dev_id": dev_id, "run_id": ids["b"], "status": "running"})
        return {"ab_id": ab_id, "a": self.get(ids["a"]), "b": self.get(ids["b"])}

    async def _launch(self, dev_id: str, spec: dict[str, Any], sides: list[dict[str, Any]], task_id: str,
                      bench_ids: list[str], label: str) -> None:
        tasks = self.ctx.extra["dev_tasks"]
        try:
            await tasks.start(dev_id, "bench", label, script=build_script(spec, sides), run_id=task_id)
        except BaseException as exc:
            with self.ctx.db.read() as s:
                started = s.get(DevTaskRun, task_id) is not None
            if not started:  # otherwise the task's end finishes the runs
                self._finish(bench_ids, "failed", getattr(exc, "message", None) or str(exc) or "could not start")
            raise

    def get(self, bench_id: str) -> dict[str, Any]:
        with self.ctx.db.read() as s:
            r = s.get(BenchRun, bench_id)
            if r is None:
                raise not_found("benchmark run not found", "BENCH_RUN_NOT_FOUND")
            return run_dict(r, full=True)

    def list(self, *, dev_id: str | None = None, machine: str | None = None, name: str | None = None,
             repo: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            q = s.query(BenchRun)
            if dev_id:
                q = q.filter(BenchRun.dev_id == dev_id)
            if machine:
                q = q.filter(BenchRun.machine == machine)
            if name:
                q = q.filter(BenchRun.def_name == name)
            if repo:
                q = q.filter(BenchRun.repo == repo)
            return [run_dict(r) for r in q.order_by(BenchRun.started_at.desc()).limit(max(1, min(limit, 1000))).all()]

    def delete(self, bench_id: str) -> None:
        with self.ctx.db.write() as s:
            r = s.get(BenchRun, bench_id)
            if r is None:
                raise not_found("benchmark run not found", "BENCH_RUN_NOT_FOUND")
            if r.status == "running":
                raise conflict("BENCH_RUNNING", "cancel the run first")
            rows = s.query(BenchRun).filter(BenchRun.ab_id == r.ab_id).all() if r.ab_id else [r]
            for x in rows:
                s.delete(x)
        self._event({"type": "bench_run", "run_id": bench_id, "status": "deleted"})

    def default_baseline(self, bench_id: str) -> str | None:
        """The run's own baseline (A/B), else the latest finished run of the same benchmark on the same machine of the
        base branch, else the previous one in the same workspace."""
        with self.ctx.db.read() as s:
            r = s.get(BenchRun, bench_id)
            if r is None:
                raise not_found("benchmark run not found", "BENCH_RUN_NOT_FOUND")
            if r.baseline_id:
                return r.baseline_id
            dev = s.get(DevWorkspace, r.dev_id)
            base = ((dev.base_ref or "") if dev else "").removeprefix("refs/waw/base/").removeprefix("origin/")
            q = s.query(BenchRun).filter(BenchRun.def_name == r.def_name, BenchRun.machine == r.machine,
                                         BenchRun.status == "passed", BenchRun.id != r.id, BenchRun.started_at < r.started_at)
            if base:
                same_repo = (BenchRun.repo == r.repo) if r.repo else (BenchRun.dev_id == r.dev_id)
                hit = q.filter(same_repo, BenchRun.branch.in_([base, f"origin/{base}"])).order_by(BenchRun.started_at.desc()).first()
                if hit:
                    return hit.id
            hit = q.filter(BenchRun.dev_id == r.dev_id).order_by(BenchRun.started_at.desc()).first()
            return hit.id if hit else None

    def compare(self, a_id: str | None, b_id: str) -> dict[str, Any]:
        b = self.get(b_id)
        a_id = a_id or self.default_baseline(b_id)
        if not a_id:
            return {"a": None, "b": b, "metrics": [], "warnings": [], "regressions": 0}
        a = self.get(a_id)
        th = float((b.get("spec") or {}).get("threshold", 5))
        metrics = compare(a, b, th)
        return {"a": a, "b": b, "metrics": metrics, "warnings": warnings(a, b), "threshold": th,
                "regressions": sum(1 for m in metrics if m["verdict"] == "regression")}

    def report(self, cmp: dict[str, Any]) -> str:
        """The comparison as text for the agent."""
        a, b = cmp["a"], cmp["b"]

        def who(x: dict[str, Any]) -> str:
            return (f"{x['label'] or x['branch'] or ''} commit {(x['commit'] or '?')[:12]}{' (with uncommitted changes)' if x['dirty'] else ''}"
                    f" on {x['machine']}, {x['started_at']}")

        lines = [f"Benchmark `{b['name']}` (`{(b.get('spec') or {}).get('command')}`).", f"Baseline: {who(a)}", f"Candidate: {who(b)}"]
        lines += [f"Warning: {w}" for w in cmp["warnings"]]
        lines.append("| metric | better | baseline median (stdev) | candidate median (stdev) | change | verdict |")
        lines.append("|---|---|---|---|---|---|")
        for m in cmp["metrics"]:
            def f(sm: dict[str, Any] | None) -> str:
                return f"{sm['median']:.6g} ({sm.get('stdev', 0):.3g})" if sm and sm.get("n") else "—"

            ch = f"{m['change_pct']:+.2f}%" if m["change_pct"] is not None else "—"
            lines.append(f"| {m['name']} | {m['better']} | {f(m['a'])} | {f(m['b'])} | {ch} | {m['verdict']} |")
        return "\n".join(lines)

    def export(self, runs: list[dict[str, Any]], fmt: str) -> str:
        if fmt == "json":
            return json.dumps([self.get(r["id"]) for r in runs], indent=1)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["run", "benchmark", "machine", "kind", "variant", "label", "branch", "commit", "dirty", "status", "started_at",
                    "metric", "n", "median", "mean", "stdev", "min", "max", "unit"])
        for r in runs:
            for name, sm in (r.get("summary") or {}).items():
                w.writerow([r["id"], r["name"], r["machine"], r["kind"], r["variant"] or "", r["label"] or "", r["branch"] or "",
                            r["commit"] or "", int(r["dirty"]), r["status"], r["started_at"], name, sm.get("n", 0),
                            *(sm.get(k, "") for k in ("median", "mean", "stdev", "min", "max")), sm.get("unit") or ""])
        return buf.getvalue()

    # ---------------------------------------------------------------- the end of a run
    def _on_task_end(self, task_run_id: str, status: str, rc: int | None) -> None:
        with self.ctx.db.read() as s:
            rows = s.query(BenchRun).filter(BenchRun.task_run_id == task_run_id, BenchRun.status == "running").all()
            if not rows:
                return
            task = s.get(DevTaskRun, task_run_id)
            log_path = task.log_path if task else None
            ids = [r.id for r in rows]
            machine = rows[0].machine
            cleanup = next((_loads(r.worktrees_json, None) for r in rows if r.worktrees_json), None)
            dev_id = rows[0].dev_id
        self._release(machine, task_run_id)
        text = ""
        if log_path:
            try:
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                pass
        parts = split_log(text.replace("\r\n", "\n"))
        failed = next((p for p in parts if p.get("rc") not in (0, None) or (p.get("rc") is None and not p.get("skipped"))), None)
        with self.ctx.db.write() as s:
            for bid in ids:
                r = s.get(BenchRun, bid)
                if r is None:
                    continue
                spec = _loads(r.spec_json, {})
                samples, summary, problems = results(spec, parts, r.variant or "a")
                r.samples_json, r.summary_json = json.dumps(samples), json.dumps(summary)
                if status in ("cancelled", "interrupted"):
                    r.status, r.error = status, None
                elif status != "passed" or failed is not None:
                    r.status = "failed"
                    if failed is not None:
                        what = "setup" if failed["phase"] == "setup" else f"{failed['phase']} {failed['i'] + 1}"
                        how = "timed out" if failed.get("rc") == 124 else (f"exit code {failed['rc']}" if failed.get("rc") is not None else "cut off")
                        r.error = f"{what} ({'baseline' if failed['variant'] == 'a' else 'candidate'} side) {how}" if r.kind == "ab" else f"{what}: {how}"
                    else:
                        r.error = f"the run ended with exit code {rc}" if rc is not None else "the run ended"
                elif problems:
                    r.status, r.error = "failed", "; ".join(problems[:5])
                else:
                    r.status, r.error = "passed", None
                r.finished_at = utcnow()
        if cleanup and cleanup.get("dirs"):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(self._cleanup(dev_id, cleanup))
        for bid in ids:
            self._event({"type": "bench_run", "dev_id": dev_id, "run_id": bid, "status": "finished"})

    async def _cleanup(self, dev_id: str, c: dict[str, Any]) -> None:
        try:
            with self.ctx.db.read() as s:
                row = s.get(DevWorkspace, dev_id)
            if row is None:
                return
            await self._remove_worktrees(self._svc().target(row), c["repo"], c["dirs"], c.get("base"))
        except Exception:  # noqa: BLE001 - leftovers are pruned by git later
            log.exception("could not remove the A/B worktrees of %s", dev_id)

    @staticmethod
    async def _remove_worktrees(t: Any, repo: str, dirs: list[str], base: str | None) -> None:
        for d in dirs:
            await t.git(repo, ["worktree", "remove", "--force", d], timeout=300)
        if dirs:
            await t.git(repo, ["worktree", "prune"])
        if base:
            await t.run(["rmdir", base], timeout=30)

    def _finish(self, ids: list[str], status: str, error: str | None) -> None:
        with self.ctx.db.write() as s:
            for bid in ids:
                r = s.get(BenchRun, bid)
                if r is not None and r.status == "running":
                    r.status, r.error, r.finished_at = status, error, utcnow()
                    if r.task_run_id:
                        self._release(r.machine, r.task_run_id)

    def reconcile(self) -> int:
        """At startup: the locks of runs still going (on a machine, in tmux), the end of those whose command ended meanwhile."""
        with self.ctx.db.read() as s:
            rows = s.query(BenchRun, DevTaskRun).outerjoin(DevTaskRun, DevTaskRun.id == BenchRun.task_run_id).filter(
                BenchRun.status == "running").all()
        ended: dict[str, tuple[str, int | None]] = {}
        orphans: list[str] = []
        for b, t in rows:
            if t is None:
                orphans.append(b.id)
            elif t.status == "running":
                lock = _loads(b.lock_json, None)
                if lock:
                    self.locks.setdefault(b.machine, {})[t.id] = lock
            else:
                ended[t.id] = (t.status, t.exit_code)
        if orphans:
            self._finish(orphans, "interrupted", None)
        for tid, (st, rc) in ended.items():
            self._on_task_end(tid, st, rc)
        return len(rows)

    def _event(self, ev: dict[str, Any]) -> None:
        if self.ctx.events:
            self.ctx.events.publish("global", ev)

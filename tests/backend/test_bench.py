"""Benchmarks (Phase 5): definitions (.waw/bench.toml wins over the app's), the generated script and reading its
output (metrics from JSON or a regex, wall time, statistics), runs holding the machine, comparison with a baseline and
its regression flag, A/B runs of two commits interleaved, export, and asking the agent to explain."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from test_development import git, make_source, new_branch_ws
from test_machine_recipes import rsettings  # noqa: F401 - fixture

from backend.app.development import targets
from backend.app.machines import bench

SPEC = {"name": "b1", "command": "cat value.txt", "repeat": 3, "warmup": 1,
        "metrics": [{"name": "v", "regex": r"v=([0-9.]+)", "better": "lower"},
                    {"name": "j", "json": "$.r[1].x", "better": "higher"}]}


@pytest.fixture(autouse=True)
def clean():
    targets._HOMES.clear()
    yield
    targets._HOMES.clear()


# ---------------------------------------------------------------------------------------------- units
def test_parse_spec_defaults_and_errors():
    s = bench.parse_spec({"name": "x", "command": "true"})
    assert s["warmup"] == 0 and s["repeat"] == 5 and s["timeout"] == 1800 and s["exclusive"] is True and s["metrics"] == []
    assert s["threshold"] == 5.0 and s["gpus"] == [] and s["env"] == {}
    bad = [{"command": "true"}, {"name": "x"}, {"name": "a b", "command": "c"}, {"name": "x", "command": "c", "repeat": 0},
           {"name": "x", "command": "c", "metrics": [{"name": "m"}]},
           {"name": "x", "command": "c", "metrics": [{"name": "m", "json": "latency"}]},
           {"name": "x", "command": "c", "metrics": [{"name": "m", "regex": "("}]},
           {"name": "x", "command": "c", "metrics": [{"name": "wall_s", "regex": "x"}]},
           {"name": "x", "command": "c", "env": {"1A": "x"}}, {"name": "x", "command": "c", "nope": 1},
           {"name": "x", "command": "c", "gpus": ["0"]}, {"name": "x", "command": "c", "exclusive": "yes"}]
    for b in bad:
        with pytest.raises(ValueError):
            bench.parse_spec(b)


def test_parse_file_and_toml_round_trip():
    spec = bench.parse_spec({**SPEC, "setup": "echo 'it''s'", "env": {"A": "1 2"}, "gpus": [1, 0], "exclusive": False})
    defs, errors = bench.parse_file(bench.to_toml(spec))
    assert errors == [] and defs == [spec]
    defs, errors = bench.parse_file('[[bench]]\nname = "a"\ncommand = "x"\n[[bench]]\nname = "a"\ncommand = "y"\n'
                                    '[[bench]]\nname = "b"\n')
    assert [d["command"] for d in defs] == ["x"] and len(errors) == 2
    assert bench.parse_file("[[bench]\n")[1]


def test_extract_and_summary():
    assert bench.extract({"json": "$.a.b"}, 'noise\n{"a": {"b": 3}}\n{"a": {"b": "4.5"}}\n') == 4.5
    assert bench.extract({"json": "$.r[1].x"}, '{"r": [{"x": 1}, {"x": 2}]}') == 2
    assert bench.extract({"json": "$.a"}, "no json") is None
    assert bench.extract({"regex": r"T: ([0-9.]+)"}, "T: 1\nT: 2.5\n") == 2.5
    assert bench.extract({"regex": r"\d+"}, "n 7") == 7
    s = bench.summarize([1.0, 2.0, 6.0])
    assert s["median"] == 2.0 and s["mean"] == 3.0 and s["min"] == 1.0 and s["max"] == 6.0 and round(s["stdev"], 3) == 2.646


def test_script_output_is_read_back(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d, v in ((a, 10), (b, 12)):
        d.mkdir()
        (d / "value.txt").write_text(f'v={v}\n{{"r": [{{"x": 0}}, {{"x": {v * 2}}}]}}\n')
        subprocess.run(["git", "init", "-q", str(d)], check=True)
    spec = bench.parse_spec({**SPEC, "setup": "echo setup >> ran.txt", "env": {"BENCH_X": "it's"},
                             "command": "cat value.txt; echo x=$BENCH_X"})
    sides = [{"variant": "a", "dir": str(a), "key": "k1"}, {"variant": "b", "dir": str(b), "key": "k2"}]
    script = bench.build_script(spec, sides)
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    parts = bench.split_log(out.stdout)
    order = [(p["variant"], p["phase"], p["i"]) for p in parts]
    assert order == [("a", "setup", 0), ("b", "setup", 0), ("a", "warmup", 0), ("b", "warmup", 0), ("a", "repeat", 0),
                     ("b", "repeat", 0), ("b", "repeat", 1), ("a", "repeat", 1), ("a", "repeat", 2), ("b", "repeat", 2)]
    assert "x=it's" in parts[4]["out"]
    samples, summary, problems = bench.results(spec, parts, "b")
    assert problems == [] and [s["values"]["v"] for s in samples] == [12, 12, 12] and summary["j"]["median"] == 24
    assert summary["wall_s"]["n"] == 3 and summary["wall_s"]["median"] >= 0
    # the setup is skipped the second time (same key), done again when the key changes
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=tmp_path)
    assert [p.get("skipped") for p in bench.split_log(out.stdout)][:2] == [True, True]
    assert (a / "ran.txt").read_text() == "setup\n"
    # a failing command stops the run; its exit code is in the log
    spec2 = bench.parse_spec({**SPEC, "command": "exit 7"})
    out = subprocess.run(["sh", "-c", bench.build_script(spec2, sides[:1])], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 7 and bench.split_log(out.stdout)[-1]["rc"] == 7


def test_compare_verdicts():
    def run(med, sd, better="lower"):
        return {"summary": {"m": {"n": 3, "median": med, "stdev": sd, "better": better}}}

    assert bench.compare(run(100, 0), run(110, 0), 5)[0]["verdict"] == "regression"
    assert bench.compare(run(100, 0), run(90, 0), 5)[0]["verdict"] == "improvement"
    assert bench.compare(run(100, 0), run(103, 0), 5)[0]["verdict"] == "same"
    assert bench.compare(run(100, 10), run(110, 0), 5)[0]["verdict"] == "same"  # within 2 stdev
    assert bench.compare(run(100, 0, "higher"), run(110, 0, "higher"), 5)[0]["verdict"] == "improvement"
    c = bench.compare(run(100, 0), run(110, 0), 5)[0]
    assert round(c["change_pct"], 6) == 10 and c["limit_pct"] == 5


# ---------------------------------------------------------------------------------------------- the API
async def wait_bench(api, bench_id: str, timeout: float = 30) -> dict:
    for _ in range(int(timeout / 0.1)):
        r = (await api.get(f"/api/v1/bench/runs/{bench_id}")).json()
        if r["status"] != "running":
            return r
        await asyncio.sleep(0.1)
    raise AssertionError(r)


def bench_source(tmp_path: Path) -> Path:
    src, _ = make_source(tmp_path)
    (src / "value.txt").write_text('v=10\n{"r": [{"x": 0}, {"x": 5}]}\n')
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "value")
    return src


@pytest.mark.parametrize("host", ["local", "box"])
async def test_runs_definitions_locks_compare_ab(running_app, rsettings, tmp_path, fake_env, host):  # noqa: F811
    h = await running_app(rsettings)
    api = h.api
    src = bench_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id, host=host)
    wt = Path(d["worktree_path"])
    dev = d["id"]
    # a definition kept in the app for this workspace, and one in the repository file that wins by name
    r = await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev, "spec": SPEC})
    assert r.status_code == 201, r.text
    app_def = r.json()
    assert (await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev, "spec": SPEC})).status_code == 409
    assert (await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev,
                                                       "spec": {"name": "x"}})).json()["error"]["code"] == "INVALID_BENCH"
    r = await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev,
                                                   "spec": {"name": "slow", "command": "sleep 30", "repeat": 1}})
    assert r.status_code == 201
    (wt / ".waw").mkdir()
    (wt / ".waw" / "bench.toml").write_text('[[bench]]\nname = "b1"\ncommand = "echo v=1"\n[[bench]]\nname = "broken"\n')
    defs = (await api.get(f"/api/v1/dev/workspaces/{dev}/bench/defs")).json()
    assert [(x["name"], x["source"], x["shadowed"]) for x in defs["defs"]] == [("b1", "file", False), ("b1", "workspace", True),
                                                                              ("slow", "workspace", False)]
    assert len(defs["errors"]) == 1 and "broken" in defs["errors"][0]
    (wt / ".waw" / "bench.toml").unlink()
    (wt / ".waw").rmdir()
    # a run: metrics of each repeat, statistics, what it ran on
    r = await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "b1"})
    assert r.status_code == 201, r.text
    first = await wait_bench(api, r.json()["id"])
    assert first["status"] == "passed", first
    assert first["summary"]["v"]["median"] == 10 and first["summary"]["j"]["n"] == 3 and first["summary"]["wall_s"]["n"] == 3
    assert first["machine"] == host and first["commit"] == git(wt, "rev-parse", "HEAD") and not first["dirty"]
    assert [s["values"]["v"] for s in first["samples"]] == [10, 10, 10] and first["source"] == "app"
    # a benchmark holding the machine: no build, no other benchmark meanwhile; cancel ends it
    slow = (await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "slow"})).json()
    r = await api.post(f"/api/v1/dev/workspaces/{dev}/tasks", json={"kind": "custom", "command": "true"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MACHINE_BUSY"
    r = await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "b1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MACHINE_BUSY"
    await asyncio.sleep(1)
    assert (await api.post(f"/api/v1/bench/runs/{slow['id']}/cancel")).status_code == 200
    assert (await wait_bench(api, slow["id"]))["status"] == "cancelled"
    assert (await api.post(f"/api/v1/dev/workspaces/{dev}/tasks", json={"kind": "custom", "command": "true"})).status_code == 201
    await asyncio.sleep(1)
    # a second run with an uncommitted change that makes v worse: compared with the first (the default baseline)
    (wt / "value.txt").write_text('v=20\n{"r": [{"x": 0}, {"x": 5}]}\n')
    second = await wait_bench(api, (await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "b1"})).json()["id"])
    assert second["status"] == "passed" and second["dirty"]
    cmp = (await api.get("/api/v1/bench/compare", params={"b": second["id"]})).json()
    assert cmp["a"]["id"] == first["id"] and cmp["regressions"] == 1
    v = next(m for m in cmp["metrics"] if m["name"] == "v")
    assert v["verdict"] == "regression" and round(v["change_pct"]) == 100
    assert next(m for m in cmp["metrics"] if m["name"] == "j")["verdict"] == "same"
    # A/B: main (in a temporary worktree) against the workspace as it is, alternating
    r = await api.post(f"/api/v1/dev/workspaces/{dev}/bench/ab", json={"name": "b1", "baseline": "main"})
    assert r.status_code == 201, r.text
    ab = r.json()
    a, b = await wait_bench(api, ab["a"]["id"]), await wait_bench(api, ab["b"]["id"])
    assert a["status"] == b["status"] == "passed", (a, b)
    assert a["summary"]["v"]["median"] == 10 and b["summary"]["v"]["median"] == 20 and a["commit"] == git(src, "rev-parse", "main")
    cmp = (await api.get("/api/v1/bench/compare", params={"b": b["id"]})).json()
    assert cmp["a"]["id"] == a["id"] and cmp["regressions"] == 1
    for _ in range(50):
        if not any("bench" in ln for ln in git(src, "worktree", "list").splitlines()):
            break
        await asyncio.sleep(0.1)
    assert not any("bench" in ln for ln in git(src, "worktree", "list").splitlines())
    assert (await api.post(f"/api/v1/dev/workspaces/{dev}/bench/ab", json={"name": "b1", "baseline": "nope"})).status_code == 400
    # history, export
    runs = (await api.get("/api/v1/bench/runs", params={"dev_id": dev, "name": "b1"})).json()["runs"]
    assert len(runs) == 4 and runs[0]["kind"] == "ab"
    csv_text = (await api.get("/api/v1/bench/runs/export", params={"dev_id": dev, "format": "csv"})).text
    assert csv_text.startswith("run,benchmark,machine") and f"{first['id']},b1,{host}" in csv_text
    data = json.loads((await api.get("/api/v1/bench/runs/export", params={"dev_id": dev, "format": "json"})).text)
    assert {x["id"] for x in data} >= {first["id"], second["id"]}
    # explain the regression: a turn of the workspace's agent with both runs
    r = await api.post(f"/api/v1/bench/runs/{second['id']}/explain", json={})
    assert r.status_code == 201, r.text
    r = await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev, "toml": bench.to_toml({**bench.parse_spec(SPEC), "name": "t"})})
    assert r.status_code == 201 and r.json()["spec"]["metrics"][0]["regex"] == SPEC["metrics"][0]["regex"]
    r = await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev, "toml": "[[bench]]\nname = 'u'\n"})
    assert r.status_code == 400 and "command" in r.json()["error"]["message"]
    # edit, delete
    r = await api.put(f"/api/v1/bench/defs/{app_def['id']}", json={"spec": {**SPEC, "repeat": 2}})
    assert r.status_code == 200 and r.json()["spec"]["repeat"] == 2
    assert (await api.delete(f"/api/v1/bench/runs/{ab['a']['id']}")).status_code == 204
    assert len((await api.get("/api/v1/bench/runs", params={"dev_id": dev})).json()["runs"]) == 3
    assert (await api.delete(f"/api/v1/bench/defs/{app_def['id']}")).status_code == 204


async def test_gpu_locks_and_missing_metrics(running_app, rsettings, tmp_path, fake_env):  # noqa: F811
    h = await running_app(rsettings)
    api = h.api
    d = await new_branch_ws(api, bench_source(tmp_path), h.ctx.workspace_id)
    dev = d["id"]
    for name, gpus in (("g0", [0]), ("g01", [0, 1]), ("g1", [1])):
        r = await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev, "spec": {
            "name": name, "command": "sleep 3", "repeat": 1, "exclusive": False, "gpus": gpus}})
        assert r.status_code == 201
    await api.post("/api/v1/bench/defs", json={"scope": "workspace", "scope_key": dev, "spec": {
        "name": "nometric", "command": "echo nothing", "repeat": 2, "metrics": [{"name": "m", "regex": "m=(\\d+)"}]}})
    g0 = (await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "g0"})).json()
    assert (await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "g01"})).status_code == 409
    g1 = await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "g1"})
    assert g1.status_code == 201  # another GPU: side by side
    assert (await api.post(f"/api/v1/dev/workspaces/{dev}/tasks", json={"kind": "custom", "command": "true"})).status_code == 201
    assert (await wait_bench(api, g0["id"]))["status"] == "passed"
    await wait_bench(api, g1.json()["id"])
    r = await wait_bench(api, (await api.post(f"/api/v1/dev/workspaces/{dev}/bench/run", json={"name": "nometric"})).json()["id"])
    assert r["status"] == "failed" and "m not found" in r["error"]

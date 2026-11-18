"""Benchmarks API (machines.bench): definitions of a development workspace, runs and A/B runs, history, comparison,
export, and asking the agent to explain a regression."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..deps import AppContext, require_auth, require_auth_mutating
from ..errors import bad_request
from .bench import Bench

router = APIRouter()


def _b(ctx: AppContext) -> Bench:
    return ctx.extra["bench"]


class DefIn(BaseModel):
    scope: str | None = Field(None, pattern="^(repo|workspace)$")
    scope_key: str | None = Field(None, max_length=300)
    spec: dict[str, Any] | None = None
    toml: str | None = Field(None, max_length=100_000)  # instead of spec: one [[bench]] table


class RunIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    label: str | None = Field(None, max_length=200)


class AbIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    baseline: str = Field(min_length=1, max_length=256)
    candidate: str | None = Field(None, max_length=256)  # default: the workspace as it is


class ExplainIn(BaseModel):
    baseline: str | None = Field(None, max_length=36)


@router.get("/dev/workspaces/{dev_id}/bench/defs")
async def defs(dev_id: str, ctx: AppContext = Depends(require_auth)):
    return await _b(ctx).definitions(dev_id)


@router.post("/bench/defs", status_code=201)
def create_def(body: DefIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _b(ctx).save_def(body.model_dump())


@router.put("/bench/defs/{def_id}")
def update_def(def_id: str, body: DefIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _b(ctx).save_def(body.model_dump(), def_id)


@router.delete("/bench/defs/{def_id}", status_code=204)
def delete_def(def_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    _b(ctx).delete_def(def_id)


@router.post("/dev/workspaces/{dev_id}/bench/run", status_code=201)
async def run(dev_id: str, body: RunIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _b(ctx).start(dev_id, body.name, body.label)


@router.post("/dev/workspaces/{dev_id}/bench/ab", status_code=201)
async def ab(dev_id: str, body: AbIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _b(ctx).start_ab(dev_id, body.name, body.baseline.strip(), (body.candidate or "").strip() or None)


@router.get("/bench/runs")
def runs(dev_id: str | None = None, machine: str | None = None, name: str | None = None, repo: str | None = None,
         limit: int = Query(200, ge=1, le=1000), ctx: AppContext = Depends(require_auth)):
    return {"runs": _b(ctx).list(dev_id=dev_id, machine=machine, name=name, repo=repo, limit=limit)}


@router.get("/bench/runs/export")
def export(format: str = Query("csv", pattern="^(csv|json)$"), dev_id: str | None = None, machine: str | None = None,
           name: str | None = None, repo: str | None = None, ctx: AppContext = Depends(require_auth)):
    b = _b(ctx)
    data = b.export(b.list(dev_id=dev_id, machine=machine, name=name, repo=repo, limit=1000), format)
    return Response(content=data, media_type="text/csv" if format == "csv" else "application/json",
                    headers={"Content-Disposition": f'attachment; filename="benchmarks.{format}"'})


@router.get("/bench/runs/{bench_id}")
def get_run(bench_id: str, ctx: AppContext = Depends(require_auth)):
    return _b(ctx).get(bench_id)


@router.delete("/bench/runs/{bench_id}", status_code=204)
def delete_run(bench_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    _b(ctx).delete(bench_id)


@router.post("/bench/runs/{bench_id}/cancel")
async def cancel(bench_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    r = _b(ctx).get(bench_id)
    if r["status"] != "running" or not r["task_run_id"]:
        raise bad_request("the run is not running", "BENCH_NOT_RUNNING")
    await ctx.extra["dev_tasks"].cancel(r["task_run_id"])
    return _b(ctx).get(bench_id)


@router.get("/bench/compare")
def compare(b: str, a: str | None = None, ctx: AppContext = Depends(require_auth)):
    """b against the baseline a (default: its A/B baseline, or the latest run of the base branch on the same machine)."""
    return _b(ctx).compare(a, b)


@router.post("/bench/runs/{bench_id}/explain", status_code=201)
async def explain(bench_id: str, body: ExplainIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Ask the workspace's agent why the run differs from its baseline: both runs' metrics and output tails go with it."""
    from ..development.routes import AskIn, ask
    from ..sessions import on_machine

    bench = _b(ctx)
    cmp = bench.compare(body.baseline, bench_id)
    if cmp["a"] is None:
        raise bad_request("there is no baseline to compare with; pick one", "NO_BASELINE")
    b = cmp["b"]
    tasks = ctx.extra["dev_tasks"]
    logs = {}
    for side in ("a", "b"):
        tid = cmp[side].get("task_run_id")
        if tid:
            try:
                logs[side] = tasks.tail(tid, 12_000)
            except Exception:  # noqa: BLE001 - the log is gone
                pass
    regressed = [m["name"] for m in cmp["metrics"] if m["verdict"] == "regression"]
    text = (f"The benchmark {b['name']} " + (f"regressed in {', '.join(regressed)}" if regressed else "changed")
            + " against its baseline. Find the likely cause in the changes between the two commits and explain it.")
    with on_machine(b["machine"] != "local"):
        return await ask(ctx, b["dev_id"], AskIn(text=text, kind="perf", context={
            "bench_report": bench.report(cmp), "bench_base_commit": cmp["a"].get("commit"), "bench_commit": b.get("commit"),
            "bench_logs": logs}))

"""OpenCode v1 and v2 command lines: chosen by `opencode --version` here, by the machine's last probe for a turn
there (v2 dropped `run --dir` and `--variant`; its model is "provider/model#variant"; `debug config` lists the
config documents, the model possibly an object)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FAKES, anchor_for

from backend.app.agents.base import TurnContext
from backend.app.agents.opencode import OpenCodeCliAdapter, _default_from_config, is_v2, opencode_major
from backend.app.config import ProviderConfig
from backend.app.development.targets import RemoteExec


def test_version_parsing():
    assert opencode_major("opencode v2.3.2") == 2 and opencode_major("1.18.3") == 1 and opencode_major("0.0.0-fake") == 0
    assert opencode_major(None) is None and opencode_major("garbage") is None
    assert is_v2("opencode v2.3.2") and is_v2("10.0.1") and not is_v2("1.18.3") and not is_v2("0.9.1")
    assert is_v2(None) and is_v2("")  # unknown: what Set up installs


def test_default_model_of_both_config_formats():
    v1 = {"model": "a/b", "variant": "low", "agent": {"rev": {"model": "c/d"}}}
    assert _default_from_config(v1, None) == {"model": "a/b", "variant": "low"}
    assert _default_from_config(v1, "rev") == {"model": "c/d", "variant": None}
    v2 = [{"type": "document", "path": "/g/opencode.json", "info": {"model": {"providerID": "p", "model": "m.1"}}},
          {"type": "document", "path": "/g/opencode.jsonc", "info": {}},
          {"type": "document", "path": "/proj/opencode.json", "info": {"model": "q/n#high",
                                                                       "agents": {"rev": {"model": {"providerID": "r", "model": "s", "variant": "max"}}}}},
          {"type": "directory", "path": "/g"}]
    assert _default_from_config(v2, None) == {"model": "q/n", "variant": "high"}  # the later document wins
    assert _default_from_config(v2[:2], None) == {"model": "p/m.1", "variant": None}
    assert _default_from_config(v2, "rev") == {"model": "r/s", "variant": "max"}
    assert _default_from_config([], None) == {"model": None, "variant": None}
    assert _default_from_config("x", None) == {"model": None, "variant": None}


@pytest.mark.parametrize("version,expect", [
    ("1.18.3", ["--dir", "/w", "--model", "a/b", "--variant", "high"]),
    ("opencode v2.3.2", ["--model", "a/b#high"]),
    (None, ["--model", "a/b#high"]),  # never probed
])
async def test_a_turn_on_a_machine_follows_its_probed_version(version, expect):
    ad = OpenCodeCliAdapter(ProviderConfig(bin=str(FAKES / "fake-opencode")))
    rex = RemoteExec(["ssh", "box"], {"opencode": "opencode"}, "/w", versions={"opencode": version} if version else {})
    h = await ad.resume_turn(TurnContext(prompt="hi", workspace=Path("/w"), native_session_id=None, cwd=Path("/w"),
                                         model="a/b", variant="high", remote=rex))
    script = h.args[-1]
    for flag in expect:
        assert flag in script, script
    assert ("--dir" in script) == (expect[0] == "--dir") and ("--variant" in script) == ("--variant" in expect)


@pytest.mark.parametrize("version", ["1.18.3", "opencode v2.3.2"])
async def test_turns_here_follow_the_installed_version(running_app, settings, fake_env: Path, version):
    (fake_env / "opencode_version").write_text(version)
    api = (await running_app(settings)).api
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "one"})
    assert (await api.wait_job(r.json()["job_id"]))[-1]["type"] == "completed"
    # only an effort chosen: v2 needs the default model to carry it ("provider/model#variant"), v1 has --variant
    sid, tid = r.json()["session_id"], r.json()["thread_id"]
    await api.put(f"/api/v1/agent-sessions/{sid}/model", json={"model": "", "variant": "high"})
    r = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "two"})
    assert (await api.wait_job(r.json()["job_id"]))[-1]["type"] == "completed"
    run = [c for c in map(json.loads, (fake_env / "opencode_calls.log").read_text().splitlines()) if c[:1] == ["run"]][-1]
    if version.startswith("1."):
        assert "--dir" in run and run[run.index("--variant") + 1] == "high" and "--model" not in run, run
    else:
        assert "--dir" not in run and "--variant" not in run and run[run.index("--model") + 1] == "fake/alpha#high", run
    assert (fake_env / "opencode_last_variant").read_text() == "high"
    # the default model is reported from either config format
    assert (await api.get("/api/v1/providers/opencode/models")).json()["default_model"] == "fake/alpha"


def test_monitor_send_follows_the_version(monkeypatch):
    from backend.app import monitor

    monkeypatch.setattr(monitor, "_bin", lambda ctx, provider: "opencode")
    v1, _ = monitor._send_argv(None, "opencode", "s1", "hi", "/p", "1.18.3")
    v2, _ = monitor._send_argv(None, "opencode", "s1", "hi", "/p", "opencode v2.3.2")
    assert v1[v1.index("--dir") + 1] == "/p" and "--dir" not in v2 and v2[-2:] == ["--", "hi"]

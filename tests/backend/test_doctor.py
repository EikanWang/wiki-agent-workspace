"""`doctor`: every check reports ok / warn / fail with a fix; problems make the command exit 1."""

from __future__ import annotations

import json
import os

from conftest import make_settings

from backend.app import cli
from backend.app.config import write_settings
from backend.app.doctor import report, run_checks


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_good_installation_and_what_breaks_it(tmp_path, workspace):
    s = make_settings(tmp_path, workspace)
    s.state_dir.mkdir(parents=True)
    write_settings(s)
    os.chmod(s.config_path, 0o600)
    c = by_name(run_checks(s, quick=True))
    assert c["Configuration"].level == "ok" and c["Access token"].level == "ok"
    assert c["Wiki folder (primary)"].level == "ok" and c["State directory"].level == "ok"
    assert c["OpenCode"].level == "ok" and c["Default agent"].level == "ok"
    assert all(x.fix == "" for x in c.values() if x.level == "ok")  # a fix only where something is wrong
    # what breaks it: a missing wiki folder, a readable config, an unset default agent, a missing env script
    s.workspace_root = tmp_path / "gone"
    s.workspaces = [{"root": str(tmp_path / "also-gone"), "name": "Notes"}]
    os.chmod(s.config_path, 0o644)
    s.default_provider = None
    s.agent_env_script = str(tmp_path / "nope.sh")
    c = by_name(run_checks(s, quick=True))
    assert c["Wiki folder (primary)"].level == "fail" and "does not exist" in c["Wiki folder (primary)"].message
    assert c["Wiki folder (Notes)"].level == "fail"
    assert c["Configuration"].level == "warn" and "chmod 600" in c["Configuration"].fix
    assert c["Default agent"].level == "warn" and c["Agent env script"].level == "fail"
    text = report(list(c.values()), color=False)
    assert "✗ Wiki folder (primary)" in text and "→ chmod 600" in text and "problem(s)" in text


def test_github_sign_in_from_the_one_click_setup(tmp_path, workspace):
    s = make_settings(tmp_path, workspace)
    s.state_dir.mkdir(parents=True)
    c = by_name(run_checks(s, quick=True))
    assert c["Continue with GitHub (sign-in)"].level == "warn" and "Set up" in c["Continue with GitHub (sign-in)"].fix
    (s.state_dir / "github-signin.json").write_text(json.dumps({"client_id": "x", "client_secret": "y", "allowed_users": ["me"]}))
    c = by_name(run_checks(s, quick=True))
    assert c["Continue with GitHub (sign-in)"].level == "ok" and "me" in c["Continue with GitHub (sign-in)"].message


def test_cli_exit_code_and_json(tmp_path, workspace, capsys, monkeypatch):
    s = make_settings(tmp_path, workspace)
    s.state_dir.mkdir(parents=True)
    write_settings(s)
    os.chmod(s.config_path, 0o600)
    monkeypatch.delenv("WAW_CONFIG", raising=False)
    rc = cli.main(["--config", str(s.config_path), "doctor", "--quick", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert {r["level"] for r in rows} <= {"ok", "warn", "fail"} and any(r["name"] == "Python" for r in rows)
    assert rc == (1 if any(r["level"] == "fail" for r in rows) else 0)
    s.workspace_root = tmp_path / "gone"
    write_settings(s)
    assert cli.main(["--config", str(s.config_path), "doctor", "--quick"]) == 1

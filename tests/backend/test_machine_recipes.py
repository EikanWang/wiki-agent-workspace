"""Machine setup recipes (Phase 4): the recipe library (create, edit, import from *.sh), recipes assigned to a machine
in order, "Set up machine" running them (check skips, the first failure stops, outdated after an edit, force), what
their env snippets set becoming <work_dir>/env.sh for every later command; browsing folders on a machine; cloning a
repository there for a workspace. The fake ssh runs commands on this machine (tests/fakes/fake-ssh)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import FAKES, make_settings
from test_development import git, make_source, new_branch_ws, wait_ready, wait_run
from test_machine_setup import wait_job

from backend.app.config import DevHost, DevSettings
from backend.app.development import targets
from backend.app.development.service import clone_name
from backend.app.machines import envfwd, recipes

PROXY_NAMES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    for n in PROXY_NAMES:
        monkeypatch.delenv(n, raising=False)
    targets._HOMES.clear()
    envfwd._CA_SENT.clear()
    yield
    targets._HOMES.clear()


@pytest.fixture
def rsettings(tmp_path, workspace):
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(code_server=str(FAKES / "fake-code-server"), ssh=str(FAKES / "fake-ssh"),
                                worktrees_dir=tmp_path / "worktrees",
                                hosts=[DevHost(name="box", ssh="me@box", work_dir=str(tmp_path / "remote"),
                                               code_server=str(FAKES / "fake-code-server"), opencode=str(FAKES / "fake-opencode"),
                                               codex=str(FAKES / "fake-codex"))])
    return s


# ---------------------------------------------------------------------------------------------- units
def test_diff_env_and_names():
    before = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/a", "GONE": "x", "SAME": "1", "PWD": "/x", "MANPATH": "/m"}
    after = {"PATH": "/opt/x/bin:/usr/bin", "LD_LIBRARY_PATH": "/a:/b", "SAME": "1", "NEW": "it's", "PWD": "/y",
             "MANPATH": "/other", "SHLVL": "3"}
    text = recipes.diff_env(before, after)
    assert text.splitlines() == ["unset GONE", "export LD_LIBRARY_PATH=\"${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}\"/b",
                                 "export MANPATH=/other", "export NEW='it'\"'\"'s'",
                                 "export PATH=/opt/x/bin\"${PATH:+:$PATH}\""]
    assert recipes.env_names(text) == ["GONE", "LD_LIBRARY_PATH", "MANPATH", "NEW", "PATH"]
    out = subprocess.run(["sh", "-c", text + 'printf "%s|%s|%s" "$PATH" "$LD_LIBRARY_PATH" "$NEW"'], capture_output=True,
                         text=True, env={"PATH": "/usr/local/bin:/bin"}).stdout
    assert out == "/opt/x/bin:/usr/local/bin:/bin|/b|it's"  # applied to whatever PATH is then
    assert recipes.diff_env({"A": "1"}, {"A": "1"}) == ""


def test_parse_file_and_version(tmp_path):
    f = tmp_path / "torch-xpu.sh"
    f.write_text("#!/bin/bash\n# name: conda env torch-xpu\n# check: conda env list | grep -q torch-xpu\n"
                 "# env: . ~/miniforge3/etc/profile.d/conda.sh\n# env: conda activate torch-xpu\n# a comment\n"
                 "conda create -y -n torch-xpu python=3.12\n# name: not a header any more\n")
    r = recipes.parse_file(f)
    assert r["name"] == "conda env torch-xpu" and r["check"] == "conda env list | grep -q torch-xpu"
    assert r["env_out"] == ". ~/miniforge3/etc/profile.d/conda.sh\nconda activate torch-xpu"
    assert r["run"] == f.read_text()
    g = tmp_path / "plain.sh"
    g.write_text("echo hi\n")
    assert recipes.parse_file(g) == {"name": "plain", "run": "echo hi\n", "check": None, "env_out": None}
    v = recipes.version_of("a", None, None)
    assert v == recipes.version_of("a", "", "") and len(v) == 12
    assert v != recipes.version_of("a", "b", None) and recipes.version_of("ab", None, None) != recipes.version_of("a", "b", None)


# ---------------------------------------------------------------------------------------------- the library
async def test_recipe_library_import_and_assign(running_app, tmp_path, workspace, fake_env):
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(ssh=str(FAKES / "fake-ssh"))
    api = (await running_app(s)).api
    await api.post("/api/v1/machines", json={"name": "m1", "ssh": "m1.internal", "work_dir": str(tmp_path / "w1")})
    await api.post("/api/v1/machines", json={"name": "b", "kind": "bastion", "ssh": "b.example"})
    r = await api.post("/api/v1/recipes", json={"name": "hello", "run": "echo hi\n"})
    assert r.status_code == 201, r.text
    one = r.json()
    assert one["version"] == recipes.version_of("echo hi\n", None, None) and one["machines"] == []
    r = await api.post("/api/v1/recipes", json={"name": "hello", "run": "echo again"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECIPE_EXISTS"
    r = await api.post("/api/v1/recipes", json={"name": "bad;name", "run": "x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_RECIPE"
    r = await api.post("/api/v1/recipes", json={"name": "blank", "run": "   "})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_RECIPE"
    # import a folder: new files added, changed ones updated, the same ones left alone
    folder = tmp_path / "wiki-recipes"
    folder.mkdir()
    (folder / "a.sh").write_text("# name: alpha\necho a\n")
    (folder / "b.sh").write_text("echo b\n")
    (folder / "notes.txt").write_text("not a recipe")
    r = await api.post("/api/v1/recipes/import", json={"folder": str(folder)})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == ["alpha", "b"] and not r.json()["errors"]
    (folder / "b.sh").write_text("echo b2\n")
    r = (await api.post("/api/v1/recipes/import", json={"folder": str(folder)})).json()
    assert r["updated"] == ["b"] and r["unchanged"] == ["alpha"] and r["added"] == []
    lib = {x["name"]: x for x in (await api.get("/api/v1/recipes")).json()["recipes"]}
    assert sorted(lib) == ["alpha", "b", "hello"] and lib["b"]["run"] == "echo b2\n" and lib["b"]["source_path"] == str(folder / "b.sh")
    assert (await api.post("/api/v1/recipes/import", json={"folder": "relative"})).json()["error"]["code"] == "INVALID_PATH"
    # assign in order; the machine's env.sh is written
    ids = [lib["b"]["id"], lib["hello"]["id"]]
    r = await api.put("/api/v1/machines/m1/recipes", json={"recipes": ids})
    assert r.status_code == 200, r.text
    assert [x["name"] for x in r.json()["recipes"]] == ["b", "hello"] and r.json()["warning"] is None
    assert all(x["status"] == "not_run" for x in r.json()["recipes"])
    assert (tmp_path / "w1" / "env.sh").exists()
    r = await api.put("/api/v1/machines/m1/recipes", json={"recipes": list(reversed(ids))})
    assert [x["name"] for x in r.json()["recipes"]] == ["hello", "b"]
    assert (await api.put("/api/v1/machines/m1/recipes", json={"recipes": [ids[0], ids[0]]})).json()["error"]["code"] == "INVALID_RECIPES"
    assert (await api.put("/api/v1/machines/m1/recipes", json={"recipes": ["nope"]})).json()["error"]["code"] == "RECIPE_NOT_FOUND"
    assert (await api.put("/api/v1/machines/b/recipes", json={"recipes": ids})).json()["error"]["code"] == "MACHINE_IS_BASTION"
    lib = {x["name"]: x for x in (await api.get("/api/v1/recipes")).json()["recipes"]}
    assert lib["hello"]["machines"] == ["m1"]
    r = await api.delete(f"/api/v1/recipes/{lib['hello']['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECIPE_IN_USE"
    assert (await api.delete(f"/api/v1/recipes/{lib['alpha']['id']}")).status_code == 200
    # deleting the machine forgets its assignments
    assert (await api.delete("/api/v1/machines/m1")).status_code == 200
    assert (await api.delete(f"/api/v1/recipes/{lib['hello']['id']}")).status_code == 200


# ---------------------------------------------------------------------------------------------- running them
async def test_setup_runs_recipes_and_env_reaches_later_commands(running_app, rsettings, tmp_path, fake_env):
    h = await running_app(rsettings)
    api = h.api
    remote = tmp_path / "remote"
    marker = tmp_path / "ran.txt"
    opt = tmp_path / "opt-tool"
    (opt / "bin").mkdir(parents=True)
    (opt / "bin" / "mytool").write_text("#!/bin/sh\necho mytool-ok\n")
    (opt / "bin" / "mytool").chmod(0o755)
    (opt / "setvars.sh").write_text(f'export PATH="{opt}/bin:$PATH"\nexport MY_ROOT={opt}\n')

    async def mk(name, run, check=None, env_out=None):
        r = await api.post("/api/v1/recipes", json={"name": name, "run": run, "check": check, "env_out": env_out})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    a = await mk("tool", f'echo "tool $WAW_WORK_DIR $(pwd)" >> {marker}\n', env_out=f". {opt}/setvars.sh")
    b = await mk("checked", f"echo checked-ran >> {marker}\n", check="true")
    c = await mk("uses tool", f"#!/bin/sh\nmytool >> {marker}\necho MY_ROOT=$MY_ROOT >> {marker}\n")
    r = await api.put("/api/v1/machines/box/recipes", json={"recipes": [a, b, c]})
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/machines/box/setup", json={"bootstrap": None})
    assert r.status_code == 202, r.text
    assert r.json()["title"] == "Recipes on box" and r.json()["kind"] == "setup"
    log = await wait_job(api, r.json()["id"])
    assert log["job"]["status"] == "passed", log["text"]
    lines = marker.read_text().splitlines()
    # $HOME is the cwd, WAW_WORK_DIR set; the check skipped "checked"; the later recipe saw the earlier one's env
    assert lines == [f"tool {remote} {Path.home()}", "mytool-ok", f"MY_ROOT={opt}"], lines
    assert log["job"]["result"]["recipes"]["ok"] == ["tool", "checked", "uses tool"]
    assert "the check passed: already done" in log["text"] and "sets MY_ROOT, PATH" in log["text"]
    st = {x["name"]: x for x in (await api.get("/api/v1/machines/box/recipes")).json()["recipes"]}
    assert all(x["status"] == "ok" for x in st.values()) and st["tool"]["env_vars"] == ["MY_ROOT", "PATH"]
    env_sh = (remote / "env.sh").read_text()
    assert f"export MY_ROOT={opt}" in env_sh and f'export PATH={opt}/bin"${{PATH:+:$PATH}}"' in env_sh
    # a later task in a workspace on the machine sees it
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id, host="box")
    run = (await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks",
                          json={"kind": "custom", "command": 'mytool; echo "root=$MY_ROOT"'})).json()
    out = await wait_run(api, run["id"])
    assert out["run"]["status"] == "passed" and "mytool-ok" in out["text"] and f"root={opt}" in out["text"], out["text"]
    # again: everything ok is skipped; an edited recipe is outdated and runs again
    marker.write_text("")
    log = await wait_job(api, (await api.post("/api/v1/machines/box/setup", json={"bootstrap": None})).json()["id"])
    assert log["job"]["result"]["recipes"]["skipped"] == ["tool", "checked", "uses tool"] and marker.read_text() == ""
    await api.put(f"/api/v1/recipes/{c}", json={"name": "uses tool", "run": f"echo edited >> {marker}\n"})
    st = {x["name"]: x for x in (await api.get("/api/v1/machines/box/recipes")).json()["recipes"]}
    assert st["uses tool"]["status"] == "outdated" and st["tool"]["status"] == "ok"
    log = await wait_job(api, (await api.post("/api/v1/machines/box/setup", json={"bootstrap": None})).json()["id"])
    assert log["job"]["result"]["recipes"]["ok"] == ["uses tool"] and marker.read_text() == "edited\n"
    # one recipe, forced
    r = await api.post("/api/v1/machines/box/setup", json={"bootstrap": None, "recipes": [b], "force": True})
    assert r.json()["title"] == "Recipe checked on box"
    log = await wait_job(api, r.json()["id"])
    assert log["job"]["result"]["recipes"]["ok"] == ["checked"]
    assert marker.read_text() == "edited\n"  # its check still says done
    r = await api.post("/api/v1/machines/box/setup", json={"bootstrap": None, "recipes": ["nope"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_RECIPES"


async def test_setup_stops_at_the_first_failure(running_app, rsettings, tmp_path, fake_env):
    api = (await running_app(rsettings)).api
    remote = tmp_path / "remote"
    marker = tmp_path / "ran.txt"
    ids = []
    for name, run, env_out in (("one", f"echo one >> {marker}", "export ONE=1"), ("boom", "echo failing; exit 3", None),
                               ("never", f"echo never >> {marker}", None)):
        ids.append((await api.post("/api/v1/recipes", json={"name": name, "run": run, "env_out": env_out})).json()["id"])
    await api.put("/api/v1/machines/box/recipes", json={"recipes": ids})
    log = await wait_job(api, (await api.post("/api/v1/machines/box/setup", json={"bootstrap": None})).json()["id"])
    assert log["job"]["status"] == "failed" and "recipe boom failed: exit 3" in log["job"]["error"], log
    assert "failing" in log["text"] and marker.read_text() == "one\n"
    st = {x["name"]: x for x in (await api.get("/api/v1/machines/box/recipes")).json()["recipes"]}
    assert (st["one"]["status"], st["boom"]["status"], st["never"]["status"]) == ("ok", "failed", "not_run")
    assert st["boom"]["error"] == "exit 3" and st["boom"]["last_job_id"] == log["job"]["id"]
    assert "export ONE=1" in (remote / "env.sh").read_text()  # what succeeded still counts
    # an env snippet that fails is a failure too
    bad = (await api.post("/api/v1/recipes", json={"name": "bad env", "run": "true", "env_out": "false"})).json()["id"]
    await api.put("/api/v1/machines/box/recipes", json={"recipes": [bad]})
    log = await wait_job(api, (await api.post("/api/v1/machines/box/setup", json={"bootstrap": None})).json()["id"])
    assert log["job"]["status"] == "failed" and "environment snippet failed" in log["job"]["error"], log
    assert "ONE" not in (remote / "env.sh").read_text()  # no longer assigned


# ---------------------------------------------------------------------------------------------- browsing a machine
async def test_machine_folder_listing(running_app, rsettings, tmp_path, fake_env):
    api = (await running_app(rsettings)).api
    base = tmp_path / "tree"
    (base / "repo" / ".git").mkdir(parents=True)
    (base / "Plain").mkdir()
    (base / ".hidden").mkdir()
    (base / "file.txt").write_text("x")
    await api.post("/api/v1/machines", json={"name": "b", "kind": "bastion", "ssh": "b.example"})
    r = await api.get("/api/v1/machines/box/fs/dirs", params={"path": str(base)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["path"] == str(base.resolve()) and j["parent"] == str(base.resolve().parent) and j["is_git"] is False
    assert [(e["name"], e["is_git"]) for e in j["entries"]] == [("Plain", False), ("repo", True)]
    assert j["entries"][1]["path"] == f"{base.resolve()}/repo"
    j = (await api.get("/api/v1/machines/box/fs/dirs", params={"path": str(base), "hidden": "true"})).json()
    assert [e["name"] for e in j["entries"]] == [".hidden", "Plain", "repo"]
    assert (await api.get("/api/v1/machines/box/fs/dirs", params={"path": str(base / "repo")})).json()["is_git"] is True
    j = (await api.get("/api/v1/machines/box/fs/dirs", params={"path": "~"})).json()
    assert j["path"] == str(Path.home().resolve())
    assert (await api.get("/api/v1/machines/box/fs/dirs", params={"path": "/"})).json()["parent"] is None
    for path, code in ((str(base / "missing"), "FOLDER_NOT_FOUND"), (str(base / "file.txt"), "NOT_A_DIRECTORY"),
                       ("relative", "INVALID_PATH")):
        assert (await api.get("/api/v1/machines/box/fs/dirs", params={"path": path})).json()["error"]["code"] == code, path
    assert (await api.get("/api/v1/machines/b/fs/dirs")).json()["error"]["code"] == "MACHINE_IS_BASTION"
    local = (await api.get("/api/v1/machines/local/fs/dirs", params={"path": str(base)})).json()
    assert [e["name"] for e in local["entries"]] == ["Plain", "repo"]


# ---------------------------------------------------------------------------------------------- cloning
@pytest.mark.parametrize("host", ["local", "box"])
async def test_workspace_from_a_clone(running_app, rsettings, tmp_path, fake_env, host):
    h = await running_app(rsettings)
    api = h.api
    _, bare = make_source(tmp_path)
    url = f"file://{bare}"
    name = clone_name(url)
    assert name == f"{tmp_path.name}-origin"
    d = await new_branch_ws(api, "", h.ctx.workspace_id, host=host, clone_url=url)
    dest = (tmp_path / "state" / "dev" / "src" if host == "local" else tmp_path / "remote" / "src") / name
    assert d["source_repo"] == str(dest.resolve()) if host == "local" else d["source_repo"] == str(dest)
    assert (dest / ".git").is_dir() and git(Path(d["worktree_path"]), "rev-parse", "--abbrev-ref", "HEAD") == "feature/x"
    assert (Path(d["worktree_path"]) / "lib.py").exists()
    # a second workspace of that repository reuses the clone (and fetches what is new)
    other = tmp_path / "pusher"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    git(other, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "new upstream")
    git(other, "push", "-q", "origin", "main")
    r = await api.post("/api/v1/dev/workspaces", json={"host": host, "source_repo": "", "mode": "branch", "branch": "feature/y",
                                                       "clone_url": url, "wiki_workspace_id": h.ctx.workspace_id})
    assert r.status_code == 201, r.text
    d2 = await wait_ready(api, r.json()["id"])
    assert d2["status"] == "ready" and d2["source_repo"] == d["source_repo"], d2
    assert git(dest, "rev-parse", "origin/main") == git(other, "rev-parse", "HEAD")


async def test_clone_errors(running_app, rsettings, tmp_path, fake_env):
    h = await running_app(rsettings)
    api = h.api
    base = {"host": "local", "source_repo": "", "wiki_workspace_id": h.ctx.workspace_id}
    r = await api.post("/api/v1/dev/workspaces", json={**base, "mode": "folder", "clone_url": "https://github.com/a/b.git"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_MODE"
    for bad in ("not a url", "-oProxyCommand=x", "/just/a/path"):
        r = await api.post("/api/v1/dev/workspaces", json={**base, "mode": "branch", "branch": "x", "clone_url": bad})
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_URL", (bad, r.text)
    r = await api.post("/api/v1/dev/workspaces", json={**base, "mode": "branch", "branch": "x",
                                                       "clone_url": f"file://{tmp_path / 'missing.git'}"})
    assert r.status_code == 201, r.text
    d = await wait_ready(api, r.json()["id"])
    assert d["status"] == "failed" and "git clone" in (d.get("error") or ""), d
    assert not (tmp_path / "state" / "dev" / "src" / "missing").exists()

"""Machine setup: what the app needs on a machine, installed without root into its work folder.

1. Probe the machine (machines.probe).
2. System packages the app relies on (tmux, git, tar, curl) with the machine's package manager when sudo works without
   a password (or the login is root); otherwise the log says what an administrator should run.
3. The app's tools — opencode, code-server, gh, codex — from their GitHub releases (or `[development] release_base`, a
   mirror with the same layout): the latest tag, the archive for the machine's CPU (and libc), unpacked into
   <work_dir>/tools/<name>-<version>/ and linked from <work_dir>/bin, which comes first on PATH for everything the app
   runs there. The machine downloads them itself (through the proxy it is given, if any); when it cannot reach the
   internet this server downloads them (into <state_dir>/cache/tools) and streams them over ssh.
4. OpenCode's settings: this server's opencode config without its secrets (API keys, tokens, headers — `{env:NAME}`
   references are kept: forward NAME in the machine's Environment) as <work_dir>/opencode/opencode.json, used by
   agent turns there.
5. Probe again.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import DevHost
from ..development.targets import SshTarget, TargetError, sh_path
from . import envfwd, probe
from .jobs import JobFailed, JobLog

APP_TOOLS = ("opencode", "code-server", "gh", "codex")
DEFAULT_TOOLS = ("opencode", "code-server", "gh")
SYSTEM_PACKAGES = ("tmux", "git", "tar", "curl")
REPOS = {"opencode": "anomalyco/opencode", "code-server": "coder/code-server", "gh": "cli/cli", "codex": "openai/codex"}
DOWNLOAD_TIMEOUT = 900
SECRET_KEY = re.compile(r"(key|token|secret|password|passwd|credential|auth|cookie|header)", re.I)
KEEP_KEYS = ("$schema", "provider", "model", "small_model", "enabled_providers", "disabled_providers")


@dataclass
class Platform:
    arch: str      # x64 | arm64
    musl: bool
    avx2: bool
    root: bool


def version_of(tag: str) -> str:
    t = tag.removeprefix("rust-")
    return t[1:] if t[:1] == "v" else t


def asset(name: str, tag: str, p: Platform) -> tuple[str, str]:
    """(archive name, the executable's path inside it)."""
    v = version_of(tag)
    deb = "amd64" if p.arch == "x64" else "arm64"
    if name == "opencode":
        base = "-baseline" if p.arch == "x64" and not p.avx2 else ""
        return f"opencode-linux-{p.arch}{base}{'-musl' if p.musl else ''}.tar.gz", "opencode"
    if name == "code-server":
        return f"code-server-{v}-linux-{deb}.tar.gz", f"code-server-{v}-linux-{deb}/bin/code-server"
    if name == "gh":
        return f"gh_{v}_linux_{deb}.tar.gz", f"gh_{v}_linux_{deb}/bin/gh"
    if name == "codex":
        triple = ("x86_64" if p.arch == "x64" else "aarch64") + "-unknown-linux-musl"
        return f"codex-{triple}.tar.gz", f"codex-{triple}"
    raise ValueError(name)


# ---------------------------------------------------------------------------------------------- plan
def _managed(caps: dict[str, Any], path: str | None) -> bool:
    w = (caps or {}).get("work_dir") or ""
    return bool(path and w and path.startswith(w.rstrip("/") + "/bin/"))


def plan(h: DevHost, caps: dict[str, Any] | None, release_base: str) -> dict[str, Any]:
    caps = caps or {}
    tools = caps.get("tools") or {}
    out = []
    for n in APP_TOOLS:
        t = tools.get(n) or {}
        out.append({"name": n, "for": probe.TOOLS.get(n, ""), "installed": bool(t.get("ok")), "path": t.get("path"),
                    "version": t.get("version"), "managed": _managed(caps, t.get("path")),
                    "default": n in DEFAULT_TOOLS and not t.get("ok"),
                    "configured": getattr(h, n.replace("-", "_"), n) if n != "gh" else "gh"})
    missing = [p for p in SYSTEM_PACKAGES if tools and not (tools.get(p) or {}).get("ok")]
    pm, sudo = caps.get("package_manager"), caps.get("sudo")
    return {"probed": bool(caps), "tools": out, "release_base": release_base,
            "system_packages": {"missing": missing, "package_manager": pm, "sudo": sudo,
                                "can_install": bool(missing and pm and sudo == "passwordless"),
                                "command": pm_command(pm, missing, root=False) if missing and pm else None},
            "agent_settings": {"source": str(p) if (p := opencode_config_source()) else None}}


def pm_command(pm: str | None, pkgs: list[str], root: bool) -> str | None:
    if not pm or not pkgs:
        return None
    s = "" if root else "sudo -n "
    names = " ".join(shlex.quote(p) for p in pkgs)
    return {
        "apt-get": f"{s}env DEBIAN_FRONTEND=noninteractive apt-get update -q && "
                   f"{s}env DEBIAN_FRONTEND=noninteractive apt-get install -y -q {names}",
        "dnf": f"{s}dnf install -y {names}",
        "yum": f"{s}yum install -y {names}",
        "zypper": f"{s}zypper --non-interactive install {names}",
        "pacman": f"{s}pacman -S --noconfirm --needed {names}",
        "apk": f"{s}apk add {names}",
    }.get(pm)


# ---------------------------------------------------------------------------------------------- opencode settings
def opencode_config_source() -> Path | None:
    files = opencode_config_files()
    return files[-1] if files else None


def opencode_config_files() -> list[Path]:
    """This server's global opencode config files, lowest precedence first."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    out = [p for p in (xdg / "opencode" / n for n in ("config.json", "opencode.json", "opencode.jsonc")) if p.is_file()]
    env = os.environ.get("OPENCODE_CONFIG")
    if env and Path(env).expanduser().is_file():
        out.append(Path(env).expanduser())
    return out


def strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas -> JSON (strings are left alone)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            e = text.find("*/", i + 2)
            i = n if e < 0 else e + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _clean(v: Any, secret: bool = False) -> Any:
    if isinstance(v, str):
        if v.startswith("{env:") and v.endswith("}"):
            return v
        if secret or "{file:" in v:
            return None
        return v
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            c = _clean(x, secret or bool(SECRET_KEY.search(str(k))))
            if c is not None and c != {}:
                out[k] = c
        return out
    if isinstance(v, list):
        return [c for c in (_clean(x, secret) for x in v) if c is not None]
    return None if secret else v


def sanitized_opencode_config(files: list[Path] | None = None) -> tuple[dict[str, Any], list[str]]:
    """(the settings a machine gets, the {env:NAME} variables they refer to)."""
    merged: dict[str, Any] = {}
    for f in files if files is not None else opencode_config_files():
        try:
            d = json.loads(strip_jsonc(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            for k in KEEP_KEYS:
                if k in d:
                    if isinstance(d[k], dict) and isinstance(merged.get(k), dict):
                        merged[k] = {**merged[k], **d[k]}
                    else:
                        merged[k] = d[k]
    clean = _clean(merged) or {}
    refs = sorted(set(re.findall(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(clean))))
    return clean, refs


# ---------------------------------------------------------------------------------------------- remote commands
async def run_logged(t: SshTarget, script: str, args: list[str], jl: JobLog, *, timeout: float = 600,
                     stdin_path: Path | None = None, env: bool = True) -> int:
    """`sh -c script _ args…` on the machine (with its forwarded environment), its output into the log."""
    pre = ""
    if env:
        f = await envfwd.push(t)
        pre = envfwd.source_snippet(f) if f else ""
    argv, _, _ = t.spawn_argv(["sh", "-c", pre + script, "_", *args], None, None)
    fh = stdin_path.open("rb") if stdin_path else None
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=fh or asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    start_new_session=True)
    except OSError as exc:
        if fh:
            fh.close()
        raise JobFailed(f"cannot run ssh: {exc}")

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            jl.write(chunk.decode("utf-8", "replace").replace("\r\n", "\n"))

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        jl.line(f"[timed out after {int(timeout)} s]")
        return 124
    finally:
        if fh:
            fh.close()
    if proc.returncode == 255:
        raise JobFailed(f"lost the connection to {t.name}")
    return proc.returncode or 0


async def capture(t: SshTarget, script: str, args: list[str] | None = None, timeout: float = 60) -> tuple[int, str]:
    rc, out, err = await t.run(["sh", "-c", script, "_", *(args or [])], timeout=timeout)
    return rc, out + err


_PLATFORM = r'''uname -m
if ldd --version 2>&1 | grep -qi musl || ls /lib/ld-musl-* >/dev/null 2>&1; then echo musl; else echo glibc; fi
if grep -q avx2 /proc/cpuinfo 2>/dev/null; then echo avx2; else echo noavx2; fi
if [ "$(id -u)" = 0 ]; then echo root; else echo user; fi
'''


async def platform(t: SshTarget) -> Platform:
    rc, out = await capture(t, _PLATFORM)
    f = (out.split() + ["", "", "", ""])[:4]
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(f[0])
    if rc != 0 or arch is None:
        raise JobFailed(f"unsupported machine: {f[0] or out.strip()[:200]} (the app's tools exist for x86_64 and aarch64 Linux)")
    return Platform(arch=arch, musl=f[1] == "musl", avx2=f[2] == "avx2", root=f[3] == "root")


# ---------------------------------------------------------------------------------------------- releases
_TAG_RE = re.compile(r"/releases/tag/([^/?#\s]+)")


async def latest_tag_local(base: str, repo: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            r = await c.get(f"{base}/{repo}/releases/latest")
    except httpx.HTTPError:
        return None
    m = _TAG_RE.search(r.headers.get("location", "")) if r.is_redirect else None
    return m.group(1) if m else None


_REMOTE_TAG = r'''u=$1
if command -v curl >/dev/null 2>&1; then curl -fsSI -o /dev/null -w '%{redirect_url}' --max-time 30 "$u"
elif command -v wget >/dev/null 2>&1; then wget -S --spider --max-redirect=0 -T 30 "$u" 2>&1 | sed -n 's/^ *[Ll]ocation: *//p' | head -n 1
else exit 20; fi
'''


async def latest_tag_remote(t: SshTarget, base: str, repo: str) -> str | None:
    f = await envfwd.push(t)
    _, out = await capture(t, (envfwd.source_snippet(f) if f else "") + _REMOTE_TAG, [f"{base}/{repo}/releases/latest"])
    m = _TAG_RE.search(out)
    return m.group(1) if m else None


# runs with $w (the work folder) set; $1 name, $2 version, $3 url ("-": the archive on stdin), $4 member
_INSTALL = r'''name=$1; ver=$2; url=$3; member=$4
dest="$w/tools/$name-$ver"; tmp="$w/tools/.dl-$name-$$"
mkdir -p "$w/tools" "$w/bin" && rm -rf "$tmp" && mkdir -p "$tmp/x" || exit 1
if [ "$url" = "-" ]; then cat > "$tmp/a.tgz" || { rm -rf "$tmp"; exit 1; }
else
  echo "downloading $url"
  if command -v curl >/dev/null 2>&1; then curl -fSL --retry 2 --connect-timeout 20 -s -S -o "$tmp/a.tgz" "$url"
  elif command -v wget >/dev/null 2>&1; then wget -q -T 30 -O "$tmp/a.tgz" "$url"
  else echo "neither curl nor wget is installed"; rm -rf "$tmp"; exit 20; fi || { rm -rf "$tmp"; exit 21; }
fi
tar xzf "$tmp/a.tgz" -C "$tmp/x" || { echo "the download is not a valid archive"; rm -rf "$tmp"; exit 22; }
[ -f "$tmp/x/$member" ] || { echo "$member is not in the archive"; rm -rf "$tmp"; exit 22; }
chmod +x "$tmp/x/$member"
rm -rf "$dest" && mv "$tmp/x" "$dest" && rm -rf "$tmp" || exit 1
ln -sfn "$dest/$member" "$w/bin/$name" || exit 1
for d in "$w"/tools/"$name"-*; do [ "$d" = "$dest" ] || rm -rf "$d"; done
echo "installed $w/bin/$name -> $dest/$member"
"$w/bin/$name" --version 2>&1 | head -n 2
'''


async def download_local(cache: Path, url: str, jl: JobLog) -> Path:
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = cache / re.sub(r"[^A-Za-z0-9._-]", "_", url.split("/releases/download/", 1)[-1])
    if dest.is_file() and dest.stat().st_size > 0:
        jl.line(f"using {dest} (downloaded before)")
        return dest
    jl.line(f"this server downloads {url}")
    tmp = dest.with_name(dest.name + ".part")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    raise JobFailed(f"download failed: HTTP {r.status_code} for {url}")
                with tmp.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 20):
                        fh.write(chunk)
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise JobFailed(f"download failed on this server too: {exc}")
    os.replace(tmp, dest)
    return dest


async def install_tool(t: SshTarget, name: str, p: Platform, base: str, cache: Path, jl: JobLog) -> str:
    """Install the latest release of `name`; its tag."""
    repo = REPOS[name]
    tag = await latest_tag_local(base, repo) or await latest_tag_remote(t, base, repo)
    if not tag:
        raise JobFailed(f"cannot find the latest release of {repo} at {base}")
    archive, member = asset(name, tag, p)
    url = f"{base}/{repo}/releases/download/{tag}/{archive}"
    w = f"w={sh_path(t.host.work_dir)}; "
    args = [name, version_of(tag), url, member]
    rc = await run_logged(t, w + _INSTALL, args, jl, timeout=DOWNLOAD_TIMEOUT)
    if rc in (20, 21):
        jl.line(f"{t.name} could not download it; sending it from this server")
        local = await download_local(cache, url, jl)
        rc = await run_logged(t, w + _INSTALL, [name, version_of(tag), "-", member], jl, timeout=DOWNLOAD_TIMEOUT,
                              stdin_path=local)
    if rc != 0:
        raise JobFailed(f"installing {name} {tag} failed (exit {rc})")
    return tag


# ---------------------------------------------------------------------------------------------- the job
async def run(t: SshTarget, h: DevHost, params: dict[str, Any], jl: JobLog, *, release_base: str, cache: Path,
              save_probe) -> dict[str, Any]:
    try:
        return await _run(t, h, params, jl, release_base=release_base, cache=cache, save_probe=save_probe)
    except TargetError as exc:
        raise JobFailed(exc.message)


async def _run(t: SshTarget, h: DevHost, params: dict[str, Any], jl: JobLog, *, release_base: str, cache: Path,
               save_probe) -> dict[str, Any]:
    result: dict[str, Any] = {"installed": {}, "skipped": {}, "failed": {}, "system_packages": None, "agent_settings": None}
    jl.step(f"Checking {t.name}")
    caps = await probe.probe(t, h)
    save_probe(caps)
    if not caps.get("complete"):
        raise JobFailed(f"the probe did not finish: {caps.get('error') or 'no answer'}")
    p = await platform(t)
    jl.line(f"{caps['os'].get('pretty') or caps['os'].get('kernel')} · {p.arch}{' musl' if p.musl else ''}"
            f" · work folder {caps.get('work_dir')} · sudo: {caps.get('sudo')}")

    tools = caps.get("tools") or {}
    missing = [n for n in SYSTEM_PACKAGES if not (tools.get(n) or {}).get("ok")]
    if params.get("system_packages", True) and missing:
        jl.step("System packages: " + ", ".join(missing))
        pm = caps.get("package_manager")
        cmd = pm_command(pm, missing, root=p.root)
        if not cmd:
            jl.line("no known package manager here; install them by hand")
            result["system_packages"] = {"missing": missing, "installed": False}
        elif p.root or caps.get("sudo") == "passwordless":
            jl.line(f"$ {cmd}")
            rc = await run_logged(t, cmd, [], jl, timeout=1200)
            result["system_packages"] = {"missing": missing, "installed": rc == 0, "exit_code": rc}
            if rc != 0:
                jl.line(f"[the package manager failed (exit {rc}); continuing]")
        else:
            jl.line("sudo needs a password here, so the app cannot install them. Ask an administrator to run:")
            jl.line(f"  {pm_command(pm, missing, root=False).replace('sudo -n ', 'sudo ')}")  # type: ignore[union-attr]
            result["system_packages"] = {"missing": missing, "installed": False, "command": pm_command(pm, missing, root=False)}

    wanted = [n for n in (params.get("tools") if params.get("tools") is not None else
                          [n for n in DEFAULT_TOOLS if not (tools.get(n) or {}).get("ok")]) if n in APP_TOOLS]
    for name in wanted:
        cur = tools.get(name) or {}
        jl.step(f"{name}")
        if cur.get("ok") and _managed(caps, cur.get("path")) and not params.get("update"):
            jl.line(f"already installed by setup: {cur.get('path')} {cur.get('version') or ''}".rstrip())
            result["skipped"][name] = cur.get("version")
            continue
        if cur.get("ok") and not _managed(caps, cur.get("path")):
            jl.line(f"{name} is on PATH already ({cur.get('path')}); setup's copy in the work folder comes first")
        try:
            result["installed"][name] = await install_tool(t, name, p, release_base, cache, jl)
        except JobFailed as exc:
            jl.line(f"[{exc.message}]")
            result["failed"][name] = exc.message
        conf = {"opencode": h.opencode, "code-server": h.code_server, "codex": h.codex}.get(name)
        if conf and conf != name:
            jl.line(f"note: this machine is set to run {conf!r} for {name}; set it to {name!r} to use this copy")

    if params.get("agent_settings", True):
        jl.step("OpenCode settings")
        files = opencode_config_files()
        if not files:
            jl.line("this server has no opencode config (~/.config/opencode/opencode.json); nothing to copy")
        else:
            cfg, refs = sanitized_opencode_config(files)
            w = await t.expand(h.work_dir)
            await t.write_text(f"{w}/opencode/opencode.json", json.dumps(cfg, indent=2) + "\n", 0o600)
            jl.line(f"copied the settings of {', '.join(str(f) for f in files)} without keys and tokens "
                    f"to {w}/opencode/opencode.json ({', '.join(sorted(cfg)) or 'empty'})")
            fwd = envfwd.collect(h).env
            for r in refs:
                jl.line(f"  uses {{env:{r}}}: " + ("forwarded" if r in fwd else
                        "not forwarded — turn on forwarding in the machine's Environment (or set it there)"))
            result["agent_settings"] = {"path": f"{w}/opencode/opencode.json", "keys": sorted(cfg), "env_refs": refs}

    jl.step("Checking again")
    caps = await probe.probe(t, h)
    save_probe(caps)
    for n in [*wanted]:
        tl = (caps.get("tools") or {}).get(n) or {}
        jl.line(f"{n}: {tl.get('path') or 'missing'} {tl.get('version') or ''}".rstrip())
    if result["failed"]:
        raise JobFailed("could not install " + ", ".join(result["failed"]))
    return result

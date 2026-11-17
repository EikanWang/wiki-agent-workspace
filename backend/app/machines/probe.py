"""What a machine has: OS, CPU, memory, free disk in its work folder, GPUs (NVIDIA, Intel, AMD), password-less sudo,
the package manager, and the tools the app and typical builds use, with their versions. One POSIX sh script sent on
stdin prints marked sections; they are parsed here, so nothing has to be installed on the machine."""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from ..config import DevHost
from ..development.targets import Target

TIMEOUT = 90

# name -> what it is needed for (shown next to a missing tool)
TOOLS: dict[str, str] = {
    "git": "worktrees, commits (required)",
    "tar": "copying the wiki and skills there (required)",
    "tmux": "terminals and jobs that survive disconnects",
    "gh": "pull requests from the machine",
    "code-server": "the VS Code editor",
    "opencode": "agent turns (OpenCode)",
    "codex": "agent turns (Codex)",
    "python3": "Python builds and tests",
    "conda": "conda environments",
    "cmake": "C/C++ builds",
    "ninja": "C/C++ builds",
    "make": "builds",
    "gcc": "C/C++ builds",
    "clang": "C/C++ builds",
    "curl": "downloads",
    "rsync": "file copies",
}
REQUIRED = ("git", "tar")

SCRIPT = r'''
w="$1"; shift
tmo() { if command -v timeout >/dev/null 2>&1; then timeout 10 "$@"; else "$@"; fi; }
echo "@@uname"; uname -s; uname -r; uname -m; uname -n
echo "@@os"; if [ -r /etc/os-release ]; then . /etc/os-release; echo "${PRETTY_NAME:-$NAME}"; fi
echo "@@nproc"; nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null
echo "@@lscpu"; LC_ALL=C lscpu 2>/dev/null | grep -E '^(Model name|Socket\(s\)|Core\(s\) per socket|Thread\(s\) per core|NUMA node\(s\)):'
echo "@@cpuinfo"; grep -m1 'model name' /proc/cpuinfo 2>/dev/null
echo "@@mem"; grep -E '^MemTotal:' /proc/meminfo 2>/dev/null
echo "@@disk"; d="$w"; while [ -n "$d" ] && [ ! -d "$d" ]; do d=$(dirname "$d"); done; echo "$d"; df -Pk "$d" 2>/dev/null | tail -n 1
echo "@@home"; printf '%s\n' "$HOME"
echo "@@shell"; printf '%s\n' "${SHELL:-}"
echo "@@sudo"; if command -v sudo >/dev/null 2>&1; then if sudo -n true >/dev/null 2>&1; then echo passwordless; else echo password; fi; else echo none; fi
echo "@@pm"; for p in apt-get dnf yum zypper pacman apk; do if command -v $p >/dev/null 2>&1; then echo $p; break; fi; done
echo "@@gnufind"; find --version 2>/dev/null | head -n 1
for spec in "$@"; do
  n="${spec%%=*}"; b="${spec#*=}"
  case "$b" in "~/"*) b="$HOME/${b#"~/"}";; esac
  p=$(command -v "$b" 2>/dev/null)
  echo "@@tool $n"
  if [ -n "$p" ]; then
    echo "$p"
    case "$n" in tmux) tmo "$p" -V 2>&1 </dev/null | head -n 1;; *) tmo "$p" --version 2>&1 </dev/null | head -n 1;; esac
  fi
done
echo "@@nvidia"; if command -v nvidia-smi >/dev/null 2>&1; then tmo nvidia-smi --query-gpu=index,name,uuid,pci.bus_id,driver_version,memory.total --format=csv,noheader 2>/dev/null; fi
echo "@@xpu"; if command -v xpu-smi >/dev/null 2>&1; then tmo xpu-smi discovery -j 2>/dev/null; fi
echo "@@rocm"; if command -v rocm-smi >/dev/null 2>&1; then tmo rocm-smi --showproductname --showbus --json 2>/dev/null; fi
echo "@@lspci"; if command -v lspci >/dev/null 2>&1; then lspci -D -mm 2>/dev/null | grep -E '"(VGA compatible controller|3D controller|Display controller|Processing accelerators)"'; fi
echo "@@drivers"; for m in xe i915 nvidia amdgpu; do if [ -r /sys/module/$m/version ]; then echo "$m $(cat /sys/module/$m/version)"; elif [ -d /sys/module/$m ]; then echo "$m"; fi; done
echo "@@end"
'''


def tool_specs(h: DevHost) -> list[str]:
    bins = {"code-server": h.code_server, "opencode": h.opencode, "codex": h.codex}
    return [f"{name}={bins.get(name, name)}" for name in TOOLS]


def sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line.rstrip())
    return out


def _pci(addr: str) -> str:
    """0000:3b:00.0 for "00000000:3B:00.0" / "3b:00.0"."""
    a = addr.strip().lower()
    m = re.match(r"^(?:([0-9a-f]+):)?([0-9a-f]{2}):([0-9a-f]{2})\.([0-9a-f])$", a)
    if not m:
        return a
    dom = int(m.group(1) or "0", 16)
    return f"{dom:04x}:{m.group(2)}:{m.group(3)}.{m.group(4)}"


def _vendor(text: str) -> str:
    t = text.lower()
    if "nvidia" in t:
        return "nvidia"
    if "intel" in t:
        return "intel"
    if "amd" in t or "advanced micro devices" in t or "ati " in t:
        return "amd"
    return "other"


def parse_gpus(sec: dict[str, list[str]]) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    seen: set[str] = set()
    drivers = {ln.split()[0]: (ln.split()[1] if len(ln.split()) > 1 else None) for ln in sec.get("drivers", []) if ln.strip()}
    for ln in sec.get("nvidia", []):
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        pci = _pci(parts[3])
        seen.add(pci)
        gpus.append({"vendor": "nvidia", "index": int(parts[0]), "name": parts[1], "uuid": parts[2], "pci": pci,
                     "driver": parts[4], "memory": parts[5], "source": "nvidia-smi"})
    xpu = "\n".join(sec.get("xpu", [])).strip()
    if xpu:
        try:
            data = json.loads(xpu)
        except ValueError:
            data = {}
        for i, d in enumerate(data.get("device_list") or []):
            pci = _pci(str(d.get("pci_bdf_address") or ""))
            seen.add(pci)
            gpus.append({"vendor": "intel", "index": int(d.get("device_id", i)), "name": d.get("device_name"),
                         "uuid": d.get("uuid"), "pci": pci, "driver": drivers.get("xe") or drivers.get("i915"),
                         "memory": d.get("memory_physical_size_byte") or d.get("lmem_size_byte"), "source": "xpu-smi"})
    rocm = "\n".join(sec.get("rocm", [])).strip()
    if rocm:
        try:
            data = json.loads(rocm)
        except ValueError:
            data = {}
        for key, d in data.items():
            if not str(key).startswith("card") or not isinstance(d, dict):
                continue
            pci = _pci(str(d.get("PCI Bus") or ""))
            seen.add(pci)
            name = d.get("Card Series") or d.get("Card series") or d.get("Card model") or d.get("Device Name")
            gpus.append({"vendor": "amd", "index": int(re.sub(r"\D", "", key) or 0), "name": name, "uuid": None, "pci": pci,
                         "driver": drivers.get("amdgpu"), "memory": None, "source": "rocm-smi"})
    counts: dict[str, int] = {}
    for ln in sec.get("lspci", []):
        try:
            f = shlex.split(ln)
        except ValueError:
            continue
        if len(f) < 4:
            continue
        pci = _pci(f[0])
        if pci in seen:
            continue
        vendor = _vendor(f[2])
        if vendor == "other":
            continue  # BMC / server graphics (ASPEED, Matrox): not a compute device
        kind = f[1]
        n = counts.get(vendor, 0)
        counts[vendor] = n + 1
        drv = {"intel": drivers.get("xe") or drivers.get("i915"), "nvidia": drivers.get("nvidia"), "amd": drivers.get("amdgpu")}.get(vendor)
        gpus.append({"vendor": vendor, "index": None, "name": f"{f[2]} {f[3]}".strip(), "uuid": None, "pci": pci, "driver": drv,
                     "memory": None, "source": "lspci", "class": kind})
    return gpus


def parse(text: str, h: DevHost) -> dict[str, Any]:
    sec = sections(text)
    un = sec.get("uname", []) + ["", "", "", ""]
    lscpu = {}
    for ln in sec.get("lscpu", []):
        k, _, v = ln.partition(":")
        lscpu[k.strip()] = v.strip()
    model = lscpu.get("Model name") or next((ln.split(":", 1)[1].strip() for ln in sec.get("cpuinfo", []) if ":" in ln), None)

    def _int(v: Any) -> int | None:
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    mem_kb = None
    for ln in sec.get("mem", []):
        m = re.search(r"(\d+)\s*kB", ln)
        if m:
            mem_kb = int(m.group(1))
    disk = {"path": None, "total_kb": None, "free_kb": None}
    d = sec.get("disk", [])
    if d:
        disk["path"] = d[0] or None
        if len(d) > 1:
            f = d[1].split()
            if len(f) >= 4:
                disk["total_kb"], disk["free_kb"] = _int(f[1]), _int(f[3])
    tools: dict[str, Any] = {}
    for name, why in TOOLS.items():
        lines = [ln for ln in sec.get(f"tool {name}", []) if ln.strip()]
        path = lines[0] if lines else None
        version = None
        if len(lines) > 1:
            m = re.search(r"\d+(?:\.\d+)+(?:[-+.\w]*)?", lines[1])
            version = m.group(0) if m else lines[1][:80]
        tools[name] = {"path": path, "version": version, "ok": bool(path), "required": name in REQUIRED, "for": why}
    sudo = (sec.get("sudo") or ["none"])[0].strip() or "none"
    gnu = any("GNU" in ln for ln in sec.get("gnufind", []))
    missing_required = [n for n in REQUIRED if not tools[n]["ok"]] + ([] if gnu else ["GNU find"])
    return {
        "os": {"kernel": un[0] or None, "release": un[1] or None, "arch": un[2] or None, "hostname": un[3] or None,
               "pretty": (sec.get("os") or [None])[0] or None},
        "cpu": {"model": model, "threads": _int((sec.get("nproc") or [None])[0]), "sockets": _int(lscpu.get("Socket(s)")),
                "cores_per_socket": _int(lscpu.get("Core(s) per socket")), "threads_per_core": _int(lscpu.get("Thread(s) per core")),
                "numa_nodes": _int(lscpu.get("NUMA node(s)"))},
        "memory_kb": mem_kb,
        "disk": disk,
        "home": (sec.get("home") or [None])[0] or None,
        "shell": (sec.get("shell") or [None])[0] or None,
        "sudo": sudo if sudo in ("passwordless", "password", "none") else "none",
        "package_manager": (sec.get("pm") or [None])[0] or None,
        "gnu_find": gnu,
        "tools": tools,
        "gpus": parse_gpus(sec),
        "missing_required": missing_required,
        "complete": "end" in sec,
    }


async def probe(t: Target, h: DevHost) -> dict[str, Any]:
    work_dir = await t.expand(h.work_dir)
    rc, out, err = await t.run(["sh", "-s", "--", work_dir, *tool_specs(h)], timeout=TIMEOUT, stdin=SCRIPT.encode())
    caps = parse(out, h)
    caps["work_dir"] = work_dir
    if not caps["complete"]:
        caps["error"] = (err.strip() or f"exit {rc}")[-400:]
    return caps

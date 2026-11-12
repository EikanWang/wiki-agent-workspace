#!/usr/bin/env bash
# One-click install of Wiki Agent Workspace, from an empty Linux account to a running server.
#
#   curl -fsSL https://raw.githubusercontent.com/EikanWang/wiki-agent-workspace/main/scripts/install.sh | bash
#   # or, in a checkout:  scripts/install.sh [options]
#
# It installs what is missing (system packages with sudo when you allow it, otherwise user-level Python and Node in
# your home folder), gets the code, creates the wiki folder if needed, runs scripts/setup.sh (Python environment,
# browser UI, configuration and access token), starts the server as a user service, checks it and prints the URL and
# the token. Re-running it is safe: it upgrades the code's dependencies and keeps your configuration and data.
#
# Options:
#   --workspace DIR        your wiki folder (created with `git init` if it does not exist). Default: asked; ~/wiki with --yes
#   --dir DIR              where the code goes when this script runs outside a checkout (default ~/wiki-agent-workspace)
#   --repo URL             the repository to clone (default https://github.com/EikanWang/wiki-agent-workspace.git)
#   --agents LIST          agent CLIs to install if missing: opencode,codex,claude | all | none (default: asked; none with --yes)
#   --with LIST            optional features: gh (PR Review, PR Development's pull requests), office (Word / PowerPoint /
#                          Visio layout), dev (PR Development: clangd, basedpyright, code-server) | all | none (default: asked)
#   --port N  --bind ADDR  --state-dir DIR   passed to setup (defaults 8765, 127.0.0.1, ~/.local/share/wiki-agent-workspace)
#   --no-system-packages   never use sudo or the package manager (user-level installs only)
#   --no-service           do not start the server; print how to start it
#   -y, --yes              do not ask; take the defaults above
#   -h, --help
set -euo pipefail

REPO_URL="https://github.com/EikanWang/wiki-agent-workspace.git"
DIR="$HOME/wiki-agent-workspace"
WORKSPACE=""
AGENTS=""
WITH=""
YES=0
NO_SYSTEM=0
NO_SERVICE=0
SETUP_ARGS=()
ORIG_ARGS=("$@")

usage() { sed -n '2,27p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//' || true; }
while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --agents) AGENTS="$2"; shift 2 ;;
    --with) WITH="$2"; shift 2 ;;
    --port|--bind|--state-dir) SETUP_ARGS+=("$1" "$2"); shift 2 ;;
    --no-system-packages) NO_SYSTEM=1; shift ;;
    --no-service) NO_SERVICE=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# ------------------------------------------------------------------ output and questions
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=""; G=""; Y=""; R=""; N=""; fi
step() { echo; echo "${B}==> $*${N}"; }
ok() { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
fail() { echo "  ${R}✗${N} $*" >&2; exit 1; }
TTY=""
if [ "$YES" -eq 0 ] && [ -r /dev/tty ] && (: < /dev/tty) 2>/dev/null; then TTY=/dev/tty; fi  # also when piped from curl
ask() {  # ask "question" default → echoes the answer
  local q="$1" def="$2" a=""
  if [ -z "$TTY" ]; then echo "$def"; return; fi
  printf "  %s [%s] " "$q" "$def" > /dev/tty
  read -r a < /dev/tty || true
  echo "${a:-$def}"
}
yes_no() { case "$(ask "$1 (y/n)" "$2")" in y|Y|yes|Yes) return 0 ;; *) return 1 ;; esac; }
has() { command -v "$1" >/dev/null 2>&1; }
in_list() { case ",$1," in *",$2,"*|*",all,"*) return 0 ;; *) return 1 ;; esac; }

[ "$(uname -s)" = "Linux" ] || fail "the server runs on Linux (browsers on any system can use it)"
[ "$(id -u)" -ne 0 ] || fail "run this as the user who owns your wiki and agent logins, not as root"

# ------------------------------------------------------------------ system packages (optional, with sudo)
PM=""
for p in apt-get dnf yum zypper pacman apk; do has "$p" && { PM="$p"; break; }; done
SUDO=""
can_sudo() {
  [ "$NO_SYSTEM" -eq 0 ] && [ -n "$PM" ] || return 1
  if [ -z "$SUDO" ]; then
    if sudo -n true 2>/dev/null; then SUDO="sudo"
    elif [ -n "$TTY" ] && has sudo && yes_no "Install system packages with sudo (asks for your password)?" "y"; then SUDO="sudo"
    else NO_SYSTEM=1; return 1; fi
  fi
  return 0
}
APT_UPDATED=0
pkg_install() {  # pkg_install name… (distro package names)
  can_sudo || return 1
  echo "  installing $* ($PM)"
  case "$PM" in
    apt-get) [ "$APT_UPDATED" -eq 1 ] || { $SUDO apt-get update -qq || true; APT_UPDATED=1; }
             $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" ;;
    dnf|yum) $SUDO "$PM" install -y -q "$@" ;;
    zypper) $SUDO zypper -n -q install "$@" ;;
    pacman) $SUDO pacman -S --noconfirm --needed "$@" ;;
    apk) $SUDO apk add -q "$@" ;;
  esac
}
pkg_name() {  # generic name → this distro's package name
  case "$1:$PM" in
    python:apt-get) echo "python3 python3-venv python3-pip" ;;
    python:pacman) echo "python python-pip" ;;
    python:apk) echo "python3 py3-pip" ;;
    python:*) echo "python3 python3-pip" ;;
    node:apt-get|node:apk) echo "nodejs npm" ;;
    node:*) echo "nodejs npm" ;;
    office:apt-get) echo "libreoffice-writer libreoffice-impress libreoffice-draw" ;;
    office:pacman) echo "libreoffice-fresh" ;;
    office:apk) echo "libreoffice" ;;
    office:*) echo "libreoffice-writer libreoffice-impress libreoffice-draw" ;;
    clangd:apt-get|clangd:apk) echo "clangd" ;;
    clangd:pacman) echo "clang" ;;
    clangd:*) echo "clang-tools-extra" ;;
    *) echo "$1" ;;
  esac
}

mkdir -p "$HOME/.local/bin"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac

# ------------------------------------------------------------------ the code
step "Wiki Agent Workspace installer"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/../backend/app/cli.py" ]; then
  ROOT="$(cd "$HERE/.." && pwd)"
  ok "code: $ROOT"
else
  has git || pkg_install git || fail "Git is needed to get the code: install git and run this again"
  if [ -d "$DIR/.git" ]; then
    echo "  updating $DIR"
    git -C "$DIR" pull --ff-only -q || warn "could not update $DIR (local changes?); using it as it is"
  else
    echo "  cloning $REPO_URL → $DIR"
    git clone -q "$REPO_URL" "$DIR"
  fi
  exec bash "$DIR/scripts/install.sh" "${ORIG_ARGS[@]}"
fi
cd "$ROOT"

# ------------------------------------------------------------------ prerequisites
step "Prerequisites"
has git || pkg_install git || fail "Git is needed: install git (e.g. sudo apt install git) and run this again"
ok "git $(git --version | awk '{print $3}')"
has curl || pkg_install curl || fail "curl is needed: install curl and run this again"

if ! has tmux; then pkg_install tmux || true; fi
if has tmux; then ok "$(tmux -V)"; else warn "tmux is not installed: terminals stay disabled until you install tmux 3.2+ (the rest works)"; fi

py_ok() { "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
PY=""
for c in ${WAW_PYTHON:-} python3.13 python3.12 python3.11 python3; do
  if has "$c" && py_ok "$c"; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  # shellcheck disable=SC2046
  pkg_install $(pkg_name python) >/dev/null 2>&1 || true
  for c in python3.13 python3.12 python3.11 python3; do if has "$c" && py_ok "$c"; then PY="$(command -v "$c")"; break; fi; done
fi
if [ -z "$PY" ]; then
  echo "  no Python 3.11+ with venv: installing one in your home folder with uv (https://docs.astral.sh/uv/)"
  has uv || curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  has uv || export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  uv python install 3.12 >/dev/null
  PY="$(uv python find 3.12)"
fi
py_ok "$PY" || fail "could not get Python 3.11+ (found $PY)"
export WAW_PYTHON="$PY"
ok "Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"

node_ok() { has node && has npm && [ "$(node -e 'process.stdout.write(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)" -ge 20 ]; }
NODE_HOME="$HOME/.local/share/wiki-agent-workspace/node"
[ -x "$NODE_HOME/bin/node" ] && export PATH="$NODE_HOME/bin:$PATH"
if ! node_ok; then
  # shellcheck disable=SC2046
  pkg_install $(pkg_name node) >/dev/null 2>&1 || true
fi
if ! node_ok; then
  echo "  no Node.js 20+: installing Node 22 in $NODE_HOME (needed only to build the browser UI)"
  case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) fail "no Node.js build for $(uname -m): install Node 20+ yourself" ;; esac
  base="https://nodejs.org/dist/latest-v22.x"
  tarball="$(curl -fsSL "$base/SHASUMS256.txt" | awk -v a="linux-$arch.tar.xz" '$2 ~ a"$" {print $2; exit}')"
  [ -n "$tarball" ] || fail "could not find the Node.js download (no internet access?)"
  rm -rf "$NODE_HOME" && mkdir -p "$NODE_HOME"
  curl -fsSL "$base/$tarball" | tar -xJ -C "$NODE_HOME" --strip-components=1
  export PATH="$NODE_HOME/bin:$PATH"
fi
node_ok || fail "could not get Node.js 20+"
ok "Node $(node --version), npm $(npm --version)"

# ------------------------------------------------------------------ the wiki folder
step "Your wiki folder"
CFG="${WAW_CONFIG:-$HOME/.config/wiki-agent-workspace/config.toml}"
if [ -z "$WORKSPACE" ] && [ -f "$CFG" ]; then
  WORKSPACE="$("$PY" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb")).get("workspace",{}).get("root",""))' "$CFG" 2>/dev/null || true)"
  [ -n "$WORKSPACE" ] && echo "  from the existing configuration: $WORKSPACE"
fi
[ -n "$WORKSPACE" ] || WORKSPACE="$(ask "Where is your wiki (a folder of Markdown files, usually a Git checkout)?" "$HOME/wiki")"
WORKSPACE="${WORKSPACE/#\~/$HOME}"
if [ ! -d "$WORKSPACE" ]; then
  if yes_no "$WORKSPACE does not exist. Create it as a new wiki (a Git repository with a first page)?" "y"; then
    mkdir -p "$WORKSPACE"
    printf '# My wiki\n\nWrite Markdown pages here, or ask the agent to. Open this folder in Wiki Agent Workspace.\n' > "$WORKSPACE/README.md"
    git -C "$WORKSPACE" init -q
    ok "created $WORKSPACE"
  else
    fail "give an existing folder with --workspace DIR"
  fi
fi
ok "$WORKSPACE"

# ------------------------------------------------------------------ agents (optional, user level)
step "Agent CLIs"
if [ -z "$AGENTS" ]; then
  missing=""
  for a in opencode codex claude; do has "$a" || missing="$missing${missing:+,}$a"; done
  if [ -z "$missing" ]; then AGENTS="none"
  elif [ -n "$TTY" ]; then AGENTS="$(ask "Install missing agent CLIs ($missing; or none)?" "none")"
  else AGENTS="none"; fi
fi
npm_user() { npm install -g --silent --prefix "$HOME/.local" "$@" >/dev/null 2>&1; }
if in_list "$AGENTS" opencode && ! has opencode; then
  echo "  installing OpenCode (https://opencode.ai)"
  curl -fsSL https://opencode.ai/install | bash >/dev/null 2>&1 || warn "OpenCode install failed (see https://opencode.ai/docs/)"
  [ -x "$HOME/.opencode/bin/opencode" ] && export PATH="$HOME/.opencode/bin:$PATH"
fi
if in_list "$AGENTS" codex && ! has codex; then echo "  installing Codex CLI"; npm_user @openai/codex || warn "Codex install failed (npm install -g @openai/codex)"; fi
if in_list "$AGENTS" claude && ! has claude; then echo "  installing Claude Code"; npm_user @anthropic-ai/claude-code || warn "Claude Code install failed"; fi
for a in opencode codex claude; do if has "$a"; then ok "$a: $(command -v "$a")"; else warn "$a: not installed"; fi; done
if ! has opencode && ! has codex; then warn "no OpenCode or Codex: reading, editing and terminals work; discussions and chats need one of them"; fi

# ------------------------------------------------------------------ optional features
step "Optional features"
if [ -z "$WITH" ]; then
  if [ -n "$TTY" ]; then WITH="$(ask "Also install: gh (PR Review), office (Word/PowerPoint/Visio layout), dev (PR Development tools); all, none or a list" "gh")"
  else WITH="none"; fi
fi
if in_list "$WITH" gh && ! has gh; then
  pkg_install gh >/dev/null 2>&1 || {
    echo "  installing the GitHub CLI into ~/.local/bin"
    case "$(uname -m)" in x86_64) ga=amd64 ;; aarch64|arm64) ga=arm64 ;; *) ga="" ;; esac
    url="$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | "$PY" -c 'import json,sys; a=sys.argv[1]; print(next((x["browser_download_url"] for x in json.load(sys.stdin)["assets"] if x["name"].endswith(f"linux_{a}.tar.gz")), ""))' "$ga" 2>/dev/null || true)"
    if [ -n "$url" ]; then tmp="$(mktemp -d)"; curl -fsSL "$url" | tar -xz -C "$tmp" --strip-components=1 && cp "$tmp/bin/gh" "$HOME/.local/bin/gh"; rm -rf "$tmp"
    else warn "could not download gh: see https://cli.github.com"; fi
  }
fi
if in_list "$WITH" office && ! has soffice; then
  # shellcheck disable=SC2046
  pkg_install $(pkg_name office) >/dev/null 2>&1 || warn "LibreOffice needs the package manager (sudo): skipped"
fi
if in_list "$WITH" dev; then
  has clangd || { # shellcheck disable=SC2046
    pkg_install $(pkg_name clangd) >/dev/null 2>&1 || warn "clangd needs the package manager (sudo): skipped"; }
  has code-server || { echo "  installing code-server into ~/.local"
    curl -fsSL https://code-server.dev/install.sh | sh -s -- --method=standalone --prefix="$HOME/.local" >/dev/null 2>&1 || warn "code-server install failed"; }
fi
for t in gh soffice clangd code-server; do if has "$t"; then ok "$t"; fi; done

# ------------------------------------------------------------------ setup
step "Setup (Python environment, browser UI, configuration)"
bash scripts/setup.sh --workspace "$WORKSPACE" ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"} | sed 's/^/  /'
if in_list "$WITH" dev && [ ! -x .venv/bin/basedpyright-langserver ]; then
  .venv/bin/python -m pip install --quiet basedpyright >/dev/null 2>&1 && ok "basedpyright (Python language server)" || warn "basedpyright install failed"
fi

# ------------------------------------------------------------------ start
URL="$(.venv/bin/python -c 'from backend.app.config import load_settings as l; s=l(); h="127.0.0.1" if s.bind in ("0.0.0.0","::","") else s.bind; print(f"http://{h}:{s.port}")')"
if [ "$NO_SERVICE" -eq 0 ]; then
  step "Starting the server"
  # keep the service running after you log out and start it at boot ("lingering"): ask, unless --yes
  if [ -n "$TTY" ] && ! yes_no "Keep the server running after you log out, and start it at boot?" "y"; then export WAW_NO_LINGER=1; fi
  bash scripts/service.sh install | sed 's/^/  /' || warn "the server did not come up: scripts/service.sh logs"
fi

step "Check"
bash scripts/doctor.sh --quick | sed 's/^/  /' || true

TOKEN="$(.venv/bin/python -c 'from backend.app.config import load_settings; print(load_settings().access_token)')"
echo
echo "${B}Done.${N}"
if [ "$NO_SERVICE" -eq 0 ]; then echo "  Open:          ${B}$URL${N}"; else echo "  Start it with: scripts/start.sh   (or scripts/service.sh install)  → $URL"; fi
echo "  Access token:  ${B}$TOKEN${N}   (paste it once; scripts/setup.sh --show-token shows it again)"
PORT="${URL##*:}"
echo "  From another machine: ssh -N -L $PORT:127.0.0.1:$PORT $USER@$(hostname -f 2>/dev/null || hostname)  then open http://127.0.0.1:$PORT"
echo
echo "  Next steps (as needed):"
has opencode && echo "    opencode auth login        # once, if OpenCode is not logged in yet"
has codex && echo "    codex login                # once, if Codex is not logged in yet"
has gh && echo "    gh auth login              # PR Review / PR Development; then in the app: account menu → Set up “Continue with GitHub”…"
echo "    scripts/doctor.sh          # check everything (with fixes);  scripts/service.sh status|logs|restart"
echo "    docs/DEPLOYMENT.md         # remote access over HTTPS, several wikis, SSH development hosts, upgrades"

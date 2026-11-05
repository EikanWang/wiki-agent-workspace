"""Command-line entry point: `wiki-agent-workspace setup|serve|print-config`."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .config import (
    DEFAULT_CONFIG_PATH,
    ProviderConfig,
    Settings,
    detect_default_provider,
    ensure_secrets,
    ensure_state_dir,
    load_settings,
    write_settings,
)


def cmd_setup(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config or os.environ.get("WAW_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    settings = load_settings(cfg_path)
    settings.config_path = cfg_path
    if args.workspace:
        settings.workspace_root = Path(args.workspace).expanduser().resolve()
    if not settings.workspace_root.is_dir():
        print(f"error: workspace root does not exist: {settings.workspace_root}", file=sys.stderr)
        return 2
    if args.bind:
        settings.bind = args.bind
    if args.port:
        settings.port = args.port
    if args.state_dir:
        settings.state_dir = Path(args.state_dir).expanduser()
    if args.codex_sandbox:
        settings.providers.setdefault("codex", ProviderConfig()).sandbox = args.codex_sandbox
    if args.default_provider:
        settings.default_provider = args.default_provider
    elif not settings.default_provider:
        settings.default_provider = detect_default_provider(settings.providers)
    settings, generated = ensure_secrets(settings)
    ensure_state_dir(settings)
    write_settings(settings)

    print(f"Configuration written to {settings.config_path} (mode 0600)")
    print(f"Workspace root: {settings.workspace_root}")
    print(f"State directory: {settings.state_dir}")
    print(f"Bind: http://{settings.bind}:{settings.port}")
    for name in ("opencode", "codex"):
        p = settings.providers.get(name, ProviderConfig())
        exe = shutil.which(p.bin or name)
        print(f"Provider {name}: {'found at ' + exe if exe else 'not found on PATH'}")
    if settings.default_provider:
        print(f"Default provider: {settings.default_provider}")
    else:
        print("Default provider: none detected. Reader and terminal work; discussions need opencode or codex on PATH "
              "(set agents.default_provider in the config file afterwards).")
    print(f"Codex app-driven sandbox: {settings.providers['codex'].sandbox} (scoped to the workspace; change with --codex-sandbox)")
    if not shutil.which("tmux"):
        print("WARNING: tmux not found; terminals are disabled until tmux 3.2+ is installed.")
    if generated or args.show_token:
        print()
        print("Access token (shown once; stored only in the config file):")
        print(f"  {settings.access_token}")
        print("Open the URL above in your browser and paste the token at the login screen.")
    return 0


def cmd_print_config(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config) if args.config else None)
    d = settings.to_toml_dict()
    d["auth"] = {"access_token": "[redacted]", "server_secret": "[redacted]"}
    import tomli_w

    print(tomli_w.dumps(d))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    if args.config:
        os.environ["WAW_CONFIG"] = args.config
    from .main import run

    run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki-agent-workspace")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="create/update the configuration file and generate the access token")
    s.add_argument("--workspace", help="existing directory containing the Wiki (default: current dir / existing config)")
    s.add_argument("--bind")
    s.add_argument("--port", type=int)
    s.add_argument("--state-dir")
    s.add_argument("--default-provider", choices=["opencode", "codex"])
    s.add_argument("--codex-sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    s.add_argument("--show-token", action="store_true", help="print the access token again")
    s.set_defaults(fn=cmd_setup)
    p = sub.add_parser("print-config", help="print the effective configuration (secrets redacted)")
    p.set_defaults(fn=cmd_print_config)
    r = sub.add_parser("serve", help="start the web server")
    r.set_defaults(fn=cmd_serve)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

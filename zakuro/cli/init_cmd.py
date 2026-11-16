"""`zakuro init` — interactive setup wizard.

Non-interactive (CI) usage: supply `--mode {local,distributed}`, plus
`--api-key`, `--dashboard-url`, `--install-zc/--no-install-zc` for distributed
mode, and `--yes` to skip confirmation prompts.
"""

from __future__ import annotations

import argparse
import sys
from typing import cast

from zakuro.cli import doctor_cmd, prompts, zc_installer
from zakuro.config import Config, load_user_config, write_user_config


def run(args: argparse.Namespace) -> int:
    interactive = prompts.is_interactive()

    try:
        existing = load_user_config()
    except Exception as exc:
        print(f"failed to load existing config: {exc!r}", file=sys.stderr)
        return 1

    print("Welcome to Zakuro setup.\n")

    # 1. Mode
    mode = _resolve_mode(args, existing, interactive)
    if mode is None:
        return 2

    print(f"Mode: {mode}\n")

    zc_install_failed = False

    if mode == "local":
        new_cfg = Config(
            default_host=Config().default_host,
            default_port=Config().default_port,
            auth_token=None,
            tailscale_enabled=existing.tailscale_enabled,
        )
    else:
        dashboard_url = _resolve_dashboard_url(args, existing, interactive)
        if dashboard_url is None:
            return 2
        api_key = _resolve_api_key(args, existing, interactive)
        if api_key is None:
            return 2

        host, port = _split_dashboard_url(dashboard_url)
        new_cfg = Config(
            default_host=host,
            default_port=port,
            auth_token=api_key,
            tailscale_enabled=existing.tailscale_enabled,
        )

        if _resolve_install_zc(args, interactive):
            result = zc_installer.install_zc()
            print(f"  zc install: {result.status} — {result.message}")
            if result.status == "failed":
                zc_install_failed = True

    target = write_user_config(new_cfg)
    print(f"\nWrote {target} (mode 0600).\n")

    # Verify
    print("Verifying:")
    doctor_rc = doctor_cmd.run(argparse.Namespace())

    if mode == "distributed":
        print(
            "\nTo use `zc` in this shell:\n"
            f'  export ZAKURO_API_KEY="$(zakuro config get auth_token)"\n'
            f'  export ZAKURO_API_URL="https://{new_cfg.default_host}"\n'
            "(Add these to your shell rc to persist.)\n"
        )

    print("You're ready. Try the @zk.fn example in the Quickstart.")

    # Config is always written; but surface non-zero exit if any step failed.
    if zc_install_failed:
        return 1
    return doctor_rc


def _resolve_mode(args: argparse.Namespace, existing: Config, interactive: bool) -> str | None:
    if args.mode in ("local", "distributed"):
        return cast(str, args.mode)
    if not interactive:
        print(
            "error: --mode is required in non-interactive mode (choices: local, distributed)",
            file=sys.stderr,
        )
        return None
    default = "distributed" if existing.auth_token else "local"
    return prompts.ask_choice(
        "Which mode do you want to run in?",
        ["local", "distributed"],
        default=default,
    )


def _resolve_dashboard_url(
    args: argparse.Namespace, existing: Config, interactive: bool
) -> str | None:
    if args.dashboard_url:
        return cast(str, args.dashboard_url)
    if not interactive:
        print("error: --dashboard-url is required for distributed mode", file=sys.stderr)
        return None
    default_url = (
        f"https://{existing.default_host}"
        if existing.auth_token and existing.default_host != "localhost"
        else "https://hub.zakuro-ai.com"
    )
    return prompts.ask("Dashboard URL", default=default_url)


def _resolve_api_key(args: argparse.Namespace, existing: Config, interactive: bool) -> str | None:
    if args.api_key:
        if not args.api_key.startswith("zk_"):
            print(
                "warning: the API key does not start with 'zk_'",
                file=sys.stderr,
            )
        return cast(str, args.api_key)
    if not interactive:
        print("error: --api-key is required for distributed mode", file=sys.stderr)
        return None
    return prompts.secret("ZAKURO_API_KEY (zk_...)")


def _resolve_install_zc(args: argparse.Namespace, interactive: bool) -> bool:
    if args.install_zc is not None:
        return bool(args.install_zc)
    if not interactive:
        return False
    return prompts.confirm("Install the `zc` broker CLI now?", default=True)


def _split_dashboard_url(url: str) -> tuple[str, int]:
    """Extract host and port from a dashboard URL.

    Examples:
        https://hub.zakuro-ai.com        -> ("hub.zakuro-ai.com", 9000)
        https://broker.example.com:9100  -> ("broker.example.com", 9100)
    """
    cleaned = url.replace("https://", "").replace("http://", "").rstrip("/")
    if ":" in cleaned:
        host, port_str = cleaned.split(":", 1)
        try:
            return host, int(port_str)
        except ValueError:
            return host, 9000
    return cleaned, 9000


# Starter config for ``zakuro init --path PATH`` (the public 0.2.24 CLI's
# non-interactive ``init``). Keys are the ones ``Config.load()`` understands;
# defaults mirror ``Config()``.
_STARTER_TEMPLATE = """\
# Zakuro configuration. Values here are overridden by ZAKURO_* environment variables.

host: {host}   # broker host
port: {port}   # broker port
# auth: <api-token>      # or set ZAKURO_AUTH

hub:
  url: {hub_url}
"""


def write_starter_config(path: str, *, force: bool = False) -> int:
    """Write a commented starter config to *path*; refuse to clobber without *force*."""
    from pathlib import Path

    target = Path(path).expanduser()
    if target.exists() and not force:
        print(f"Config already exists at {target} (use --force to overwrite).", file=sys.stderr)
        return 1
    defaults = Config()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _STARTER_TEMPLATE.format(
            host=defaults.default_host, port=defaults.default_port, hub_url=defaults.hub_url
        ),
        encoding="utf-8",
    )
    print(f"Wrote starter config to {target}")
    print("Edit it, or override any value with ZAKURO_* environment variables.")
    return 0

"""`zakuro config get <key>` — read-only field accessor for ~/.zakuro/config.yaml."""

from __future__ import annotations

import argparse
import sys

from zakuro.config import load_user_config

_PUBLIC_FIELDS = (
    "default_host",
    "default_port",
    "auth_token",
    "tailscale_enabled",
    "hub_url",
    "cache_dir",
)


def run(args: argparse.Namespace) -> int:
    if args.action != "get":
        print(f"unknown action: {args.action!r}", file=sys.stderr)
        return 2
    if args.key not in _PUBLIC_FIELDS:
        print(
            f"unknown config key: {args.key!r}. Known keys: {', '.join(_PUBLIC_FIELDS)}",
            file=sys.stderr,
        )
        return 2
    cfg = load_user_config()
    value = getattr(cfg, args.key)
    print("" if value is None else value)
    return 0

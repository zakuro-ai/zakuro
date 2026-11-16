"""`zakuro config get [KEY] [--json]` — read-only accessor for ~/.zakuro/config.yaml."""

from __future__ import annotations

import argparse
import json
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
    key = getattr(args, "key", None)
    cfg = load_user_config()
    if key is None:
        # All fields, with the auth token masked (as the public 0.2.24 CLI did).
        data = cfg.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2, default=str))
        else:
            for k in sorted(data):
                print(f"{k} = {data[k]}")
        return 0
    if key not in _PUBLIC_FIELDS:
        print(
            f"Unknown config key {key!r}. Known keys: {', '.join(_PUBLIC_FIELDS)}",
            file=sys.stderr,
        )
        return 1
    value = getattr(cfg, key)
    print("" if value is None else value)
    return 0

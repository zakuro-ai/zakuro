"""Entry point for the ``zakuro`` CLI — setup and diagnostics.

Distinct from ``zakuro-worker`` (which runs a worker server). This provides:

* ``zakuro init``               — scaffold a config file.
* ``zakuro doctor``             — environment / install diagnostics.
* ``zakuro config get [KEY]``   — read resolved configuration.
* ``zakuro allocator replay LOG`` — summarise an allocator decision log.

Kept import-light: heavy optional deps are probed lazily inside ``doctor`` so
``zakuro --help`` works in a core-only install.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from zakuro import __version__

# Default scaffold written by `zakuro init`. Mirrors the keys understood by
# zakuro.config.Config.load(); secrets are left blank to fill in.
_CONFIG_TEMPLATE = """\
# Zakuro configuration. Precedence (high to low): ZAKURO_* env vars,
# ./zakuro.yaml, ~/.zakuro/config.yaml, built-in defaults.

# Broker
host: my.zakuro-ai.com
port: 9000
# auth: <token>            # or set ZAKURO_AUTH

# Object storage (MinIO)
storage:
  host: localhost:9000
  access_key: ""
  secret_key: ""
  secure: false

# Model/data hub
hub:
  url: http://hub.zakuro.ai
"""

# Friendly aliases accepted by `zakuro config get` in addition to the raw
# Config attribute names.
#: Substrings that mark a config field as a credential. Matched against the
#: attribute name so the rule holds for fields that do not exist yet.
_SECRET_MARKERS = ("secret", "token", "password", "_key", "credential")


def _is_secret_attr(attr: str) -> bool:
    """Would printing this attribute disclose a credential?

    Deliberately name-based and deliberately broad. The alternative -- an
    explicit list of secret fields -- is the same hand-maintained construct
    that already failed once in `Config.to_dict`.
    """
    lowered = attr.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


_CONFIG_ALIASES = {
    "host": "default_host",
    "port": "default_port",
    "auth": "auth_token",
}


def _default_config_path(local: bool) -> Path:
    return Path("./zakuro.yaml") if local else Path.home() / ".zakuro" / "config.yaml"


def _cmd_init(args: argparse.Namespace) -> int:
    path = _default_config_path(args.local)
    if path.exists() and not args.force:
        print(f"refusing to overwrite existing {path} (use --force)", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    print(f"wrote {path}")
    return 0


def _probe(module: str) -> bool:
    """True if ``module`` can be imported. Used for extra-availability checks."""
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"zakuro {__version__}")
    py = sys.version_info
    py_ok = py >= (3, 10)
    print(f"  [{'OK' if py_ok else '!!'}] python {py.major}.{py.minor}.{py.micro} (need >= 3.10)")
    ok = ok and py_ok

    extras = {
        "worker": ["fastapi", "uvicorn", "psutil", "aioquic"],
        "observability": ["structlog", "prometheus_client", "opentelemetry"],
        "ray": ["ray"],
        "dask": ["dask"],
        "spark": ["pyspark"],
    }
    print("  extras:")
    for extra, mods in extras.items():
        missing = [m for m in mods if not _probe(m)]
        status = "OK" if not missing else f"missing {', '.join(missing)}"
        print(f"    [{'OK' if not missing else '--'}] {extra}: {status}")

    print("  config:")
    found_any = False
    for path in (Path("./zakuro.yaml"), Path.home() / ".zakuro" / "config.yaml"):
        if path.exists():
            found_any = True
            print(f"    [OK] {path}")
    if not found_any:
        print("    [--] no config file found (run `zakuro init`)")

    log_path = os.environ.get("ZAKURO_DECISION_LOG") or str(
        Path.home() / ".zakuro" / "allocator.jsonl"
    )
    exists = Path(log_path).exists()
    print(
        f"  decision log: [{'OK' if exists else '--'}] {log_path}{'' if exists else ' (none yet)'}"
    )

    print("status:", "ok" if ok else "problems found")
    return 0 if ok else 1


def _cmd_config_get(args: argparse.Namespace) -> int:
    from zakuro import Config

    cfg = Config.load()
    redacted = cfg.to_dict()  # auth_token already masked here
    if args.key is None:
        for k, v in redacted.items():
            print(f"{k}={v}")
        return 0

    attr = _CONFIG_ALIASES.get(args.key, args.key)
    if attr not in redacted and not hasattr(cfg, attr):
        print(f"unknown config key: {args.key}", file=sys.stderr)
        return 1
    # Two layers, because the first one alone is what failed. `to_dict()` is
    # hand-maintained, so a secret added to Config and forgotten here would be
    # printed in clear by the fallback below -- which is exactly how
    # storage_secret_key, storage_access_key and tailscale_auth_key leaked.
    # The denylist is derived from the field NAME, so a future `*_key`,
    # `*_token` or `*_secret` field is covered the day it is added, whether or
    # not anyone remembers this function.
    if attr not in redacted and _is_secret_attr(attr):
        print(f"refusing to print secret config key: {args.key}", file=sys.stderr)
        return 1
    value = redacted[attr] if attr in redacted else getattr(cfg, attr)
    print(value)
    return 0


def _cmd_allocator_replay(args: argparse.Namespace) -> int:
    from zakuro.replay import replay_decisions

    try:
        summary = replay_decisions(args.log)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(summary.render())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zakuro", description="Zakuro setup and diagnostics CLI")
    parser.add_argument("--version", action="version", version=f"zakuro {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Scaffold a config file")
    p_init.add_argument(
        "--local", action="store_true", help="Write ./zakuro.yaml instead of ~/.zakuro/config.yaml"
    )
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing file")
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="Report environment and install diagnostics")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_config = sub.add_parser("config", help="Inspect resolved configuration")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_get = config_sub.add_parser("get", help="Print a config value (or all)")
    p_config_get.add_argument(
        "key", nargs="?", default=None, help="Config key (e.g. host); omit for all"
    )
    p_config_get.set_defaults(func=_cmd_config_get)

    p_alloc = sub.add_parser("allocator", help="Allocator tooling")
    alloc_sub = p_alloc.add_subparsers(dest="allocator_command")
    p_replay = alloc_sub.add_parser("replay", help="Summarise an allocator decision log")
    p_replay.add_argument("log", help="Path to the decision-log JSONL file")
    p_replay.set_defaults(func=_cmd_allocator_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 1
    result = func(args)
    return int(result)


if __name__ == "__main__":
    sys.exit(main())

"""The ``zakuro`` command-line entry point (#222).

A light-weight operator/setup CLI, distinct from ``zakuro-worker`` (which
starts a worker server). Subcommands:

    zakuro init                 # scaffold a user config at ~/.zakuro/config.yaml
    zakuro doctor               # environment + connectivity diagnostics
    zakuro config get [KEY]     # print resolved configuration
    zakuro allocator replay LOG # summarise an allocator decision log

Kept import-light: heavy / optional dependencies are imported lazily inside
the subcommand that needs them so ``zakuro --help`` works on a core install.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from zakuro import __version__

# Starter config written by ``zakuro init``. Mirrors the keys Config.load()
# understands (see zakuro/config.py) with production-safe defaults commented
# so a new user knows what is tunable.
_INIT_TEMPLATE = """\
# Zakuro configuration — see https://docs.zakuro-ai.com for the full reference.
# Values here are overridden by ZAKURO_* environment variables.

host: my.zakuro-ai.com   # default broker host
port: 9000               # default broker port
# auth: <api-token>      # or set ZAKURO_AUTH

tailscale:
  enabled: true

hub:
  url: http://hub.zakuro.ai
"""


def _default_config_path() -> Path:
    return Path.home() / ".zakuro" / "config.yaml"


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else _default_config_path()
    if path.exists() and not args.force:
        print(
            f"Config already exists at {path} (use --force to overwrite).",
            file=sys.stderr,
        )
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_INIT_TEMPLATE, encoding="utf-8")
    print(f"Wrote starter config to {path}")
    print("Edit it, or override any value with ZAKURO_* environment variables.")
    return 0


def _worker_extra_available() -> tuple[bool, list[str]]:
    """Return (all_present, missing) for the ``[worker]`` extra dependencies."""
    missing = []
    for mod in ("fastapi", "uvicorn", "psutil", "aioquic"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return (not missing, missing)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from zakuro.config import Config
    from zakuro.standalone import detect_backend

    print(f"zakuro {__version__}")
    print(f"  python: {sys.version.split()[0]} ({sys.executable})")

    worker_ok, missing = _worker_extra_available()
    if worker_ok:
        print("  worker extra: installed (fastapi, uvicorn, psutil, aioquic)")
    else:
        print(f"  worker extra: MISSING {', '.join(missing)} — `pip install 'zakuro-ai[worker]'`")

    cfg_path = _default_config_path()
    print(
        f"  config file: {cfg_path} ({'present' if cfg_path.exists() else 'not found — run `zakuro init`'})"
    )

    config = Config.load()
    print(f"  broker: {config.default_host}:{config.default_port}")

    # Backend detection touches the network (localhost + configured broker),
    # so it is opt-out via --no-probe for air-gapped / offline diagnostics.
    if args.no_probe:
        print("  backend: probe skipped (--no-probe)")
    else:
        backend = detect_backend(config)
        if backend is None:
            print("  backend: none reachable — calls run standalone (in-process)")
        else:
            print(f"  backend: {backend}")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from zakuro.config import Config

    config = Config.load()
    data = config.to_dict()  # auth_token is masked here
    if args.key:
        if args.key not in data:
            valid = ", ".join(sorted(data))
            print(
                f"Unknown config key {args.key!r}. Known keys: {valid}",
                file=sys.stderr,
            )
            return 1
        value = data[args.key]
        print(value if value is not None else "")
        return 0
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        for key in sorted(data):
            print(f"{key} = {data[key]}")
    return 0


def _cmd_allocator_replay(args: argparse.Namespace) -> int:
    from zakuro.adaptive import replay_decisions

    try:
        replay = replay_decisions(args.log)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        payload: dict[str, Any] = {
            "path": replay.path,
            "schema": replay.schema,
            "total": replay.total,
            "ok": replay.ok,
            "failed": replay.failed,
            "dropped": replay.dropped,
            "duration_secs": replay.duration_secs,
            "mean_expected_secs": replay.mean_expected_secs,
            "mean_actual_secs": replay.mean_actual_secs,
            "calibration_error_secs": replay.calibration_error_secs,
            "fn_counts": replay.fn_counts,
            "workers": [
                {
                    "idx": w.idx,
                    "picks": w.picks,
                    "ok": w.ok,
                    "failed": w.failed,
                    "failure_rate": w.failure_rate,
                    "mean_actual_secs": w.mean_actual_secs,
                }
                for w in (replay.workers[i] for i in sorted(replay.workers))
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(replay.summary())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zakuro",
        description="Zakuro setup + operations CLI (see `zakuro-worker` to start a worker).",
    )
    parser.add_argument("--version", action="version", version=f"zakuro {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Scaffold a user config file.")
    p_init.add_argument("--path", default=None, help="Config path (default ~/.zakuro/config.yaml).")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="Diagnose the environment + backend reachability.")
    p_doctor.add_argument("--no-probe", action="store_true", help="Skip network backend probing.")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_config = sub.add_parser("config", help="Inspect resolved configuration.")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_get = config_sub.add_parser("get", help="Print config (all, or a single KEY).")
    p_get.add_argument("key", nargs="?", default=None, help="Config key to print.")
    p_get.add_argument("--json", action="store_true", help="Emit JSON.")
    p_get.set_defaults(func=_cmd_config)

    p_alloc = sub.add_parser("allocator", help="AdaptiveCompute allocator tools.")
    alloc_sub = p_alloc.add_subparsers(dest="allocator_command", required=True)
    p_replay = alloc_sub.add_parser("replay", help="Summarise a decision-log JSONL file.")
    p_replay.add_argument("log", help="Path to the allocator decision log.")
    p_replay.add_argument("--json", action="store_true", help="Emit JSON.")
    p_replay.set_defaults(func=_cmd_allocator_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

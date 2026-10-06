"""Top-level `zakuro` CLI: dispatches `init`, `doctor`, `config`."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from zakuro.cli import allocator_cmd, config_cmd, doctor_cmd, init_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zakuro",
        description="Zakuro setup and diagnostics CLI.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    init_p = sub.add_parser("init", help="Interactive setup wizard.")
    init_p.add_argument(
        "--mode",
        choices=["local", "distributed"],
        default=None,
        help="Skip the mode prompt.",
    )
    init_p.add_argument("--api-key", default=None, help="Skip the API-key prompt.")
    init_p.add_argument(
        "--dashboard-url",
        default=None,
        help="Skip the dashboard-URL prompt (e.g. https://hub.zakuro-ai.com).",
    )
    install_grp = init_p.add_mutually_exclusive_group()
    install_grp.add_argument(
        "--install-zc",
        dest="install_zc",
        action="store_true",
        default=None,
        help="Install the `zc` CLI without prompting.",
    )
    install_grp.add_argument(
        "--no-install-zc",
        dest="install_zc",
        action="store_false",
        help="Skip installing `zc`.",
    )
    init_p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Assume yes for all confirmation prompts.",
    )

    sub.add_parser("doctor", help="Verify the install.")

    config_p = sub.add_parser("config", help="Read fields from ~/.zakuro/config.yaml.")
    config_sub = config_p.add_subparsers(dest="action", metavar="ACTION")
    get_p = config_sub.add_parser("get", help="Print a single field's value.")
    get_p.add_argument("key", help="Config field name (e.g. auth_token, default_host).")

    alloc_p = sub.add_parser("allocator", help="Inspect AdaptiveCompute decision logs.")
    alloc_sub = alloc_p.add_subparsers(dest="action", metavar="ACTION")
    replay_p = alloc_sub.add_parser("replay", help="Summarise a decision-log JSONL file.")
    replay_p.add_argument(
        "log",
        help="Path to the decision log (default written to ~/.zakuro/allocator.jsonl).",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return init_cmd.run(args)
    if args.command == "doctor":
        return doctor_cmd.run(args)
    if args.command == "config":
        if args.action == "get":
            return config_cmd.run(args)
        parser.parse_args(["config", "--help"])  # exits
        return 2
    if args.command == "allocator":
        if args.action == "replay":
            return allocator_cmd.run(args)
        parser.parse_args(["allocator", "--help"])  # exits
        return 2

    parser.print_help()
    return 2

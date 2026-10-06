"""`zakuro allocator replay <log>` — summarise an AdaptiveCompute decision log."""

from __future__ import annotations

import argparse
from pathlib import Path


def run(args: argparse.Namespace) -> int:
    from zakuro.replay import format_report, load_decisions, summarize_decisions

    path = Path(args.log).expanduser()
    if not path.exists():
        print(f"decision log not found: {path}")
        return 1

    rows = load_decisions(path)
    if not rows:
        print(f"no decisions found in {path}")
        return 1

    print(format_report(summarize_decisions(rows)))
    return 0

"""`zakuro allocator replay <log>` — summarise an AdaptiveCompute decision log."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def run(args: argparse.Namespace) -> int:
    from zakuro.replay import format_report, load_decisions, summarize_decisions

    path = Path(args.log).expanduser()
    if not path.exists():
        print(f"decision log not found: {path}")
        return 1

    if getattr(args, "json", False):
        return _print_json(path)

    rows = load_decisions(path)
    if not rows:
        print(f"no decisions found in {path}")
        return 1

    print(f"Decision log: {path}")
    print(format_report(summarize_decisions(rows)))
    return 0


def _print_json(path: Path) -> int:
    """Machine-readable summary, in the shape the public 0.2.24 CLI emitted."""
    from zakuro.adaptive import replay_decisions

    replay = replay_decisions(path)
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
        "task_batches": replay["task_batches"],
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0

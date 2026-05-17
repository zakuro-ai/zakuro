#!/usr/bin/env python3
"""Compare a fresh bench_all.py JSON output against a stored baseline.

Used by the nightly continuous-benchmarking workflow (#130). Exits 1 and
prints a markdown table of regressions when any headline metric is more
than ``--threshold`` slower than its baseline. The CI workflow turns that
exit code into a GitHub Issue.

Baselines live in bench/baselines/<runner-slug>.json (currently just one,
for the ubuntu-latest hosted runner — extend the matrix when we add an
arm64 self-hosted runner).

Re-bless a baseline with::

    python scripts/bench_compare.py --result results.json \\
        --baseline bench/baselines/ubuntu-latest.json --rebless

This script never mutates the baseline unless ``--rebless`` is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _flatten_metrics(payload: Any, prefix: str = "") -> dict[str, float]:
    """Walk a nested dict/list structure, keep numeric leaves keyed by path."""
    out: dict[str, float] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            out.update(_flatten_metrics(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            out.update(_flatten_metrics(value, f"{prefix}[{index}]"))
    elif isinstance(payload, (int, float)) and not isinstance(payload, bool):
        out[prefix] = float(payload)
    return out


def _classify(baseline: float, current: float, *, threshold: float) -> str:
    """Return one of {"ok", "regression", "improvement"} for a given pair."""
    if baseline == 0:
        return "ok"  # no signal to compare against
    delta = (current - baseline) / abs(baseline)
    if delta > threshold:
        return "regression"
    if delta < -threshold:
        return "improvement"
    return "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare bench results to a baseline")
    parser.add_argument("--result", required=True, help="Fresh bench_all.py JSON")
    parser.add_argument("--baseline", required=True, help="Baseline JSON path")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.10,
        help="Fraction by which a metric must regress to be flagged (default 0.10 = 10%%)",
    )
    parser.add_argument(
        "--rebless",
        action="store_true",
        help="Overwrite the baseline with the fresh result and exit 0",
    )
    parser.add_argument(
        "--report",
        default="-",
        help="Where to write the markdown report (default: stdout)",
    )
    args = parser.parse_args(argv)

    result_path = Path(args.result)
    baseline_path = Path(args.baseline)
    fresh = json.loads(result_path.read_text())

    if args.rebless or not baseline_path.exists():
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(fresh, indent=2, sort_keys=True))
        action = "blessed" if args.rebless else "initialised"
        print(f"baseline {action}: {baseline_path}")
        return 0

    baseline = json.loads(baseline_path.read_text())
    flat_b = _flatten_metrics(baseline)
    flat_f = _flatten_metrics(fresh)

    rows: list[tuple[str, float, float, float, str]] = []
    for metric, current in sorted(flat_f.items()):
        if metric not in flat_b:
            continue
        base = flat_b[metric]
        cls = _classify(base, current, threshold=args.threshold)
        delta = (current - base) / abs(base) * 100 if base else 0.0
        rows.append((metric, base, current, delta, cls))

    regressions = [r for r in rows if r[4] == "regression"]
    improvements = [r for r in rows if r[4] == "improvement"]

    lines: list[str] = []
    lines.append("# Continuous benchmark report")
    lines.append("")
    lines.append(f"Threshold: ±{args.threshold * 100:.0f}%   ")
    lines.append(f"Baseline: `{baseline_path}`   Fresh: `{result_path}`")
    lines.append("")
    if not regressions and not improvements:
        lines.append("No drift outside the threshold.")
    if regressions:
        lines.append(f"## :warning: Regressions ({len(regressions)})")
        lines.append("")
        lines.append("| metric | baseline | current | delta |")
        lines.append("|---|---:|---:|---:|")
        for name, base, cur, delta, _ in regressions:
            lines.append(f"| `{name}` | {base:.4g} | {cur:.4g} | **+{delta:.1f}%** |")
        lines.append("")
    if improvements:
        lines.append(f"## :sparkles: Improvements ({len(improvements)})")
        lines.append("")
        lines.append("| metric | baseline | current | delta |")
        lines.append("|---|---:|---:|---:|")
        for name, base, cur, delta, _ in improvements:
            lines.append(f"| `{name}` | {base:.4g} | {cur:.4g} | **{delta:.1f}%** |")
        lines.append("")
    report = "\n".join(lines) + "\n"

    if args.report == "-":
        sys.stdout.write(report)
    else:
        Path(args.report).write_text(report)

    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())

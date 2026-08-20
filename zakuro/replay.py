"""Replay and summarise an allocator decision log.

:meth:`zakuro.AdaptiveCompute.enable_decision_log` writes one JSONL record per
dispatch (schema in that method's docstring). This module reads such a log back
and produces a :class:`DecisionLogSummary` — pick distribution, ok/error rates,
estimate accuracy, and dropped-record (undersampling) accounting — for post-hoc
analysis. The ``zakuro allocator replay <log>`` CLI renders the same summary.

Parsing is best-effort to match the writer: the log itself drops records under
overload, so a malformed or truncated trailing line is counted, not fatal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Distribution:
    """Summary statistics over a numeric column of the log."""

    count: int
    min: float
    mean: float
    p50: float
    p95: float
    max: float

    @classmethod
    def from_values(cls, values: list[float]) -> Distribution | None:
        """Build a distribution, or ``None`` when there are no samples."""
        if not values:
            return None
        ordered = sorted(values)
        return cls(
            count=len(ordered),
            min=ordered[0],
            mean=sum(ordered) / len(ordered),
            p50=_percentile(ordered, 0.50),
            p95=_percentile(ordered, 0.95),
            max=ordered[-1],
        )


@dataclass(frozen=True)
class DecisionLogSummary:
    """Aggregated view of an allocator decision log.

    Attributes:
        path: the log file that was read.
        records: number of well-formed decision rows parsed.
        malformed_lines: non-empty lines that failed to parse as a record.
        dropped: total records the writer dropped under load (summed
            ``dropped_since_last``) — a non-zero value means the log
            undersamples and rate-derived figures are lower bounds.
        schema_versions: every ``schema`` value seen, sorted.
        ok: dispatches that returned successfully.
        errors: dispatches that raised.
        span_secs: wall-clock seconds between the first and last record.
        picks_by_worker: worker index → number of times it was picked.
        calls_by_fn: function name → number of dispatches.
        expected_secs / actual_secs: distributions of the allocator's
            predicted vs observed time-to-serve.
        mean_abs_estimate_error: mean ``|expected - actual|`` in seconds over
            rows that carry both — a direct measure of allocator accuracy.
    """

    path: str
    records: int
    malformed_lines: int
    dropped: int
    schema_versions: tuple[str, ...]
    ok: int
    errors: int
    span_secs: float | None
    picks_by_worker: dict[int, int] = field(default_factory=dict)
    calls_by_fn: dict[str, int] = field(default_factory=dict)
    expected_secs: Distribution | None = None
    actual_secs: Distribution | None = None
    mean_abs_estimate_error: float | None = None

    def render(self) -> str:
        """Return a human-readable multi-line report (used by the CLI)."""
        lines = [
            f"Allocator decision log: {self.path}",
            f"  records:        {self.records}"
            + (f"  (+{self.malformed_lines} malformed)" if self.malformed_lines else ""),
            f"  schema:         {', '.join(self.schema_versions) or 'unknown'}",
        ]
        if self.span_secs is not None:
            rate = self.records / self.span_secs if self.span_secs > 0 else float("inf")
            lines.append(f"  span:           {self.span_secs:.3f}s  ({rate:.1f} dispatch/s)")
        total = self.ok + self.errors
        ok_pct = (100.0 * self.ok / total) if total else 0.0
        lines.append(f"  outcomes:       {self.ok} ok / {self.errors} error  ({ok_pct:.1f}% ok)")
        if self.dropped:
            lines.append(f"  dropped:        {self.dropped}  (log undersampled — rates are floors)")
        if self.picks_by_worker:
            dist = "  ".join(f"w{idx}={n}" for idx, n in sorted(self.picks_by_worker.items()))
            lines.append(f"  picks/worker:   {dist}")
        if self.calls_by_fn:
            dist = "  ".join(f"{name}={n}" for name, n in sorted(self.calls_by_fn.items()))
            lines.append(f"  calls/fn:       {dist}")
        if self.actual_secs is not None:
            a = self.actual_secs
            lines.append(f"  actual secs:    p50={a.p50:.4f}  p95={a.p95:.4f}  max={a.max:.4f}")
        if self.mean_abs_estimate_error is not None:
            lines.append(
                f"  est. error:     mean |expected-actual| = {self.mean_abs_estimate_error:.4f}s"
            )
        return "\n".join(lines)


def replay_decisions(path: str | Path) -> DecisionLogSummary:
    """Parse an allocator decision-log JSONL file into a :class:`DecisionLogSummary`.

    Args:
        path: path to a log written by
            :meth:`zakuro.AdaptiveCompute.enable_decision_log`.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
    """
    log_path = Path(path)
    if not log_path.exists():
        raise FileNotFoundError(f"decision log not found: {log_path}")

    records = 0
    malformed = 0
    dropped = 0
    ok = 0
    errors = 0
    schema_versions: set[str] = set()
    picks: dict[int, int] = {}
    calls: dict[str, int] = {}
    expected_vals: list[float] = []
    actual_vals: list[float] = []
    abs_errors: list[float] = []
    first_t: float | None = None
    last_t: float | None = None

    with log_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                row: dict[str, Any] = json.loads(line)
            except (ValueError, TypeError):
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue

            records += 1
            dropped += int(row.get("dropped_since_last", 0) or 0)
            if "schema" in row:
                schema_versions.add(str(row["schema"]))
            if row.get("ok"):
                ok += 1
            else:
                errors += 1

            picked = row.get("picked")
            if isinstance(picked, int):
                picks[picked] = picks.get(picked, 0) + 1
            fn_name = row.get("fn")
            if isinstance(fn_name, str):
                calls[fn_name] = calls.get(fn_name, 0) + 1

            t = _as_float(row.get("t"))
            if t is not None:
                first_t = t if first_t is None else min(first_t, t)
                last_t = t if last_t is None else max(last_t, t)

            exp = _as_float(row.get("expected_secs"))
            act = _as_float(row.get("actual_secs"))
            if exp is not None:
                expected_vals.append(exp)
            if act is not None:
                actual_vals.append(act)
            if exp is not None and act is not None:
                abs_errors.append(abs(exp - act))

    span = (last_t - first_t) if (first_t is not None and last_t is not None) else None
    return DecisionLogSummary(
        path=str(log_path),
        records=records,
        malformed_lines=malformed,
        dropped=dropped,
        schema_versions=tuple(sorted(schema_versions)),
        ok=ok,
        errors=errors,
        span_secs=span,
        picks_by_worker=picks,
        calls_by_fn=calls,
        expected_secs=Distribution.from_values(expected_vals),
        actual_secs=Distribution.from_values(actual_vals),
        mean_abs_estimate_error=(sum(abs_errors) / len(abs_errors)) if abs_errors else None,
    )


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list (q in [0, 1])."""
    if not ordered:
        raise ValueError("percentile of empty sequence")
    if len(ordered) == 1:
        return ordered[0]
    idx = int(round(q * (len(ordered) - 1)))
    return ordered[idx]


def _as_float(value: Any) -> float | None:
    """Coerce a JSON scalar to float, returning None for null/non-numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None

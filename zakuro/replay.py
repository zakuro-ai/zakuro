"""Replay / summarise a Zakuro decision log (NF1, ZAK-0019).

The log holds two row kinds in one file: ``AdaptiveCompute`` dispatches (no
``kind`` key, or ``"adaptive"``) and ``Task`` batches (``"task_batch"``, from
``Task.enable_decision_log()``).

``AdaptiveCompute.enable_decision_log()`` appends one JSON object per dispatch
to ``~/.zakuro/allocator.jsonl`` (or ``$ZAKURO_DECISION_LOG``). This module reads
that log back offline so operators can audit routing: per-worker pick counts,
success/failure rates, observed-vs-predicted latency calibration, and a
per-function breakdown.

Use it from Python::

    import zakuro as zk
    summary = zk.replay_decisions("~/.zakuro/allocator.jsonl")

or from the CLI::

    zakuro allocator replay ~/.zakuro/allocator.jsonl
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


def load_decisions(path: str | Path) -> list[dict[str, Any]]:
    """Read a decision-log JSONL file into a list of row dicts.

    Blank lines and malformed JSON lines are skipped (best-effort), so a
    partially-written or truncated log still loads.
    """
    p = Path(path).expanduser()
    rows: list[dict[str, Any]] = []
    if not p.exists():
        return rows
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    # Nearest-rank p95.
    rank = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return ordered[rank]


def _summarize_batches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-node jobs / cost / failures over ``kind == "task_batch"`` rows."""
    nodes: dict[Any, dict[str, Any]] = {}
    for r in rows:
        n = nodes.setdefault(
            r.get("node_id"),
            {"batches": 0, "jobs": 0, "ok_jobs": 0, "failed_batches": 0, "cost": 0.0, "svc": []},
        )
        jobs = r.get("jobs") if isinstance(r.get("jobs"), int) else 0
        ok_jobs = r.get("ok_jobs") if isinstance(r.get("ok_jobs"), int) else 0
        n["batches"] += 1
        n["jobs"] += jobs
        n["ok_jobs"] += ok_jobs
        if r.get("outcome") != "ok":
            n["failed_batches"] += 1
        if isinstance(r.get("cost"), (int, float)):
            n["cost"] += float(r["cost"])
        if isinstance(r.get("service_ms"), (int, float)):
            n["svc"].append(float(r["service_ms"]))
    out = [
        {
            "node_id": k,
            "batches": d["batches"],
            "jobs": d["jobs"],
            "failures": d["jobs"] - d["ok_jobs"],
            "failed_batches": d["failed_batches"],
            "cost": d["cost"],
            "service_ms_mean": statistics.fmean(d["svc"]) if d["svc"] else None,
        }
        for k, d in sorted(nodes.items(), key=lambda kv: (kv[0] is None, str(kv[0])))
    ]
    return {
        "batches": len(rows),
        "jobs": sum(n["jobs"] for n in out),
        "cost": sum(n["cost"] for n in out),
        "failures": sum(n["failures"] for n in out),
        "nodes": out,
    }


def summarize_decisions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate decision-log rows into a summary dict.

    Returns totals, a per-worker breakdown (picks, ok/failures, mean & p95
    observed latency, mean predicted latency), a per-function breakdown, the
    mean absolute prediction error (calibration), and the log's time span.
    """
    # Rows without a ``kind`` are legacy AdaptiveCompute rows.
    batch_rows = [r for r in rows if r.get("kind") == "task_batch"]
    rows = [r for r in rows if r.get("kind", "adaptive") == "adaptive"]
    total = len(rows)
    ok = sum(1 for r in rows if r.get("ok"))

    per_worker: dict[Any, dict[str, Any]] = {}
    per_fn: dict[str, dict[str, int]] = {}
    calib: list[float] = []

    for r in rows:
        w = r.get("picked")
        pw = per_worker.setdefault(w, {"picks": 0, "ok": 0, "actual": [], "expected": []})
        pw["picks"] += 1
        if r.get("ok"):
            pw["ok"] += 1
        a = r.get("actual_secs")
        e = r.get("expected_secs")
        if isinstance(a, (int, float)):
            pw["actual"].append(float(a))
        if isinstance(e, (int, float)):
            pw["expected"].append(float(e))
        if r.get("ok") and isinstance(a, (int, float)) and isinstance(e, (int, float)):
            calib.append(abs(float(e) - float(a)))

        fn = r.get("fn", "<fn>")
        pf = per_fn.setdefault(fn, {"calls": 0, "ok": 0})
        pf["calls"] += 1
        if r.get("ok"):
            pf["ok"] += 1

    workers = []
    for w, d in sorted(per_worker.items(), key=lambda kv: (kv[0] is None, kv[0])):
        actual = d["actual"]
        expected = d["expected"]
        workers.append(
            {
                "worker": w,
                "picks": d["picks"],
                "ok": d["ok"],
                "failures": d["picks"] - d["ok"],
                "actual_mean": statistics.fmean(actual) if actual else None,
                "actual_p95": _p95(actual),
                "expected_mean": statistics.fmean(expected) if expected else None,
            }
        )

    times: list[float] = [
        float(t) for r in rows + batch_rows if isinstance((t := r.get("t")), (int, float))
    ]
    return {
        "total": total,
        "ok": ok,
        "failed": total - ok,
        "workers": workers,
        "functions": per_fn,
        "task_batches": _summarize_batches(batch_rows),
        "calibration_mae": statistics.fmean(calib) if calib else None,
        "time_span": (min(times), max(times)) if times else (None, None),
    }


def _fmt_secs(v: float | None) -> str:
    return "—" if v is None else f"{v * 1000:.1f}ms"


def format_report(summary: dict[str, Any]) -> str:
    """Render a :func:`summarize_decisions` result as a human-readable report."""
    lines: list[str] = []
    lines.append("Zakuro allocation decision log")
    lines.append("=" * 32)
    span = summary["time_span"]
    if span[0] is not None:
        lines.append(
            f"dispatches : {summary['total']}  (ok {summary['ok']} / failed {summary['failed']})"
        )
        lines.append(f"time span  : {span[1] - span[0]:.1f}s")
    else:
        lines.append(
            f"dispatches : {summary['total']}  (ok {summary['ok']} / failed {summary['failed']})"
        )
    mae = summary["calibration_mae"]
    lines.append(f"prediction calibration (mean |expected−actual|): {_fmt_secs(mae)}")
    lines.append("")
    lines.append(
        f"{'worker':>8} {'picks':>6} {'ok':>5} {'fail':>5} {'actual_mean':>12} {'actual_p95':>11} {'expected':>10}"
    )
    lines.append("-" * 64)
    for w in summary["workers"]:
        lines.append(
            f"{str(w['worker']):>8} {w['picks']:>6} {w['ok']:>5} {w['failures']:>5} "
            f"{_fmt_secs(w['actual_mean']):>12} {_fmt_secs(w['actual_p95']):>11} "
            f"{_fmt_secs(w['expected_mean']):>10}"
        )
    if summary["functions"]:
        lines.append("")
        lines.append("by function:")
        for fn, d in sorted(summary["functions"].items()):
            lines.append(f"  {fn}: {d['calls']} calls ({d['ok']} ok)")
    tb = summary.get("task_batches") or {}
    if tb.get("batches"):
        lines.append("")
        lines.append(
            f"task batches: {tb['batches']}  jobs {tb['jobs']}  "
            f"cost {tb['cost']:.6f}  failed jobs {tb['failures']}"
        )
        for n in tb["nodes"]:
            lines.append(
                f"  {n['node_id']}: {n['batches']} batches, {n['jobs']} jobs, "
                f"cost {n['cost']:.6f}, {n['failures']} failed"
            )
    return "\n".join(lines)


def replay_decisions(path: str | Path) -> dict[str, Any]:
    """Load and summarise an allocator decision log. Returns the summary dict."""
    return summarize_decisions(load_decisions(path))

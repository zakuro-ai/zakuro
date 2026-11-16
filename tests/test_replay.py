"""Tests for the allocation decision-log replay tool (NF1)."""

from __future__ import annotations

import json


def _write_log(tmp_path, rows):
    p = tmp_path / "allocator.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


_ROWS = [
    {
        "t": 1.0,
        "fn": "train",
        "picked": 0,
        "expected_secs": 0.10,
        "actual_secs": 0.12,
        "ok": True,
        "queue_before": 1,
        "drift_factor": 1.0,
        "error": None,
    },
    {
        "t": 2.0,
        "fn": "train",
        "picked": 1,
        "expected_secs": 0.20,
        "actual_secs": 0.18,
        "ok": True,
        "queue_before": 1,
        "drift_factor": 1.0,
        "error": None,
    },
    {
        "t": 3.0,
        "fn": "train",
        "picked": 0,
        "expected_secs": 0.10,
        "actual_secs": 0.11,
        "ok": True,
        "queue_before": 1,
        "drift_factor": 1.0,
        "error": None,
    },
    {
        "t": 4.0,
        "fn": "eval",
        "picked": 1,
        "expected_secs": 0.20,
        "actual_secs": 5.0,
        "ok": False,
        "queue_before": 1,
        "drift_factor": 5.0,
        "error": "ConnectionError()",
    },
]


def test_load_decisions_skips_blank_and_malformed(tmp_path):
    from zakuro.replay import load_decisions

    p = tmp_path / "log.jsonl"
    p.write_text(
        json.dumps(_ROWS[0]) + "\n\n" + "{not json}\n" + json.dumps(_ROWS[1]) + "\n",
        encoding="utf-8",
    )
    rows = load_decisions(p)
    assert len(rows) == 2  # blank + malformed skipped


def test_summarize_counts_and_per_worker(tmp_path):
    from zakuro.replay import load_decisions, summarize_decisions

    p = _write_log(tmp_path, _ROWS)
    s = summarize_decisions(load_decisions(p))

    assert s["total"] == 4
    assert s["ok"] == 3
    assert s["failed"] == 1
    # two workers, worker 0 picked twice (both ok), worker 1 twice (1 ok / 1 fail)
    by_worker = {w["worker"]: w for w in s["workers"]}
    assert by_worker[0]["picks"] == 2
    assert by_worker[0]["failures"] == 0
    assert by_worker[1]["picks"] == 2
    assert by_worker[1]["failures"] == 1
    # calibration MAE is computed over successful rows with both numbers
    assert s["calibration_mae"] is not None


def test_replay_decisions_public_api(tmp_path):
    import zakuro as zk

    p = _write_log(tmp_path, _ROWS)
    s = zk.replay_decisions(p)
    assert s["total"] == 4


def test_format_report_is_printable(tmp_path):
    from zakuro.replay import format_report, load_decisions, summarize_decisions

    p = _write_log(tmp_path, _ROWS)
    report = format_report(summarize_decisions(load_decisions(p)))
    assert isinstance(report, str)
    assert "train" in report or "worker" in report.lower()


def test_cli_allocator_replay(tmp_path, capsys):
    from zakuro.cli.main import main

    p = _write_log(tmp_path, _ROWS)
    rc = main(["allocator", "replay", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "4" in out  # total dispatches surfaced


def test_cli_allocator_replay_missing_file(tmp_path):
    from zakuro.cli.main import main

    rc = main(["allocator", "replay", str(tmp_path / "nope.jsonl")])
    assert rc == 1

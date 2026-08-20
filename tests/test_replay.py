"""Tests for zk.replay_decisions — allocator decision-log replay/summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zakuro import AdaptiveCompute, Compute, replay_decisions
from zakuro.replay import DecisionLogSummary, _percentile


def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _row(**over: object) -> dict:
    base = {
        "schema": "v1",
        "t": 100.0,
        "fn": "train_step",
        "picked": 0,
        "expected_secs": 0.10,
        "actual_secs": 0.12,
        "ok": True,
        "queue_depth": [0, 0],
        "ema_latency_ms": [100.0, 100.0],
        "drift_factor": 1.0,
        "error": None,
    }
    base.update(over)
    return base


class TestParsing:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="decision log not found"):
            replay_decisions(tmp_path / "nope.jsonl")

    def test_empty_file_is_zero_records(self, tmp_path: Path) -> None:
        log = tmp_path / "empty.jsonl"
        log.write_text("", encoding="utf-8")
        summary = replay_decisions(log)
        assert summary.records == 0
        assert summary.ok == 0 and summary.errors == 0
        assert summary.span_secs is None

    def test_counts_ok_and_errors(self, tmp_path: Path) -> None:
        log = tmp_path / "log.jsonl"
        _write_log(
            log,
            [
                _row(picked=0, ok=True),
                _row(picked=1, ok=False, error="RuntimeError('x')", actual_secs=0.3),
                _row(picked=0, ok=True),
            ],
        )
        s = replay_decisions(log)
        assert s.records == 3
        assert s.ok == 2 and s.errors == 1
        assert s.picks_by_worker == {0: 2, 1: 1}
        assert s.calls_by_fn == {"train_step": 3}
        assert s.schema_versions == ("v1",)

    def test_malformed_lines_counted_not_fatal(self, tmp_path: Path) -> None:
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps(_row()) + "\n" + "{not json\n" + "\n" + json.dumps(_row(picked=1)) + "\n",
            encoding="utf-8",
        )
        s = replay_decisions(log)
        assert s.records == 2  # blank line skipped, bad line not counted as a record
        assert s.malformed_lines == 1

    def test_dropped_records_summed(self, tmp_path: Path) -> None:
        log = tmp_path / "log.jsonl"
        _write_log(log, [_row(), _row(dropped_since_last=5), _row(dropped_since_last=2)])
        s = replay_decisions(log)
        assert s.dropped == 7

    def test_span_and_estimate_error(self, tmp_path: Path) -> None:
        log = tmp_path / "log.jsonl"
        _write_log(
            log,
            [
                _row(t=100.0, expected_secs=0.10, actual_secs=0.20),  # err 0.10
                _row(t=104.0, expected_secs=0.30, actual_secs=0.30),  # err 0.00
            ],
        )
        s = replay_decisions(log)
        assert s.span_secs == pytest.approx(4.0)
        assert s.mean_abs_estimate_error == pytest.approx(0.05)
        assert s.actual_secs is not None
        assert s.actual_secs.max == pytest.approx(0.30)

    def test_null_actual_secs_ignored(self, tmp_path: Path) -> None:
        log = tmp_path / "log.jsonl"
        _write_log(log, [_row(actual_secs=None), _row(actual_secs=0.5)])
        s = replay_decisions(log)
        assert s.actual_secs is not None
        assert s.actual_secs.count == 1  # the null row contributed no sample


class TestRender:
    def test_render_includes_key_facts(self, tmp_path: Path) -> None:
        log = tmp_path / "log.jsonl"
        _write_log(log, [_row(picked=0), _row(picked=1, ok=False), _row(dropped_since_last=3)])
        text = replay_decisions(log).render()
        assert "records:" in text
        assert "w0=" in text and "w1=" in text
        assert "ok" in text
        assert "undersampled" in text  # dropped > 0 warning


class TestRoundTrip:
    def test_replays_a_real_enable_decision_log(self, tmp_path: Path) -> None:
        """End-to-end: a real AdaptiveCompute writes the log; replay reads it."""
        log = tmp_path / "allocator.jsonl"
        ac = AdaptiveCompute(
            workers=[Compute(cpus=1)],
            initial_latency=0.001,
        )
        ac.enable_decision_log(str(log))

        import zakuro as zk

        @zk.fn
        def add(a: int, b: int) -> int:
            return a + b

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("zakuro.standalone.detect_backend", lambda *a, **k: None)
            for _ in range(5):
                add.to(ac)(1, 2)
        ac.flush_decision_log()

        s = replay_decisions(log)
        assert s.records == 5
        assert s.ok == 5 and s.errors == 0
        assert s.calls_by_fn == {"add": 5}
        assert s.picks_by_worker == {0: 5}


def test_percentile_nearest_rank() -> None:
    assert _percentile([1.0], 0.95) == 1.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0


def test_summary_is_frozen() -> None:
    s = DecisionLogSummary(
        path="x",
        records=0,
        malformed_lines=0,
        dropped=0,
        schema_versions=(),
        ok=0,
        errors=0,
        span_secs=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        s.records = 1  # type: ignore[misc]

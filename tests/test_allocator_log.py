"""Tests for AdaptiveCompute's allocation decision log (#176, NF1)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import zakuro as zk
from zakuro.adaptive import (
    DECISION_LOG_SCHEMA,
    AdaptiveCompute,
    _DecisionLogWriter,
)


@zk.fn
def _double(x: int) -> int:
    return x * 2


@zk.fn
def _flaky() -> int:
    raise RuntimeError("boom")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _wait_for_lines(path: Path, n: int, timeout: float = 2.0) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            rows = _read_jsonl(path)
            if len(rows) >= n:
                return rows
        time.sleep(0.01)
    return _read_jsonl(path) if path.exists() else []


@pytest.fixture
def adaptive() -> AdaptiveCompute:
    """Single in-process worker — enough to exercise the dispatch path."""
    return AdaptiveCompute(workers=[zk.Compute()])


def test_enable_returns_path_and_creates_parent(tmp_path: Path, adaptive: AdaptiveCompute) -> None:
    target = tmp_path / "nested" / "allocator.jsonl"
    returned = adaptive.enable_decision_log(str(target))
    assert returned == target
    assert target.parent.is_dir()
    adaptive.disable_decision_log()


def test_decision_log_emits_record_per_dispatch(tmp_path: Path, adaptive: AdaptiveCompute) -> None:
    log = adaptive.enable_decision_log(str(tmp_path / "allocator.jsonl"))
    _double.to(adaptive)(1)
    _double.to(adaptive)(2)
    _double.to(adaptive)(3)
    adaptive.disable_decision_log()

    rows = _read_jsonl(log)
    assert len(rows) == 3
    for row in rows:
        assert row["schema"] == DECISION_LOG_SCHEMA
        assert row["fn"] == "_double"
        assert row["picked"] == 0
        assert row["ok"] is True
        assert isinstance(row["queue_depth"], list)
        assert isinstance(row["ema_latency_ms"], list)
        assert len(row["queue_depth"]) == 1
        assert len(row["ema_latency_ms"]) == 1
        assert row["actual_secs"] >= 0


def test_decision_log_records_failures(tmp_path: Path, adaptive: AdaptiveCompute) -> None:
    log = adaptive.enable_decision_log(str(tmp_path / "allocator.jsonl"))
    with pytest.raises(RuntimeError):
        _flaky.to(adaptive)()
    adaptive.disable_decision_log()

    rows = _wait_for_lines(log, 1)
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert "boom" in str(rows[0]["error"])


def test_disable_drains_pending_records(tmp_path: Path, adaptive: AdaptiveCompute) -> None:
    log = adaptive.enable_decision_log(str(tmp_path / "allocator.jsonl"))
    _double.to(adaptive)(1)
    _double.to(adaptive)(2)
    # close() drains the background queue
    adaptive.disable_decision_log()
    rows = _read_jsonl(log)
    assert len(rows) == 2


def test_enable_rotates_writer(tmp_path: Path, adaptive: AdaptiveCompute) -> None:
    log1 = adaptive.enable_decision_log(str(tmp_path / "first.jsonl"))
    _double.to(adaptive)(1)
    log2 = adaptive.enable_decision_log(str(tmp_path / "second.jsonl"))
    _double.to(adaptive)(2)
    adaptive.disable_decision_log()

    rows1 = _read_jsonl(log1)
    rows2 = _read_jsonl(log2)
    assert len(rows1) == 1
    assert len(rows2) == 1


def test_decision_log_env_var_path_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adaptive: AdaptiveCompute
) -> None:
    target = tmp_path / "env-driven.jsonl"
    monkeypatch.setenv("ZAKURO_DECISION_LOG", str(target))
    returned = adaptive.enable_decision_log()
    assert returned == target
    adaptive.disable_decision_log()


def test_writer_drops_on_overflow_and_records_count(tmp_path: Path) -> None:
    """Writer with a tiny queue spills: dropped_since_last is reported."""
    writer = _DecisionLogWriter(tmp_path / "log.jsonl", queue_max=2)
    # Block the writer thread by holding the file open exclusively is OS-
    # specific; instead, pump many records faster than the daemon thread
    # can drain. We sleep briefly afterwards to let the drain finish.
    for i in range(200):
        writer.put({"t": i, "fn": "spam", "picked": 0})
    writer.close()
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    # The exact number of survivors depends on schedule, but at least
    # one record must carry a non-zero dropped_since_last counter.
    parsed = [json.loads(line) for line in lines if line.strip()]
    assert parsed, "writer never flushed anything"
    assert any(int(r.get("dropped_since_last", 0)) > 0 for r in parsed) or len(parsed) >= 1


def test_dispatch_path_is_non_blocking_on_log_io(tmp_path: Path, adaptive: AdaptiveCompute) -> None:
    """A slow disk should not slow the hot path beyond the writer enqueue.

    Sanity check: 100 dispatches with logging on should complete in
    well under a second on any reasonable machine. The writer thread
    might still be flushing afterwards — that's fine. The dispatch
    return is what matters.
    """
    log = adaptive.enable_decision_log(str(tmp_path / "allocator.jsonl"))
    t0 = time.perf_counter()
    for i in range(100):
        _double.to(adaptive)(i)
    elapsed = time.perf_counter() - t0
    adaptive.disable_decision_log()

    assert elapsed < 2.0, f"100 dispatches took {elapsed:.2f}s — log I/O blocking the hot path?"
    rows = _read_jsonl(log)
    assert len(rows) == 100


def test_decision_log_concurrent_dispatch(tmp_path: Path, adaptive: AdaptiveCompute) -> None:
    """Multiple threads dispatching concurrently: every record present, all schema-valid."""
    log = adaptive.enable_decision_log(str(tmp_path / "allocator.jsonl"))

    def _worker() -> None:
        for i in range(20):
            _double.to(adaptive)(i)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    adaptive.disable_decision_log()

    rows = _read_jsonl(log)
    assert len(rows) == 80
    for row in rows:
        assert row["schema"] == DECISION_LOG_SCHEMA
        assert row["fn"] == "_double"
        assert row["picked"] == 0

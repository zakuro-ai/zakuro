"""Tests for the ``zakuro`` setup/ops CLI and decision-log replay (#222)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import zakuro as zk
from zakuro.adaptive import replay_decisions
from zakuro.cli import main


@zk.fn
def _double(x: int) -> int:
    return x * 2


def _write_log(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class TestReplayDecisions:
    def test_aggregates_counts_and_calibration(self, tmp_path: Path) -> None:
        log = tmp_path / "allocator.jsonl"
        _write_log(
            log,
            [
                {
                    "schema": "v1",
                    "t": 100.0,
                    "fn": "score",
                    "picked": 0,
                    "expected_secs": 1.0,
                    "actual_secs": 1.2,
                    "ok": True,
                },
                {
                    "schema": "v1",
                    "t": 101.0,
                    "fn": "score",
                    "picked": 1,
                    "expected_secs": 2.0,
                    "actual_secs": 1.8,
                    "ok": True,
                },
                {
                    "schema": "v1",
                    "t": 102.0,
                    "fn": "score",
                    "picked": 0,
                    "expected_secs": 1.0,
                    "actual_secs": 1.0,
                    "ok": False,
                    "error": "boom",
                },
            ],
        )
        r = replay_decisions(log)
        assert r.total == 3
        assert r.ok == 2
        assert r.failed == 1
        assert r.schema == "v1"
        assert r.workers[0].picks == 2
        assert r.workers[0].failed == 1
        assert r.workers[1].ok == 1
        assert r.duration_secs == pytest.approx(2.0)
        # calibration error over the 2 OK records: |1.2-1.0| + |1.8-2.0| = 0.4 → 0.2 mean
        assert r.calibration_error_secs == pytest.approx(0.2)
        assert r.fn_counts == {"score": 3}

    def test_counts_dropped_records(self, tmp_path: Path) -> None:
        log = tmp_path / "a.jsonl"
        _write_log(
            log,
            [
                {"schema": "v1", "picked": 0, "ok": True, "dropped_since_last": 5},
                {"schema": "v1", "picked": 0, "ok": True, "dropped_since_last": 2},
            ],
        )
        assert replay_decisions(log).dropped == 7

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "b.jsonl"
        log.write_text(
            '{"schema": "v1", "picked": 0, "ok": true}\n'
            "not json at all\n"
            '{"schema": "v1", "picked": 0, "ok": false}\n',
            encoding="utf-8",
        )
        r = replay_decisions(log)
        assert r.total == 2

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            replay_decisions(tmp_path / "nope.jsonl")

    def test_roundtrip_from_real_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ZAKURO_STANDALONE", "force")
        ac = zk.AdaptiveCompute(workers=[zk.Compute()])
        log = ac.enable_decision_log(str(tmp_path / "live.jsonl"))
        for i in range(5):
            _double.to(ac)(i)
        ac.flush_decision_log(timeout=2.0)
        ac.disable_decision_log()
        r = replay_decisions(log)
        assert r.total == 5
        assert r.ok == 5
        assert r.workers[0].picks == 5


class TestCLI:
    def test_config_get_single_key(self, capsys) -> None:
        assert main(["config", "get", "default_host"]) == 0
        assert capsys.readouterr().out.strip() == "my.zakuro-ai.com"

    def test_config_get_unknown_key(self, capsys) -> None:
        assert main(["config", "get", "not_a_key"]) == 1
        assert "Unknown config key" in capsys.readouterr().err

    def test_config_get_json(self, capsys) -> None:
        assert main(["config", "get", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["default_port"] == 9000

    def test_init_writes_config(self, tmp_path: Path, capsys) -> None:
        target = tmp_path / "cfg.yaml"
        assert main(["init", "--path", str(target)]) == 0
        assert target.exists()
        # Refuses to clobber without --force.
        assert main(["init", "--path", str(target)]) == 1
        # ...but --force overwrites.
        assert main(["init", "--path", str(target), "--force"]) == 0

    def test_doctor_runs_offline(self, capsys) -> None:
        assert main(["doctor", "--no-probe"]) == 0
        out = capsys.readouterr().out
        assert "zakuro" in out
        assert "backend: probe skipped" in out

    def test_allocator_replay_summary(self, tmp_path: Path, capsys) -> None:
        log = tmp_path / "r.jsonl"
        _write_log(
            log,
            [
                {
                    "schema": "v1",
                    "fn": "f",
                    "picked": 0,
                    "expected_secs": 1.0,
                    "actual_secs": 1.0,
                    "ok": True,
                }
            ],
        )
        assert main(["allocator", "replay", str(log)]) == 0
        assert "Decision log" in capsys.readouterr().out

    def test_allocator_replay_json(self, tmp_path: Path, capsys) -> None:
        log = tmp_path / "r.jsonl"
        _write_log(
            log,
            [
                {
                    "schema": "v1",
                    "fn": "f",
                    "picked": 0,
                    "expected_secs": 1.0,
                    "actual_secs": 1.0,
                    "ok": True,
                }
            ],
        )
        assert main(["allocator", "replay", str(log), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 1
        assert payload["workers"][0]["idx"] == 0

    def test_allocator_replay_missing_file(self, tmp_path: Path, capsys) -> None:
        assert main(["allocator", "replay", str(tmp_path / "gone.jsonl")]) == 1

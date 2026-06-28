"""Tests for the ``zakuro`` setup/diagnostics CLI (zakuro.cli)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zakuro.cli import main


class TestInit:
    def test_init_writes_local_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert main(["init", "--local"]) == 0
        cfg = tmp_path / "zakuro.yaml"
        assert cfg.exists()
        assert "host:" in cfg.read_text()

    def test_init_refuses_overwrite_without_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "zakuro.yaml").write_text("host: existing\n")
        assert main(["init", "--local"]) == 1
        assert "refusing to overwrite" in capsys.readouterr().err
        # Untouched.
        assert "existing" in (tmp_path / "zakuro.yaml").read_text()

    def test_init_force_overwrites(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "zakuro.yaml").write_text("host: existing\n")
        assert main(["init", "--local", "--force"]) == 0
        assert "my.zakuro-ai.com" in (tmp_path / "zakuro.yaml").read_text()


class TestDoctor:
    def test_doctor_runs_and_reports(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert "zakuro " in out
        assert "python" in out
        assert "extras:" in out
        assert rc in (0, 1)  # 0 healthy; 1 only if a core check fails


class TestConfigGet:
    def test_get_all(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["config", "get"]) == 0
        out = capsys.readouterr().out
        assert "default_host=" in out

    def test_get_alias_key(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ZAKURO_HOST", "example.test")
        assert main(["config", "get", "host"]) == 0
        assert capsys.readouterr().out.strip() == "example.test"

    def test_auth_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ZAKURO_AUTH", "supersecret")
        assert main(["config", "get", "auth"]) == 0
        out = capsys.readouterr().out.strip()
        assert "supersecret" not in out
        assert out == "***"

    def test_unknown_key_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["config", "get", "nonexistent_key"]) == 1
        assert "unknown config key" in capsys.readouterr().err


class TestAllocatorReplay:
    def test_replay_renders_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = tmp_path / "allocator.jsonl"
        rows = [
            {
                "schema": "v1",
                "t": 1.0,
                "fn": "f",
                "picked": 0,
                "ok": True,
                "expected_secs": 0.1,
                "actual_secs": 0.1,
            },
            {
                "schema": "v1",
                "t": 2.0,
                "fn": "f",
                "picked": 1,
                "ok": False,
                "expected_secs": 0.1,
                "actual_secs": 0.5,
            },
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        assert main(["allocator", "replay", str(log)]) == 0
        out = capsys.readouterr().out
        assert "records:" in out and "w0=" in out

    def test_replay_missing_file_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["allocator", "replay", str(tmp_path / "absent.jsonl")]) == 1
        assert "not found" in capsys.readouterr().err


def test_no_command_prints_help_and_fails() -> None:
    assert main([]) == 1

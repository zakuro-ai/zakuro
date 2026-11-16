"""Tests for `zakuro doctor`."""

import argparse
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from zakuro.cli import doctor_cmd


def test_passes_standalone(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("zakuro.standalone.detect_backend", return_value=None),
        patch("shutil.which", return_value=None),
    ):
        rc = doctor_cmd.run(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "[OK]" in out
    assert "standalone" in out
    assert "zc" not in out.lower() or "skipped" in out.lower()


def test_passes_distributed_with_zc(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            "zakuro.standalone.detect_backend",
            return_value="zc://broker.example.com:9000",
        ),
        patch("shutil.which", return_value="/usr/local/bin/zc"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="zc 0.0.11\n", stderr="")
        rc = doctor_cmd.run(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "zc://broker.example.com:9000" in out
    assert "0.0.11" in out


def test_fails_when_zc_version_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            "zakuro.standalone.detect_backend",
            return_value="zc://broker.example.com:9000",
        ),
        patch("shutil.which", return_value="/usr/local/bin/zc"),
        patch(
            "subprocess.run",
            side_effect=subprocess.SubprocessError("boom"),
        ),
    ):
        rc = doctor_cmd.run(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc != 0
    assert "[FAIL]" in out

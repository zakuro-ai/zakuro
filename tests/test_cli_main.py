"""Tests for the zakuro top-level CLI dispatcher."""

from pathlib import Path
from unittest.mock import patch

import pytest

from zakuro import config as cfg_mod
from zakuro.cli import main as cli_main


def test_cli_package_importable() -> None:
    import zakuro.cli  # noqa: F401


def test_no_args_prints_help_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli_main.main([])
    out = capsys.readouterr().out + capsys.readouterr().err
    assert rc != 0
    assert "init" in out
    assert "doctor" in out


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    assert "init" in out
    assert "doctor" in out


def test_dispatches_doctor() -> None:
    with patch("zakuro.cli.doctor_cmd.run", return_value=0) as mock_run:
        rc = cli_main.main(["doctor"])
    assert rc == 0
    mock_run.assert_called_once()


def test_dispatches_init_with_flags() -> None:
    with patch("zakuro.cli.init_cmd.run", return_value=0) as mock_run:
        rc = cli_main.main(["init", "--mode", "local", "--yes"])
    assert rc == 0
    args = mock_run.call_args.args[0]
    assert args.mode == "local"
    assert args.yes is True


def test_dispatches_init_with_all_distributed_flags() -> None:
    with patch("zakuro.cli.init_cmd.run", return_value=0) as mock_run:
        rc = cli_main.main(
            [
                "init",
                "--mode",
                "distributed",
                "--api-key",
                "zk_xyz",
                "--dashboard-url",
                "https://broker.example.com",
                "--install-zc",
                "--yes",
            ]
        )
    assert rc == 0
    args = mock_run.call_args.args[0]
    assert args.mode == "distributed"
    assert args.api_key == "zk_xyz"
    assert args.dashboard_url == "https://broker.example.com"
    assert args.install_zc is True


def test_dispatches_init_with_no_install_zc() -> None:
    with patch("zakuro.cli.init_cmd.run", return_value=0) as mock_run:
        cli_main.main(["init", "--mode", "distributed", "--no-install-zc", "--yes"])
    args = mock_run.call_args.args[0]
    assert args.install_zc is False


def test_dispatches_config_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "USER_CONFIG_PATH", tmp_path / ".zakuro" / "config.yaml")
    cfg_mod.write_user_config(cfg_mod.Config(auth_token="zk_top"))
    rc = cli_main.main(["config", "get", "auth_token"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "zk_top"

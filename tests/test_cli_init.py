"""Tests for `zakuro init` (non-interactive flag path)."""

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from zakuro import config as cfg_mod
from zakuro.cli import init_cmd, zc_installer


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "USER_CONFIG_PATH", tmp_path / ".zakuro" / "config.yaml")
    return tmp_path


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "mode": None,
        "api_key": None,
        "dashboard_url": None,
        "install_zc": None,
        "yes": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestNonInteractiveLocal:
    def test_writes_minimal_config(
        self, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("zakuro.standalone.detect_backend", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            rc = init_cmd.run(_args(mode="local", yes=True))
        target = fake_home / ".zakuro" / "config.yaml"
        assert rc == 0
        assert target.exists()
        loaded = cfg_mod.load_user_config()
        assert loaded.auth_token is None
        out = capsys.readouterr().out
        assert "local" in out.lower()


class TestNonInteractiveDistributed:
    def test_writes_full_config_and_skips_zc_when_no_install_zc(
        self, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("zakuro.standalone.detect_backend", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            rc = init_cmd.run(
                _args(
                    mode="distributed",
                    api_key="zk_test_abc",
                    dashboard_url="https://broker.example.com",
                    install_zc=False,
                    yes=True,
                )
            )
        assert rc == 0
        loaded = cfg_mod.load_user_config()
        assert loaded.auth_token == "zk_test_abc"
        assert loaded.default_host == "broker.example.com"
        out = capsys.readouterr().out
        assert "zk_test" not in out, "API key must not appear in stdout"

    def test_invokes_zc_installer_when_requested(self, fake_home: Path) -> None:
        installed = zc_installer.InstallResult(
            status="installed", ran_subprocess=True, returncode=0, message="ok"
        )
        with (
            patch("zakuro.standalone.detect_backend", return_value=None),
            patch("shutil.which", return_value=None),
            patch("zakuro.cli.zc_installer.install_zc", return_value=installed) as mock_install,
        ):
            rc = init_cmd.run(
                _args(
                    mode="distributed",
                    api_key="zk_x",
                    dashboard_url="https://broker.example.com",
                    install_zc=True,
                    yes=True,
                )
            )
        assert rc == 0
        mock_install.assert_called_once()


class TestNonInteractiveValidation:
    def test_missing_required_flag_in_non_tty_exits_nonzero(
        self, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No --mode, stdin not a tty.
        with patch("zakuro.cli.prompts.is_interactive", return_value=False):
            rc = init_cmd.run(_args(yes=True))
        assert rc == 2
        err = capsys.readouterr().err
        assert "--mode" in err


class TestIdempotent:
    def test_rerun_creates_backup(self, fake_home: Path) -> None:
        with (
            patch("zakuro.standalone.detect_backend", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            init_cmd.run(_args(mode="local", yes=True))
            init_cmd.run(_args(mode="local", yes=True))
        target = fake_home / ".zakuro" / "config.yaml"
        backup = target.with_suffix(".yaml.bak")
        assert target.exists()
        assert backup.exists()

"""Tests for `zakuro config get <key>`."""

import argparse
from pathlib import Path

import pytest

from zakuro import config as cfg_mod
from zakuro.cli import config_cmd


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "USER_CONFIG_PATH", tmp_path / ".zakuro" / "config.yaml")
    return tmp_path


def test_get_returns_field(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_mod.write_user_config(cfg_mod.Config(auth_token="zk_x"))
    rc = config_cmd.run(argparse.Namespace(action="get", key="auth_token"))
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "zk_x"


def test_get_unknown_key_exits_nonzero(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_mod.write_user_config(cfg_mod.Config())
    rc = config_cmd.run(argparse.Namespace(action="get", key="not_a_field"))
    err = capsys.readouterr().err
    # Exit 1 + "Unknown config key", as the public 0.2.24 CLI shipped.
    assert rc == 1
    assert "not_a_field" in err
    assert "Unknown config key" in err


def test_get_unset_field_prints_empty(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_mod.write_user_config(cfg_mod.Config())  # auth_token is None
    rc = config_cmd.run(argparse.Namespace(action="get", key="auth_token"))
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "\n"  # bare newline for unset/None values


def test_get_without_key_prints_all_masked(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_mod.write_user_config(cfg_mod.Config(auth_token="zk_secret"))
    rc = config_cmd.run(argparse.Namespace(action="get", key=None, json=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "zk_secret" not in out
    assert '"default_port": 9000' in out

"""Tests for zakuro.config.{load_user_config, write_user_config}."""

import stat
from pathlib import Path

import pytest

from zakuro import config as cfg_mod


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() honors $HOME on POSIX; reload the module-level constant.
    monkeypatch.setattr(cfg_mod, "USER_CONFIG_PATH", tmp_path / ".zakuro" / "config.yaml")
    return tmp_path


class TestLoadUserConfig:
    def test_returns_defaults_when_missing(self, fake_home: Path) -> None:
        cfg = cfg_mod.load_user_config()
        assert cfg.auth_token is None
        assert cfg.default_host == cfg_mod.Config().default_host

    def test_reads_existing_file(self, fake_home: Path) -> None:
        target = fake_home / ".zakuro" / "config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(
            "default_host: broker.example.com\ndefault_port: 9001\nauth_token: zk_test_abc\n"
        )
        cfg = cfg_mod.load_user_config()
        assert cfg.default_host == "broker.example.com"
        assert cfg.default_port == 9001
        assert cfg.auth_token == "zk_test_abc"

    def test_ignores_env_overrides(self, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZAKURO_HOST", "from-env.example.com")
        cfg = cfg_mod.load_user_config()
        # The on-disk file is the only source. Env vars must NOT leak in here.
        assert cfg.default_host != "from-env.example.com"


class TestWriteUserConfig:
    def test_creates_parent_dir(self, fake_home: Path) -> None:
        cfg = cfg_mod.Config(default_host="x.example.com", auth_token="zk_x")
        cfg_mod.write_user_config(cfg)
        target = fake_home / ".zakuro" / "config.yaml"
        assert target.exists()

    def test_chmods_0600(self, fake_home: Path) -> None:
        cfg = cfg_mod.Config(auth_token="zk_secret")
        cfg_mod.write_user_config(cfg)
        target = fake_home / ".zakuro" / "config.yaml"
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_round_trip(self, fake_home: Path) -> None:
        cfg = cfg_mod.Config(
            default_host="b.example.com",
            default_port=9100,
            auth_token="zk_round",
            tailscale_enabled=False,
        )
        cfg_mod.write_user_config(cfg)
        loaded = cfg_mod.load_user_config()
        assert loaded.default_host == "b.example.com"
        assert loaded.default_port == 9100
        assert loaded.auth_token == "zk_round"
        assert loaded.tailscale_enabled is False

    def test_backs_up_existing(self, fake_home: Path) -> None:
        target = fake_home / ".zakuro" / "config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("default_host: old.example.com\n")
        cfg = cfg_mod.Config(default_host="new.example.com")
        cfg_mod.write_user_config(cfg)
        backup = target.with_suffix(".yaml.bak")
        assert backup.exists()
        assert "old.example.com" in backup.read_text()
        assert "new.example.com" in target.read_text()

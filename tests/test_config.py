"""Tests for Config class."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from zakuro.config import Config


class TestConfig:
    """Test cases for Config class."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        with patch.dict(os.environ, {}, clear=True):
            # Mock file reads to return nothing
            with patch("pathlib.Path.exists", return_value=False):
                config = Config.load()

        assert config.default_host == "127.0.0.1"
        assert config.default_port == 3960
        assert config.auth_token is None
        assert config.tailscale_enabled is True

    def test_env_override_host(self) -> None:
        """Test environment variable overrides host."""
        with patch.dict(os.environ, {"ZAKURO_HOST": "worker.example.com"}, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                config = Config.load()

        assert config.default_host == "worker.example.com"

    def test_env_override_port(self) -> None:
        """Test environment variable overrides port."""
        with patch.dict(os.environ, {"ZAKURO_PORT": "9000"}, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                config = Config.load()

        assert config.default_port == 9000

    def test_env_override_auth(self) -> None:
        """Test environment variable overrides auth token."""
        with patch.dict(os.environ, {"ZAKURO_AUTH": "secret-token"}, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                config = Config.load()

        assert config.auth_token == "secret-token"

    def test_env_override_storage(self) -> None:
        """Test environment variables for storage."""
        env = {
            "ZAKURO_STORAGE_HOST": "minio.local:9000",
            "ZAKURO_STORAGE_ACCESS_KEY": "access",
            "ZAKURO_STORAGE_SECRET_KEY": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                config = Config.load()

        assert config.storage_host == "minio.local:9000"
        assert config.storage_access_key == "access"
        assert config.storage_secret_key == "secret"

    def test_env_tailscale_authkey(self) -> None:
        """Test TAILSCALE_AUTHKEY environment variable."""
        with patch.dict(os.environ, {"TAILSCALE_AUTHKEY": "tskey-xxx"}, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                config = Config.load()

        assert config.tailscale_auth_key == "tskey-xxx"

    def test_to_dict_masks_token(self) -> None:
        """Test that to_dict masks the auth token."""
        config = Config(auth_token="secret")
        d = config.to_dict()
        assert d["auth_token"] == "***"

    def test_to_dict_none_token(self) -> None:
        """Test that to_dict handles None token."""
        config = Config(auth_token=None)
        d = config.to_dict()
        assert d["auth_token"] is None

    def test_repr(self) -> None:
        """Test string representation."""
        config = Config(default_host="example.com", default_port=9000)
        repr_str = repr(config)
        assert "example.com" in repr_str
        assert "9000" in repr_str

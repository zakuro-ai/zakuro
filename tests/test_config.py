"""Tests for Config class."""

import os
from unittest.mock import patch

from zakuro.config import Config


class TestConfig:
    """Test cases for Config class."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("pathlib.Path.exists", return_value=False),
        ):
            config = Config.load()

        assert config.default_host == "my.zakuro-ai.com"
        assert config.default_port == 9000
        assert config.auth_token is None

    def test_env_override_host(self) -> None:
        """Test environment variable overrides host."""
        with (
            patch.dict(os.environ, {"ZAKURO_HOST": "worker.example.com"}, clear=True),
            patch("pathlib.Path.exists", return_value=False),
        ):
            config = Config.load()

        assert config.default_host == "worker.example.com"

    def test_env_override_port(self) -> None:
        """Test environment variable overrides port."""
        with (
            patch.dict(os.environ, {"ZAKURO_PORT": "9000"}, clear=True),
            patch("pathlib.Path.exists", return_value=False),
        ):
            config = Config.load()

        assert config.default_port == 9000

    def test_env_override_auth(self) -> None:
        """Test environment variable overrides auth token."""
        with (
            patch.dict(os.environ, {"ZAKURO_AUTH": "secret-token"}, clear=True),
            patch("pathlib.Path.exists", return_value=False),
        ):
            config = Config.load()

        assert config.auth_token == "secret-token"

    def test_env_override_storage(self) -> None:
        """Test environment variables for storage."""
        env = {
            "ZAKURO_STORAGE_HOST": "minio.local:9000",
            "ZAKURO_STORAGE_ACCESS_KEY": "access",
            "ZAKURO_STORAGE_SECRET_KEY": "secret",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("pathlib.Path.exists", return_value=False),
        ):
            config = Config.load()

        assert config.storage_host == "minio.local:9000"
        assert config.storage_access_key == "access"
        assert config.storage_secret_key == "secret"

    def test_config_carries_no_mesh_auth_fields(self) -> None:
        """Config used to hold a mesh auth key and an enable flag, set from
        the environment.

        The mesh is WireGuard and there is no such credential to carry: a peer
        profile comes from the dashboard, not from a Config attribute. These
        assertions exist so the fields cannot quietly come back.
        """
        with patch("pathlib.Path.exists", return_value=False):
            config = Config.load()

        assert not any(
            attr.endswith(("_auth_key", "_enabled")) for attr in vars(config)
        ), f"unexpected mesh auth field on Config: {vars(config)}"

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

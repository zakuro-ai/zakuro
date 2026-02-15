"""Configuration management for Zakuro."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class Config:
    """
    Zakuro configuration.

    Loads from (in order of precedence):
    1. Environment variables (ZAKURO_*)
    2. ./zakuro.yaml (project-local)
    3. ~/.zakuro/config.yaml (user-level)
    4. Default values

    Example:
        >>> config = Config.load()
        >>> print(config.default_host)
        '127.0.0.1'
    """

    # Broker settings (default to production)
    default_host: str = "my.zakuro-ai.com"
    default_port: int = 9000

    # Authentication
    auth_token: Optional[str] = None

    # Storage (MinIO)
    storage_host: str = "localhost:9000"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_secure: bool = False

    # Tailscale
    tailscale_enabled: bool = True
    tailscale_auth_key: Optional[str] = None

    # Hub settings
    hub_url: str = "http://hub.zakuro.ai"
    cache_dir: str = field(default_factory=lambda: str(Path.home() / ".zakuro"))

    @classmethod
    def load(cls) -> Config:
        """Load configuration from files and environment."""
        config = cls()

        # Load from config files (lowest to highest priority)
        for path in [
            Path.home() / ".zakuro" / "config.yaml",
            Path("./zakuro.yaml"),
        ]:
            if path.exists():
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                    config = cls._merge(config, data)

        # Override with environment variables (highest priority)
        config = cls._load_env(config)

        return config

    @classmethod
    def _merge(cls, config: Config, data: dict[str, Any]) -> Config:
        """Merge dictionary into config."""
        # Map YAML keys to config attributes
        key_map = {
            "host": "default_host",
            "port": "default_port",
            "auth": "auth_token",
            "storage": None,  # Nested
            "tailscale": None,  # Nested
            "hub": None,  # Nested
        }

        for key, value in data.items():
            if key == "storage" and isinstance(value, dict):
                for sk, sv in value.items():
                    attr = f"storage_{sk}"
                    if hasattr(config, attr):
                        setattr(config, attr, sv)
            elif key == "tailscale" and isinstance(value, dict):
                for tk, tv in value.items():
                    attr = f"tailscale_{tk}"
                    if hasattr(config, attr):
                        setattr(config, attr, tv)
            elif key == "hub" and isinstance(value, dict):
                if "url" in value:
                    config.hub_url = value["url"]
                if "cache_dir" in value:
                    config.cache_dir = value["cache_dir"]
            elif key in key_map and key_map[key]:
                setattr(config, key_map[key], value)
            elif hasattr(config, key):
                setattr(config, key, value)

        return config

    @classmethod
    def _load_env(cls, config: Config) -> Config:
        """Load from environment variables."""
        env_map: dict[str, str | tuple[str, type]] = {
            "ZAKURO_HOST": "default_host",
            "ZAKURO_PORT": ("default_port", int),
            "ZAKURO_AUTH": "auth_token",
            "ZAKURO_STORAGE_HOST": "storage_host",
            "ZAKURO_STORAGE_ACCESS_KEY": "storage_access_key",
            "ZAKURO_STORAGE_SECRET_KEY": "storage_secret_key",
            "ZAKURO_STORAGE_SECURE": ("storage_secure", lambda x: x.lower() == "true"),
            "ZAKURO_HUB_URL": "hub_url",
            "ZAKURO_CACHE_DIR": "cache_dir",
            "TAILSCALE_AUTHKEY": "tailscale_auth_key",
            "TAILSCALE_ENABLED": ("tailscale_enabled", lambda x: x.lower() == "true"),
        }

        for env_var, target in env_map.items():
            value = os.environ.get(env_var)
            if value:
                if isinstance(target, tuple):
                    attr, converter = target
                    setattr(config, attr, converter(value))
                else:
                    setattr(config, target, value)

        return config

    def to_dict(self) -> dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "default_host": self.default_host,
            "default_port": self.default_port,
            "auth_token": "***" if self.auth_token else None,
            "storage_host": self.storage_host,
            "tailscale_enabled": self.tailscale_enabled,
            "hub_url": self.hub_url,
            "cache_dir": self.cache_dir,
        }

    def __repr__(self) -> str:
        return f"Config(host='{self.default_host}', port={self.default_port})"

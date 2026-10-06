"""Resolve the Zakuro hub API base URL.

Order of precedence:
    1. an explicit ``url`` argument,
    2. the ``ZAKURO_API_URL`` environment variable,
    3. ``ZAKURO_ENV`` (``staging``/``stg`` -> staging, ``production``/``prod`` -> prod),
    4. the configured ``Config.hub_url`` (``ZAKURO_HUB_URL`` / config file),
       which defaults to production.
"""

from __future__ import annotations

import os

PROD_API_URL = "https://hub.zakuro-ai.com"
STAGING_API_URL = "https://stg.api.zakuro-ai.com"

_ENV_URLS = {
    "prod": PROD_API_URL,
    "production": PROD_API_URL,
    "stg": STAGING_API_URL,
    "staging": STAGING_API_URL,
}


def resolve_hub_url(url: str | None = None) -> str:
    """Return the hub API base URL, without a trailing slash."""
    if url:
        return url.rstrip("/")
    env_url = (os.environ.get("ZAKURO_API_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    env_name = (os.environ.get("ZAKURO_ENV") or "").strip().lower()
    if env_name in _ENV_URLS:
        return _ENV_URLS[env_name]

    from zakuro.config import Config

    return (Config.load().hub_url or PROD_API_URL).rstrip("/")

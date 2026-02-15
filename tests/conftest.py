"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture
def local_compute():
    """Create a Compute instance targeting localhost."""
    from zakuro import Compute

    return Compute(host="127.0.0.1", port=3960)

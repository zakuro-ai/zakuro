"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture
def local_compute():
    """Create a Compute instance targeting localhost."""
    from zakuro import Compute

    return Compute(host="127.0.0.1", port=3960)


# Modules that exercise the transport layer (scheme parsing, reachability
# probes, adaptive warm-up) and so construct a Compute pointing straight at a
# worker or cluster. Released zakuro refuses direct addressing unless
# ZAKURO_ALLOW_DIRECT is set (work is meant to go through a broker); that
# policy is separate from what these modules test, so they opt out of it here
# rather than each test testing the policy by accident.
_TRANSPORT_LEVEL_MODULES = (
    "test_compute",
    "test_processors",
    "test_standalone",
    "test_adaptive",
    "test_worker_runner",
)


@pytest.fixture(autouse=True)
def _allow_direct_addressing_in_transport_tests(request, monkeypatch):
    module = request.node.module.__name__.rsplit(".", 1)[-1]
    if module in _TRANSPORT_LEVEL_MODULES:
        monkeypatch.setenv("ZAKURO_ALLOW_DIRECT", "1")

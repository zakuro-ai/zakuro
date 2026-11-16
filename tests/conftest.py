"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture
def local_compute():
    """Create a Compute instance targeting localhost."""
    from zakuro import Compute

    return Compute(host="127.0.0.1", port=3960)


# Modules that exercise the TRANSPORT layer -- scheme parsing, reachability
# probes, adaptive warm-up -- and so must be able to construct a Compute
# pointing straight at a worker or cluster. The addressing POLICY (only
# zc://node-<fp> reaches across the mesh) is a separate concern, tested on its
# own in test_mesh_nodes.py::TestDirectAddressingIsRefused. Without this they
# would be testing the policy by accident, and every transport test would have
# to be rewritten whenever the policy moves.
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

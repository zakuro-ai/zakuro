#!/usr/bin/env python3
"""Test that default API points to production broker."""

import zakuro as zk
from zakuro.config import Config


def test_default_config():
    """Test that config defaults to production broker."""
    config = Config()
    assert config.default_host == "my.zakuro-ai.com", f"Expected my.zakuro-ai.com, got {config.default_host}"
    assert config.default_port == 9000, f"Expected port 9000, got {config.default_port}"
    print("✓ Config defaults correct")


def test_default_compute():
    """Test that Compute() without args leaves URI unresolved for standalone fallback."""
    compute = zk.Compute()

    # No URI or host - detection happens at call time, standalone otherwise
    assert compute.uri is None, f"Expected None uri, got {compute.uri}"
    assert compute.host is None, f"Expected None host, got {compute.host}"

    print("✓ Compute defaults to unresolved (standalone-capable)")


def test_explicit_uri():
    """Test that explicit URI still works."""
    compute = zk.Compute(uri="zakuro://localhost:3960", verify=False)
    assert compute.scheme == "zakuro"
    assert compute.host == "localhost"
    assert compute.port == 3960
    print("✓ Explicit URI works")


if __name__ == "__main__":
    print("Testing zakuro-ai default API configuration...")
    print()

    test_default_config()
    test_default_compute()
    test_explicit_uri()

    print()
    print("All tests passed! ✅")
    print()
    print("Default configuration:")
    print("  - API: my.zakuro-ai.com:9000 (used when zc installed + broker reachable)")
    print("  - Fallback: standalone (in-process)")
    print(f"  - Version: {zk.__version__}")

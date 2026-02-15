#!/usr/bin/env python3
"""Test that default API points to production broker."""

import zakuro as zk
from zakuro.config import Config


def test_default_config():
    """Test that config defaults to production broker."""
    config = Config()
    assert config.default_host == "my.api.zakuro-ai.com", f"Expected my.api.zakuro-ai.com, got {config.default_host}"
    assert config.default_port == 9000, f"Expected port 9000, got {config.default_port}"
    print("✓ Config defaults correct")


def test_default_compute():
    """Test that Compute() without args uses production broker."""
    compute = zk.Compute()

    # Should use broker scheme by default
    assert compute.scheme == "zc", f"Expected scheme 'zc', got {compute.scheme}"

    # Should point to production
    assert compute.host == "my.api.zakuro-ai.com", f"Expected my.api.zakuro-ai.com, got {compute.host}"
    assert compute.port == 9000, f"Expected port 9000, got {compute.port}"
    assert compute.uri == "zc://my.api.zakuro-ai.com:9000", f"Expected zc://my.api.zakuro-ai.com:9000, got {compute.uri}"

    print("✓ Compute defaults correct")
    print(f"  URI: {compute.uri}")
    print(f"  Host: {compute.host}")
    print(f"  Port: {compute.port}")


def test_explicit_uri():
    """Test that explicit URI still works."""
    compute = zk.Compute(uri="zakuro://localhost:3960")
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
    print(f"  - API: my.api.zakuro-ai.com:9000")
    print(f"  - Scheme: zc:// (broker)")
    print(f"  - Version: {zk.__version__}")

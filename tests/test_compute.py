"""Tests for Compute class."""

import pytest

from zakuro import Compute


class TestCompute:
    """Test cases for Compute resource class."""

    def test_default_resources(self) -> None:
        """Test default resource values."""
        compute = Compute(host="localhost")  # Override discovery
        assert compute.cpus == 1.0
        assert compute.memory == "1Gi"
        assert compute.gpus == 0

    def test_custom_resources(self) -> None:
        """Test custom resource specification."""
        compute = Compute(cpus=4, memory="8Gi", gpus=1, host="localhost")
        assert compute.cpus == 4
        assert compute.memory == "8Gi"
        assert compute.gpus == 1

    def test_memory_bytes_gi(self) -> None:
        """Test memory conversion for Gi units."""
        assert Compute(memory="1Gi", host="localhost").memory_bytes() == 1024**3
        assert Compute(memory="2Gi", host="localhost").memory_bytes() == 2 * 1024**3

    def test_memory_bytes_mi(self) -> None:
        """Test memory conversion for Mi units."""
        assert Compute(memory="512Mi", host="localhost").memory_bytes() == 512 * 1024**2
        assert Compute(memory="1024Mi", host="localhost").memory_bytes() == 1024 * 1024**2

    def test_memory_bytes_g(self) -> None:
        """Test memory conversion for G units."""
        assert Compute(memory="2G", host="localhost").memory_bytes() == 2 * 1024**3

    def test_invalid_memory_format(self) -> None:
        """Test that invalid memory format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid memory format"):
            Compute(memory="invalid", host="localhost")

    def test_endpoint_property(self) -> None:
        """Test endpoint URL construction."""
        compute = Compute(host="worker.local", port=9000)
        assert compute.endpoint == "http://worker.local:9000"

    def test_repr(self) -> None:
        """Test string representation."""
        compute = Compute(cpus=2, memory="4Gi", host="localhost")
        repr_str = repr(compute)
        assert "cpus=2" in repr_str
        assert "memory='4Gi'" in repr_str

    def test_repr_with_gpus(self) -> None:
        """Test repr includes GPUs when specified."""
        compute = Compute(cpus=1, memory="1Gi", gpus=2, host="localhost")
        assert "gpus=2" in repr(compute)

    def test_env_dict(self) -> None:
        """Test environment variable specification."""
        compute = Compute(
            host="localhost",
            env={"KEY": "value", "ANOTHER": "val2"},
        )
        assert compute.env == {"KEY": "value", "ANOTHER": "val2"}

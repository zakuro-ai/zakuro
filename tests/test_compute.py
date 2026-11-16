"""Tests for Compute class."""

import pytest

from zakuro import Compute


class TestCompute:
    """Test cases for Compute resource class."""

    def test_default_resources(self) -> None:
        """Test default resource values.

        ``memory`` defaults to ``None`` — the standalone fallback raises if an
        explicit memory constraint is set but cannot be enforced, so leaving it
        unset by default keeps the fallback path usable.
        """
        compute = Compute(host="localhost")  # Override discovery
        assert compute.cpus == 1.0
        assert compute.memory is None
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

    def test_repr_with_image(self) -> None:
        """Test repr includes the worker image when specified."""
        compute = Compute(cpus=1, memory="1Gi", image="zakuro/worker:1", host="localhost")
        assert "image='zakuro/worker:1'" in repr(compute)

    def test_memory_bytes_requires_memory(self) -> None:
        """memory_bytes() on an uncapped Compute is a programming error."""
        with pytest.raises(ValueError, match="memory is not set"):
            Compute(host="localhost").memory_bytes()

    def test_memory_bytes_rejects_memory_mutated_after_construction(self) -> None:
        """Validation happens in __post_init__, so memory_bytes re-checks the format."""
        compute = Compute(memory="1Gi", host="localhost")
        compute.memory = "lots"
        with pytest.raises(ValueError, match="Invalid memory format"):
            compute.memory_bytes()

    def test_endpoint_warns_and_marks_in_process_without_host(self) -> None:
        """An unresolved Compute has no meaningful endpoint; it says so loudly."""
        compute = Compute()
        with pytest.warns(UserWarning, match="no resolved backend"):
            assert compute.endpoint == "zc://in-process"


class TestVerifyReachable:
    """Compute(uri=...) probes the backend at construction unless verify=False."""

    def test_unreachable_tcp_backend_raises_connection_error(self, monkeypatch) -> None:
        """A refused TCP connect is reported with the URI and the escape hatch."""
        import socket

        def _refused(address, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(socket, "create_connection", _refused)
        with pytest.raises(ConnectionError, match="is not reachable"):
            Compute(uri="zakuro://worker.invalid:3960")

    def test_quic_backend_only_resolves_dns(self, monkeypatch) -> None:
        """QUIC is UDP; there is no cheap probe, so only the name is resolved."""
        import socket

        seen: dict = {}

        def _getaddrinfo(host, port, type=None):
            seen["host"] = host
            seen["type"] = type
            return [("fake",)]

        monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
        monkeypatch.setattr(
            socket,
            "create_connection",
            lambda *a, **k: pytest.fail("QUIC must not TCP-connect"),
        )
        compute = Compute(uri="quic://worker.local:4433")
        assert compute.port == 4433
        assert seen["host"] == "worker.local"
        assert seen["type"] == socket.SOCK_DGRAM

    def test_quic_unresolvable_host_raises_connection_error(self, monkeypatch) -> None:
        """An unresolvable QUIC host fails at construction with a fixable message."""
        import socket

        def _fail(host, port, type=None):
            raise socket.gaierror("no such host")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        with pytest.raises(ConnectionError, match="unresolvable host"):
            Compute(uri="quic://nope.invalid:4433")

    def test_verify_false_skips_the_probe(self, monkeypatch) -> None:
        """verify=False is the documented way to inspect a Compute offline."""
        import socket

        monkeypatch.setattr(
            socket, "create_connection", lambda *a, **k: pytest.fail("must not probe")
        )
        assert Compute(uri="zakuro://worker.invalid:3960", verify=False).host == "worker.invalid"


class TestCheck:
    """Compute.check() validates connectivity first, then resource headroom."""

    def _patch_client(self, monkeypatch, *, ping=True, info: dict | None = None) -> list[str]:
        """Install a stub ZakuroClient; return the list it appends to on close()."""
        import zakuro.client

        closed: list[str] = []

        class _StubClient:
            def __init__(self, compute) -> None:
                self._compute = compute

            def ping(self) -> bool:
                if isinstance(ping, Exception):
                    raise ping
                return ping

            def info(self) -> dict:
                return info or {}

            def close(self) -> None:
                closed.append("closed")

        monkeypatch.setattr(zakuro.client, "ZakuroClient", _StubClient)
        return closed

    def test_returns_worker_info_when_resources_fit(self, monkeypatch) -> None:
        """A worker with headroom returns its own info dict unchanged."""
        payload = {"cpus_available": 8.0, "memory_available": 16 * 1024**3, "gpus_available": 2}
        closed = self._patch_client(monkeypatch, info=payload)
        compute = Compute(host="worker", cpus=2, memory="1Gi", gpus=1, verify=False)
        assert compute.check() == payload
        assert closed == ["closed"]

    def test_unreachable_worker_raises_connection_error(self, monkeypatch) -> None:
        """A failed health probe is a ConnectionError naming the endpoint."""
        self._patch_client(monkeypatch, ping=False)
        with pytest.raises(ConnectionError, match="Worker unreachable"):
            Compute(host="worker", verify=False).check()

    def test_transport_failure_is_wrapped_as_connection_error(self, monkeypatch) -> None:
        """Any transport-level exception is normalised to ConnectionError."""
        self._patch_client(monkeypatch, ping=OSError("socket died"))
        with pytest.raises(ConnectionError, match="socket died"):
            Compute(host="worker", verify=False).check()

    def test_insufficient_cpus(self, monkeypatch) -> None:
        """Asking for more CPUs than the worker reports is refused up front."""
        self._patch_client(monkeypatch, info={"cpus_available": 1.0})
        with pytest.raises(RuntimeError, match="Insufficient CPUs: requested 4"):
            Compute(host="worker", cpus=4, verify=False).check()

    def test_insufficient_memory(self, monkeypatch) -> None:
        """The memory check compares against the resolved byte count."""
        self._patch_client(monkeypatch, info={"memory_available": 1024})
        with pytest.raises(RuntimeError, match="Insufficient memory: requested 8Gi"):
            Compute(host="worker", memory="8Gi", verify=False).check()

    def test_insufficient_gpus(self, monkeypatch) -> None:
        """GPUs are checked last, after CPU and memory."""
        self._patch_client(monkeypatch, info={"gpus_available": 0})
        with pytest.raises(RuntimeError, match="Insufficient GPUs: requested 2"):
            Compute(host="worker", gpus=2, verify=False).check()

    def test_absent_fields_are_not_enforced(self, monkeypatch) -> None:
        """A worker that omits a resource key is trusted rather than rejected."""
        self._patch_client(monkeypatch, info={})
        assert Compute(host="worker", cpus=64, gpus=8, verify=False).check() == {}

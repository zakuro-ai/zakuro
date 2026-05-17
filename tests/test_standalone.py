"""Tests for standalone (in-process fallback) mode."""

from __future__ import annotations

import os
import warnings
from unittest.mock import patch

import pytest

from zakuro import Compute, cls, fn
from zakuro.standalone import applied_resource_limits, detect_backend, is_standalone


class TestComputeDefaults:
    """Compute() with no args should leave URI unresolved for detection."""

    def test_compute_no_args_has_no_uri(self) -> None:
        compute = Compute()
        assert compute.uri is None
        assert compute.host is None

    def test_compute_with_uri_preserved(self) -> None:
        compute = Compute(uri="ray://head:10001", verify=False)
        assert compute.uri == "ray://head:10001"
        assert compute.host == "head"
        assert compute.port == 10001

    def test_compute_with_host_builds_uri(self) -> None:
        compute = Compute(host="worker.local", port=3960)
        assert compute.uri == "zc://worker.local:3960"


class TestDetectBackend:
    """detect_backend() probes and returns URI or None."""

    def test_returns_none_when_nothing_reachable(self) -> None:
        with (
            patch("zakuro.standalone._tcp_reachable", return_value=False),
            patch("zakuro.standalone._has_zc_cli", return_value=False),
            patch("zakuro.standalone._discover_tailscale_worker", return_value=None),
        ):
            assert detect_backend() is None

    def test_returns_local_worker_uri_when_reachable(self) -> None:
        def _only_local_worker(host: str, port: int) -> bool:
            return host == "127.0.0.1" and port == 3960

        with patch("zakuro.standalone._tcp_reachable", side_effect=_only_local_worker):
            assert detect_backend() == "zakuro://127.0.0.1:3960"

    def test_returns_broker_when_zc_installed_and_reachable(self) -> None:
        def _only_remote_broker(host: str, port: int) -> bool:
            return host == "my.zakuro-ai.com"

        from zakuro.config import Config

        config = Config(default_host="my.zakuro-ai.com", default_port=9000)
        with (
            patch("zakuro.standalone._tcp_reachable", side_effect=_only_remote_broker),
            patch("zakuro.standalone._has_zc_cli", return_value=True),
        ):
            assert detect_backend(config) == "zc://my.zakuro-ai.com:9000"

    def test_skips_remote_broker_without_zc_cli(self) -> None:
        with (
            patch("zakuro.standalone._tcp_reachable", return_value=False) as tcp,
            patch("zakuro.standalone._has_zc_cli", return_value=False),
            patch("zakuro.standalone._discover_tailscale_worker", return_value=None),
        ):
            assert detect_backend() is None
            probed_hosts = {call.args[0] for call in tcp.call_args_list}
            assert "my.zakuro-ai.com" not in probed_hosts

    def test_force_env_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZAKURO_STANDALONE", "force")
        with patch("zakuro.standalone._tcp_reachable", return_value=True):
            assert detect_backend() is None

    def test_is_standalone_true_when_no_backend(self) -> None:
        with patch("zakuro.standalone.detect_backend", return_value=None):
            assert is_standalone() is True

    def test_is_standalone_false_when_backend_found(self) -> None:
        with patch("zakuro.standalone.detect_backend", return_value="zakuro://127.0.0.1:3960"):
            assert is_standalone() is False


class TestFnStandaloneFallback:
    """@fn with a URI-less Compute should run in-process when nothing is reachable."""

    def test_fn_runs_in_process_when_no_backend(self) -> None:
        @fn
        def add(a: int, b: int) -> int:
            return a + b

        with patch("zakuro.standalone.detect_backend", return_value=None):
            result = add.to(Compute(cpus=2))(3, 4)
        assert result == 7

    def test_fn_uses_detected_backend_when_available(self) -> None:
        calls: dict[str, object] = {}

        @fn
        def noop() -> None:
            calls["local"] = True

        class _FakeProcessor:
            def __enter__(self) -> _FakeProcessor:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def execute(self, func_bytes: bytes, args: tuple, kwargs: dict) -> str:
                calls["remote"] = True
                return "remote-result"

        with (
            patch(
                "zakuro.standalone.detect_backend",
                return_value="zakuro://127.0.0.1:3960",
            ),
            patch(
                "zakuro.processors.registry.get_processor",
                return_value=_FakeProcessor(),
            ),
        ):
            result = noop.to(Compute())()

        assert result == "remote-result"
        assert "remote" in calls
        assert "local" not in calls


class TestEndpointAndSchemeWhenUnresolved:
    """Unresolved Compute should warn (not raise) and return standalone markers."""

    def test_endpoint_warns_and_returns_marker(self) -> None:
        compute = Compute()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            endpoint = compute.endpoint
        assert endpoint == "zc://in-process"
        assert any("standalone" in str(w.message).lower() for w in caught)

    def test_endpoint_resolved_host_returns_http(self) -> None:
        compute = Compute(host="worker.local", port=3960)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            endpoint = compute.endpoint
        assert endpoint == "http://worker.local:3960"
        assert caught == []

    def test_scheme_returns_zc_when_unresolved(self) -> None:
        assert Compute().scheme == "zc"


class TestAdvisoryResourceLimits:
    """applied_resource_limits should set thread env vars and restore them."""

    def test_sets_cpu_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
        monkeypatch.delenv("MKL_NUM_THREADS", raising=False)
        compute = Compute(cpus=3)
        with applied_resource_limits(compute):
            assert os.environ["OMP_NUM_THREADS"] == "3"
            assert os.environ["MKL_NUM_THREADS"] == "3"
        assert "OMP_NUM_THREADS" not in os.environ
        assert "MKL_NUM_THREADS" not in os.environ

    def test_fractional_cpus_round_up(self) -> None:
        with applied_resource_limits(Compute(cpus=0.5)):
            assert os.environ["OMP_NUM_THREADS"] == "1"

    def test_zero_gpus_hides_cuda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
        with applied_resource_limits(Compute(gpus=0)):
            assert os.environ["CUDA_VISIBLE_DEVICES"] == ""
        assert os.environ["CUDA_VISIBLE_DEVICES"] == "0,1"

    def test_restores_previous_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMP_NUM_THREADS", "42")
        with applied_resource_limits(Compute(cpus=2)):
            assert os.environ["OMP_NUM_THREADS"] == "2"
        assert os.environ["OMP_NUM_THREADS"] == "42"

    def test_fn_standalone_applies_limits(self) -> None:
        observed: dict[str, str] = {}

        @fn
        def probe() -> None:
            observed["omp"] = os.environ.get("OMP_NUM_THREADS", "")

        with patch("zakuro.standalone.detect_backend", return_value=None):
            probe.to(Compute(cpus=4))()

        assert observed["omp"] == "4"


class TestClsStandaloneFallback:
    """@cls with a URI-less Compute should instantiate in-process when nothing is reachable."""

    def test_cls_instantiates_in_process_when_no_backend(self) -> None:
        @cls
        class Counter:
            def __init__(self, start: int = 0) -> None:
                self.value = start

            def inc(self) -> int:
                self.value += 1
                return self.value

        with patch("zakuro.standalone.detect_backend", return_value=None):
            counter = Counter.to(Compute())(10)

        assert counter.value == 10
        assert counter.inc() == 11


class TestExplicitUriVerification:
    """Explicit URIs should be probed at construction."""

    def test_explicit_uri_unreachable_raises(self) -> None:
        # Reserved TEST-NET-1 address 192.0.2.0/24 is guaranteed unroutable.
        with pytest.raises(ConnectionError, match="not reachable"):
            Compute(uri="ray://192.0.2.1:10001")

    def test_explicit_uri_reachable_succeeds(self) -> None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            compute = Compute(uri=f"ray://127.0.0.1:{port}")
        assert compute.uri == f"ray://127.0.0.1:{port}"

    def test_verify_false_skips_probe(self) -> None:
        compute = Compute(uri="ray://192.0.2.1:10001", verify=False)
        assert compute.uri == "ray://192.0.2.1:10001"

    def test_host_only_not_probed(self) -> None:
        # host-only constructs the URI from host/port but doesn't count as
        # "explicit" — retains legacy behavior so no probe is performed.
        compute = Compute(host="192.0.2.1", port=12345)
        assert compute.host == "192.0.2.1"


class TestMemoryEnforcementRaises:
    """Explicit memory with standalone fallback should raise, not silently run."""

    def test_fn_raises_when_memory_set_and_no_backend(self) -> None:
        @fn
        def noop() -> int:
            return 1

        with (
            patch("zakuro.standalone.detect_backend", return_value=None),
            pytest.raises(RuntimeError, match="Memory limit"),
        ):
            noop.to(Compute(memory="2Gi"))()

    def test_fn_runs_when_memory_unset_and_no_backend(self) -> None:
        @fn
        def noop() -> int:
            return 1

        with patch("zakuro.standalone.detect_backend", return_value=None):
            assert noop.to(Compute(cpus=2))() == 1

    def test_cls_raises_when_memory_set_and_no_backend(self) -> None:
        @cls
        class Box:
            def __init__(self, x: int) -> None:
                self.x = x

        with (
            patch("zakuro.standalone.detect_backend", return_value=None),
            pytest.raises(RuntimeError, match="Memory limit"),
        ):
            Box.to(Compute(memory="1Gi"))(5)

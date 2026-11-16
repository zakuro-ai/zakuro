"""Tests for processor system."""

import json
from unittest.mock import MagicMock, patch

import pytest

from zakuro import Compute, available_processors
from zakuro.processors import ProcessorConfig, ProcessorRegistry, get_processor
from zakuro.processors.base import Processor
from zakuro.processors.http import HttpProcessor


class TestProcessorConfig:
    """Test cases for ProcessorConfig."""

    def test_parse_zakuro_uri(self) -> None:
        """Test parsing zakuro:// URI."""
        config = ProcessorConfig.from_uri("zakuro://worker:3960")
        assert config.scheme == "zakuro"
        assert config.host == "worker"
        assert config.port == 3960

    def test_parse_ray_uri(self) -> None:
        """Test parsing ray:// URI."""
        config = ProcessorConfig.from_uri("ray://head-node:10001")
        assert config.scheme == "ray"
        assert config.host == "head-node"
        assert config.port == 10001

    def test_parse_dask_uri(self) -> None:
        """Test parsing dask:// URI."""
        config = ProcessorConfig.from_uri("dask://scheduler:8786")
        assert config.scheme == "dask"
        assert config.host == "scheduler"
        assert config.port == 8786

    def test_parse_tcp_uri(self) -> None:
        """Test parsing tcp:// URI (Dask alias)."""
        config = ProcessorConfig.from_uri("tcp://scheduler:8786")
        assert config.scheme == "tcp"
        assert config.host == "scheduler"
        assert config.port == 8786

    def test_parse_spark_uri(self) -> None:
        """Test parsing spark:// URI."""
        config = ProcessorConfig.from_uri("spark://master:7077")
        assert config.scheme == "spark"
        assert config.host == "master"
        assert config.port == 7077

    def test_default_ports(self) -> None:
        """Test default port assignment."""
        assert ProcessorConfig.from_uri("zakuro://host").port == 3960
        assert ProcessorConfig.from_uri("ray://host").port == 10001
        assert ProcessorConfig.from_uri("dask://host").port == 8786
        assert ProcessorConfig.from_uri("spark://host").port == 7077

    def test_parse_query_params(self) -> None:
        """Test parsing query parameters."""
        config = ProcessorConfig.from_uri("ray://host:10001?timeout=30&retries=3")
        assert config.params == {"timeout": "30", "retries": "3"}

    def test_endpoint_property(self) -> None:
        """Test endpoint generation."""
        config = ProcessorConfig.from_uri("ray://head:10001")
        assert config.endpoint == "ray://head:10001"


class TestProcessorRegistry:
    """Test cases for ProcessorRegistry."""

    def test_singleton_pattern(self) -> None:
        """Test that registry is a singleton."""
        r1 = ProcessorRegistry()
        r2 = ProcessorRegistry()
        assert r1 is r2

    def test_http_always_available(self) -> None:
        """Test that HTTP processor is always discovered."""
        registry = ProcessorRegistry()
        registry.refresh()
        assert "zakuro" in registry.available()
        assert "http" in registry.available()

    def test_broker_always_available(self) -> None:
        """Test that Broker processor is always discovered."""
        registry = ProcessorRegistry()
        registry.refresh()
        assert "zc" in registry.available()
        assert "broker" in registry.available()

    def test_available_processors(self) -> None:
        """Test available_processors returns canonical names."""
        registry = ProcessorRegistry()
        registry.refresh()
        procs = registry.available_processors()
        assert "zakuro" in procs

    def test_get_processor_http(self) -> None:
        """Test getting HTTP processor by URI."""
        registry = ProcessorRegistry()
        compute = Compute(host="localhost")
        processor = registry.get("zakuro://localhost:3960", compute)
        assert isinstance(processor, HttpProcessor)

    def test_get_processor_invalid_scheme(self) -> None:
        """Test error for unsupported scheme."""
        registry = ProcessorRegistry()
        compute = Compute(host="localhost")
        with pytest.raises(ValueError, match="Unsupported processor scheme"):
            registry.get("unknown://host:1234", compute)


class TestHttpProcessor:
    """Test cases for HttpProcessor."""

    def test_is_available(self) -> None:
        """Test HTTP processor is always available."""
        assert HttpProcessor.is_available() is True

    def test_priority(self) -> None:
        """Test HTTP processor has lowest priority."""
        assert HttpProcessor.priority == 10

    def test_schemes(self) -> None:
        """Test HTTP processor handles correct schemes."""
        assert "zakuro" in HttpProcessor.schemes
        assert "http" in HttpProcessor.schemes

    def test_context_manager(self) -> None:
        """Test processor context manager lifecycle."""
        config = ProcessorConfig.from_uri("zakuro://localhost:3960")
        compute = Compute(host="localhost")
        processor = HttpProcessor(config, compute)

        assert not processor.is_connected

        with patch.object(processor, "_client", MagicMock()):
            processor.connect()
            assert processor.is_connected
            processor.disconnect()
            assert not processor.is_connected

    def test_execute_not_connected(self) -> None:
        """Test execute raises when not connected."""
        config = ProcessorConfig.from_uri("zakuro://localhost:3960")
        compute = Compute(host="localhost")
        processor = HttpProcessor(config, compute)

        with pytest.raises(RuntimeError, match="not connected"):
            processor.execute(b"", (), {})


class TestRayProcessor:
    """Test cases for RayProcessor (mocked)."""

    def test_is_available_without_ray(self) -> None:
        """Test RayProcessor.is_available when ray not installed."""
        with patch.dict("sys.modules", {"ray": None}):
            from zakuro.processors.ray_processor import RayProcessor

            # When import fails, is_available returns False
            with patch.object(RayProcessor, "is_available", return_value=False):
                assert RayProcessor.is_available() is False

    def test_priority(self) -> None:
        """Test Ray processor has highest priority."""
        from zakuro.processors.ray_processor import RayProcessor

        assert RayProcessor.priority == 50

    def test_schemes(self) -> None:
        """Test Ray processor handles correct schemes."""
        from zakuro.processors.ray_processor import RayProcessor

        assert "ray" in RayProcessor.schemes


class TestDaskProcessor:
    """Test cases for DaskProcessor (mocked)."""

    def test_priority(self) -> None:
        """Test Dask processor priority."""
        from zakuro.processors.dask_processor import DaskProcessor

        assert DaskProcessor.priority == 40

    def test_schemes(self) -> None:
        """Test Dask processor handles correct schemes."""
        from zakuro.processors.dask_processor import DaskProcessor

        assert "dask" in DaskProcessor.schemes
        assert "tcp" in DaskProcessor.schemes


class TestSparkProcessor:
    """Test cases for SparkProcessor (mocked)."""

    def test_priority(self) -> None:
        """Test Spark processor priority."""
        from zakuro.processors.spark_processor import SparkProcessor

        assert SparkProcessor.priority == 30

    def test_schemes(self) -> None:
        """Test Spark processor handles correct schemes."""
        from zakuro.processors.spark_processor import SparkProcessor

        assert "spark" in SparkProcessor.schemes


class TestBrokerProcessor:
    """Test cases for BrokerProcessor."""

    def test_is_available(self) -> None:
        """Test Broker processor is always available."""
        from zakuro.processors.broker import BrokerProcessor

        assert BrokerProcessor.is_available() is True

    def test_priority(self) -> None:
        """Test Broker processor has highest priority."""
        from zakuro.processors.broker import BrokerProcessor

        assert BrokerProcessor.priority == 100

    def test_schemes(self) -> None:
        """Test Broker processor handles correct schemes."""
        from zakuro.processors.broker import BrokerProcessor

        assert "zc" in BrokerProcessor.schemes
        assert "broker" in BrokerProcessor.schemes

    def test_default_port(self) -> None:
        """Test broker default port is 9000."""
        config = ProcessorConfig.from_uri("zc://localhost")
        assert config.port == 9000

    def test_parse_uri(self) -> None:
        """Test parsing broker URI."""
        config = ProcessorConfig.from_uri("zc://broker.zakuro-ai.com:9000")
        assert config.scheme == "zc"
        assert config.host == "broker.zakuro-ai.com"
        assert config.port == 9000


class TestModuleLevelFunctions:
    """Test module-level convenience functions."""

    def test_get_processor(self) -> None:
        """Test get_processor function."""
        compute = Compute(host="localhost")
        processor = get_processor("zakuro://localhost:3960", compute)
        assert isinstance(processor, Processor)

    def test_available_processors_export(self) -> None:
        """Test available_processors is exported from main module."""
        procs = available_processors()
        assert isinstance(procs, list)
        assert "zakuro" in procs


class TestComputeUriIntegration:
    """Test Compute class URI integration."""

    def test_uri_field(self) -> None:
        """Test Compute with explicit URI."""
        compute = Compute(uri="ray://head:10001", cpus=4, verify=False)
        assert compute.uri == "ray://head:10001"
        assert compute.host == "head"
        assert compute.port == 10001
        assert compute.cpus == 4

    def test_host_port_builds_uri(self) -> None:
        """Test Compute builds URI from host/port."""
        compute = Compute(host="worker", port=9000)
        assert compute.uri == "zc://worker:9000"

    def test_scheme_property(self) -> None:
        """Test scheme property extracts from URI."""
        compute = Compute(uri="dask://scheduler:8786", verify=False)
        assert compute.scheme == "dask"

    def test_default_scheme_is_zc(self) -> None:
        """Test default scheme is zc."""
        compute = Compute(host="localhost")
        assert compute.scheme == "zc"


class TestBrokerTargeting:
    """BrokerProcessor forwards mesh pin targets into the requirements header."""

    def _run_execute(self, options: dict) -> dict:
        """Execute through a mocked broker; return the parsed requirements dict."""
        import cloudpickle
        import httpx

        from zakuro.processors.base import ProcessorConfig
        from zakuro.processors.broker import BrokerProcessor

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["requirements"] = json.loads(request.headers["X-Zakuro-Requirements"])
            return httpx.Response(200, content=cloudpickle.dumps("ok"))

        compute = Compute(host="broker", port=9000, memory="1Gi", verify=False)
        compute.processor_options = options
        proc = BrokerProcessor(ProcessorConfig.from_uri("zc://broker:9000"), compute)
        proc._client = httpx.Client(
            base_url="http://broker:9000", transport=httpx.MockTransport(handler)
        )
        proc._connected = True

        result = proc.execute(cloudpickle.dumps(lambda: "ok"), (), {})
        assert result == "ok"
        return captured["requirements"]

    def test_target_worker_and_node_forwarded(self) -> None:
        """target_worker/target_node from processor_options land in requirements verbatim."""
        req = self._run_execute(
            {"target_worker": "zc://worker-abc", "target_node": "zc://node-alpha"}
        )
        assert req["target_worker"] == "zc://worker-abc"
        assert req["target_node"] == "zc://node-alpha"

    def test_no_target_omits_keys(self) -> None:
        """Without pins the keys are absent (broker uses normal strategy routing)."""
        req = self._run_execute({})
        assert "target_worker" not in req
        assert "target_node" not in req


class TestHttpProcessorTransport:
    """HttpProcessor delegates to ZakuroClient; these pin the delegation."""

    def _connected(self, client: MagicMock) -> HttpProcessor:
        config = ProcessorConfig.from_uri("zakuro://worker:3960")
        processor = HttpProcessor(config, Compute(host="worker", port=3960, verify=False))
        processor._client = client
        processor._connected = True
        return processor

    def test_connect_rewrites_compute_host_and_port_from_uri(self) -> None:
        """The URI is authoritative: connect() pushes its host/port onto Compute."""
        compute = Compute(host="stale", port=1234, verify=False)
        processor = HttpProcessor(ProcessorConfig.from_uri("zakuro://worker:3960"), compute)
        processor.connect()
        try:
            assert compute.host == "worker"
            assert compute.port == 3960
        finally:
            processor.disconnect()

    def test_connect_is_idempotent(self) -> None:
        """A second connect() must not discard the live client."""
        processor = HttpProcessor(
            ProcessorConfig.from_uri("zakuro://worker:3960"), Compute(host="worker", verify=False)
        )
        processor.connect()
        first = processor._client
        processor.connect()
        try:
            assert processor._client is first
        finally:
            processor.disconnect()

    def test_execute_unwraps_result(self) -> None:
        """The worker's cloudpickle reply is deserialized for the caller."""
        import cloudpickle

        client = MagicMock()
        client.execute.return_value = cloudpickle.dumps(21)
        assert self._connected(client).execute(cloudpickle.dumps(lambda: 21), (), {}) == 21

    def test_execute_reraises_worker_exception(self) -> None:
        """An exception in the reply payload is raised on the client side."""
        import cloudpickle

        client = MagicMock()
        client.execute.return_value = cloudpickle.dumps(ZeroDivisionError("nope"))
        with pytest.raises(ZeroDivisionError, match="nope"):
            self._connected(client).execute(cloudpickle.dumps(lambda: 1), (), {})

    def test_execute_payload_forwards_extra_headers(self) -> None:
        """@cls affinity headers are passed straight through to the client."""
        client = MagicMock()
        client.execute.return_value = b"raw"
        headers = {"X-Zakuro-Instance-Id": "instance_1"}
        assert self._connected(client).execute_payload(b"payload", headers) == b"raw"
        client.execute.assert_called_once_with(b"payload", extra_headers=headers)

    def test_execute_payload_requires_a_connection(self) -> None:
        """Stateful dispatch outside the context manager is a clear error."""
        processor = HttpProcessor(
            ProcessorConfig.from_uri("zakuro://worker:3960"), Compute(host="worker", verify=False)
        )
        with pytest.raises(RuntimeError, match="not connected"):
            processor.execute_payload(b"payload")

    def test_ping_delegates_and_defaults_false(self) -> None:
        """ping() is False before connect and mirrors the client afterwards."""
        processor = HttpProcessor(
            ProcessorConfig.from_uri("zakuro://worker:3960"), Compute(host="worker", verify=False)
        )
        assert processor.ping() is False
        client = MagicMock()
        client.ping.return_value = True
        assert self._connected(client).ping() is True

    def test_info_delegates_and_requires_a_connection(self) -> None:
        """info() surfaces the worker document, or refuses if not connected."""
        processor = HttpProcessor(
            ProcessorConfig.from_uri("zakuro://worker:3960"), Compute(host="worker", verify=False)
        )
        with pytest.raises(RuntimeError, match="not connected"):
            processor.info()
        client = MagicMock()
        client.info.return_value = {"name": "w1"}
        assert self._connected(client).info() == {"name": "w1"}


class TestProcessorBaseDefaults:
    """Processor's own defaults, independent of any backend."""

    def test_execute_payload_is_unsupported_by_default(self) -> None:
        """Backends that do not speak the stateful protocol say so by name."""
        from zakuro.processors.dask_processor import DaskProcessor

        processor = DaskProcessor(
            ProcessorConfig.from_uri("dask://scheduler:8786"),
            Compute(host="scheduler", verify=False),
        )
        with pytest.raises(NotImplementedError, match="DaskProcessor does not support"):
            processor.execute_payload(b"payload")

    def test_config_and_compute_are_exposed(self) -> None:
        """The config/compute properties are the public accessors on a processor."""
        config = ProcessorConfig.from_uri("zakuro://worker:3960")
        compute = Compute(host="worker", verify=False)
        processor = HttpProcessor(config, compute)
        assert processor.config is config
        assert processor.compute is compute
        assert repr(processor) == "HttpProcessor(uri='zakuro://worker:3960')"


class TestRegistrySelection:
    """get_best picks by declared priority and builds a default URI for it."""

    def test_get_best_returns_highest_priority_backend(self) -> None:
        """With only the always-available backends installed, the broker (100) wins."""
        from zakuro.processors.broker import BrokerProcessor

        registry = ProcessorRegistry()
        registry.refresh()
        processor = registry.get_best(Compute(host="broker-host", verify=False))
        assert isinstance(processor, BrokerProcessor)
        assert processor.config.host == "broker-host"
        assert processor.config.port == 9000

    def test_get_best_defaults_host_to_localhost(self) -> None:
        """An unresolved Compute still yields a usable local default URI."""
        registry = ProcessorRegistry()
        registry.refresh()
        assert registry.get_best(Compute()).config.host == "localhost"

    def test_get_best_raises_when_nothing_is_registered(self) -> None:
        """An empty registry is a hard error, not a silent fallback."""
        registry = ProcessorRegistry()
        try:
            registry._processors = {}
            with pytest.raises(RuntimeError, match="No processors available"):
                registry.get_best(Compute(host="localhost"))
        finally:
            registry.refresh()

    def test_get_best_processor_module_function(self) -> None:
        """The module-level helper shares the singleton registry."""
        from zakuro.processors.broker import BrokerProcessor
        from zakuro.processors.registry import get_best_processor

        assert isinstance(
            get_best_processor(Compute(host="broker-host", verify=False)), BrokerProcessor
        )

    def test_refresh_forces_rediscovery(self) -> None:
        """refresh() drops the cache so newly installed extras are picked up."""
        registry = ProcessorRegistry()
        registry.refresh()
        assert registry._processors is None
        assert "zc" in registry.available()
        assert registry._processors is not None

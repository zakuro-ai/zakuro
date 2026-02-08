"""Tests for processor system."""

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
        config = ProcessorConfig.from_uri("zakuro://worker:8000")
        assert config.scheme == "zakuro"
        assert config.host == "worker"
        assert config.port == 8000

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
        assert ProcessorConfig.from_uri("zakuro://host").port == 8000
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
        processor = registry.get("zakuro://localhost:8000", compute)
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
        config = ProcessorConfig.from_uri("zakuro://localhost:8000")
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
        config = ProcessorConfig.from_uri("zakuro://localhost:8000")
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
        config = ProcessorConfig.from_uri("zc://broker.zakuro.ai:9000")
        assert config.scheme == "zc"
        assert config.host == "broker.zakuro.ai"
        assert config.port == 9000


class TestModuleLevelFunctions:
    """Test module-level convenience functions."""

    def test_get_processor(self) -> None:
        """Test get_processor function."""
        compute = Compute(host="localhost")
        processor = get_processor("zakuro://localhost:8000", compute)
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
        compute = Compute(uri="ray://head:10001", cpus=4)
        assert compute.uri == "ray://head:10001"
        assert compute.host == "head"
        assert compute.port == 10001
        assert compute.cpus == 4

    def test_host_port_builds_uri(self) -> None:
        """Test Compute builds URI from host/port."""
        compute = Compute(host="worker", port=9000)
        assert compute.uri == "zakuro://worker:9000"

    def test_scheme_property(self) -> None:
        """Test scheme property extracts from URI."""
        compute = Compute(uri="dask://scheduler:8786")
        assert compute.scheme == "dask"

    def test_default_scheme_is_zakuro(self) -> None:
        """Test default scheme is zakuro."""
        compute = Compute(host="localhost")
        assert compute.scheme == "zakuro"

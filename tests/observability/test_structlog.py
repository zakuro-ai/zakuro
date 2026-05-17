"""Unit tests for the structlog JSON pipeline (#125, RFC 0003 Track A)."""

from __future__ import annotations

import io
import json
import logging
import re
from contextvars import copy_context
from typing import Any

import pytest

from zakuro.observability import logging as obs_logging
from zakuro.observability.sentry import _REQUEST_CONTEXT, set_request_context

structlog = pytest.importorskip("structlog", reason="needs [observability] extra")


@pytest.fixture(autouse=True)
def _reset_logging() -> Any:
    """Each test gets a fresh init + ContextVar state."""
    obs_logging._reset_for_tests()
    token = _REQUEST_CONTEXT.set({})
    yield
    _REQUEST_CONTEXT.reset(token)
    obs_logging._reset_for_tests()


def _captured_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_init_logging_returns_true_when_structlog_present() -> None:
    assert obs_logging.init_logging("worker") is True


def test_init_logging_is_idempotent() -> None:
    assert obs_logging.init_logging("worker") is True
    assert obs_logging.init_logging("worker") is True  # second call is a no-op


def test_log_record_is_json_with_stable_keys(capsys: pytest.CaptureFixture[str]) -> None:
    obs_logging.init_logging("worker")
    log = obs_logging.get_logger("test")
    log.info("hello_event", extra_field=7)
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "hello_event" in line)
    record = json.loads(line)
    assert record["event"] == "hello_event"
    assert record["level"] == "info"
    assert record["component"] == "worker"
    assert record["extra_field"] == 7
    # time is ISO 8601 with a Z suffix (UTC), no microseconds-or-not policy here
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", record["time"])


def test_request_context_tags_attach_to_records(capsys: pytest.CaptureFixture[str]) -> None:
    obs_logging.init_logging("worker")
    log = obs_logging.get_logger("test")

    def emit_in_context() -> None:
        set_request_context(request_id="req-abc", tenant_id="tenant-acme", worker_id="worker-3")
        log.info("job_started")

    copy_context().run(emit_in_context)
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "job_started" in line)
    record = json.loads(line)
    assert record["request-id"] == "req-abc"
    assert record["tenant-id"] == "tenant-acme"
    assert record["worker-id"] == "worker-3"


def test_pii_is_redacted_from_string_fields(capsys: pytest.CaptureFixture[str]) -> None:
    obs_logging.init_logging("worker")
    log = obs_logging.get_logger("test")
    log.info("captured", user_text="contact ada@example.com via 10.0.0.5")
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "captured" in line)
    record = json.loads(line)
    assert "@example.com" not in record["user_text"]
    assert "10.0.0.5" not in record["user_text"]
    assert "<email>" in record["user_text"]
    assert "<ip>" in record["user_text"]


def test_stdlib_logging_flows_through_json_formatter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    obs_logging.init_logging("worker")
    logging.getLogger("legacy").warning("via_stdlib")
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "via_stdlib" in line)
    record = json.loads(line)
    assert record["level"] == "warning"
    assert record["event"] == "via_stdlib"
    assert record["component"] == "worker"


def test_level_env_var_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_LOG_LEVEL", "WARNING")
    obs_logging.init_logging("worker")
    assert logging.getLogger().level == logging.WARNING


def test_level_kwarg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_LOG_LEVEL", "WARNING")
    obs_logging.init_logging("worker", level="DEBUG")
    assert logging.getLogger().level == logging.DEBUG

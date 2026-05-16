"""Unit tests for zakuro.observability.sentry._before_send.

The scrubber is exposed at module level so we can exercise it without
booting the Sentry SDK or hitting the network. Each test feeds a hand-
shaped event dict in and asserts the right thing was scrubbed / kept.
"""

from __future__ import annotations

import pytest

from zakuro.observability import sentry as sentry_obs
from zakuro.observability.sentry import _before_send, set_request_context


@pytest.fixture(autouse=True)
def reset_request_context():
    """Ensure ContextVar state doesn't leak between tests."""
    sentry_obs._REQUEST_CONTEXT.set({})
    yield
    sentry_obs._REQUEST_CONTEXT.set({})


def test_strips_email_from_user_field():
    event = {"user": {"id": "u123", "email": "alice@example.com"}}
    out = _before_send(event)
    assert "email" not in out["user"]
    assert out["user"]["id"] == "u123"


def test_strips_ip_and_username_from_user_field():
    event = {"user": {"ip_address": "192.168.1.42", "username": "alice"}}
    out = _before_send(event)
    assert "ip_address" not in out["user"]
    assert "username" not in out["user"]


def test_drops_request_body_entirely():
    event = {"request": {"data": "secret=hunter2", "method": "POST"}}
    out = _before_send(event)
    assert "data" not in out["request"]
    assert out["request"]["method"] == "POST"   # non-PII preserved


def test_redacts_sensitive_headers_case_insensitive():
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret-token",
                "Cookie": "session=abc",
                "X-API-Key": "k123",
                "User-Agent": "zakuro/1.0",
            }
        }
    }
    out = _before_send(event)
    assert out["request"]["headers"]["Authorization"] == "<redacted>"
    assert out["request"]["headers"]["Cookie"] == "<redacted>"
    assert out["request"]["headers"]["X-API-Key"] == "<redacted>"
    assert out["request"]["headers"]["User-Agent"] == "zakuro/1.0"


def test_redacts_emails_inside_log_messages():
    event = {
        "message": "user alice@example.com hit /api/v1/jobs",
        "extra": {"contact": "Reach me at bob@example.com please"},
    }
    out = _before_send(event)
    assert "<email>" in out["message"]
    assert "alice@example.com" not in out["message"]
    assert "<email>" in out["extra"]["contact"]
    assert "bob@example.com" not in out["extra"]["contact"]


def test_redacts_ipv4_addresses_inside_strings():
    event = {"message": "connection from 10.0.0.4 to 192.168.1.1 failed"}
    out = _before_send(event)
    assert "10.0.0.4" not in out["message"]
    assert "192.168.1.1" not in out["message"]
    assert out["message"].count("<ip>") == 2


def test_redacts_home_paths_inside_strings():
    event = {"message": "wrote checkpoint to /Users/alice/work/run.ckpt"}
    out = _before_send(event)
    assert "/Users/alice" not in out["message"]
    assert "/Users/<user>" in out["message"]


def test_preserves_non_pii_strings():
    event = {
        "message": "job j-42 finished",
        "level": "info",
        "extra": {"version": "0.2.6", "duration_ms": 123},
    }
    out = _before_send(event)
    assert out["message"] == "job j-42 finished"
    assert out["level"] == "info"
    assert out["extra"]["version"] == "0.2.6"
    assert out["extra"]["duration_ms"] == 123


def test_set_request_context_propagates_to_tags():
    set_request_context(
        request_id="req-1",
        tenant_id="tenant-acme",
        worker_id="worker-5",
    )
    event: dict = {}
    out = _before_send(event)
    assert out["tags"]["request-id"] == "req-1"
    assert out["tags"]["tenant-id"] == "tenant-acme"
    assert out["tags"]["worker-id"] == "worker-5"


def test_set_request_context_partial_update():
    set_request_context(request_id="req-1", tenant_id="tenant-acme")
    set_request_context(request_id="req-2")  # only request-id changes
    event: dict = {}
    out = _before_send(event)
    assert out["tags"]["request-id"] == "req-2"
    assert out["tags"]["tenant-id"] == "tenant-acme"


def test_handles_event_with_no_user_or_request():
    """A bare message-only event should survive untouched."""
    event = {"message": "all good"}
    out = _before_send(event)
    assert out["message"] == "all good"
    # tags dict is always created so callers can rely on its presence
    assert "tags" in out


def test_handles_user_field_that_is_not_a_dict():
    """The Sentry schema technically allows `user: None`; don't choke on it."""
    event = {"user": None, "message": "x"}
    out = _before_send(event)
    assert out["message"] == "x"


def test_redacts_pii_inside_breadcrumbs_list():
    """Breadcrumbs are lists of dicts and the scrubber must walk them."""
    event = {
        "breadcrumbs": [
            {"message": "connected from 192.168.1.10"},
            {"message": "user alice@example.com signed in"},
        ]
    }
    out = _before_send(event)
    for crumb in out["breadcrumbs"]:
        assert "192.168.1.10" not in crumb["message"]
        assert "alice@example.com" not in crumb["message"]

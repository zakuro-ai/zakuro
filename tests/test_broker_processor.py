"""Tests for zakuro.processors.broker — auth, requirements and the balance API.

Broker calls are served by an httpx.MockTransport, so nothing here touches the
network; the assertions are on the exact request the SDK builds and on how it
interprets the broker's status codes and headers.
"""

from __future__ import annotations

import json

import cloudpickle
import httpx
import pytest

from zakuro import Compute
from zakuro.processors.base import ProcessorConfig
from zakuro.processors.broker import BrokerError, BrokerProcessor


def _make_processor(**compute_kwargs) -> BrokerProcessor:
    compute = Compute(host="127.0.0.1", port=9000, verify=False, **compute_kwargs)
    return BrokerProcessor(ProcessorConfig.from_uri("zc://127.0.0.1:9000"), compute)


def _wire(processor: BrokerProcessor, handler) -> BrokerProcessor:
    """Attach a mock transport to an already-constructed processor."""
    processor._client = httpx.Client(
        base_url="http://broker:9000", transport=httpx.MockTransport(handler)
    )
    processor._connected = True
    processor._user_id = "alice"
    return processor


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    """Broker auth is env-driven; start every test from a blank slate."""
    for var in ("ZAKURO_API_KEY", "ZAKURO_AUTH", "ZAKURO_USER"):
        monkeypatch.delenv(var, raising=False)


class TestConnect:
    """connect() resolves the user identity and the auth header."""

    def test_api_key_becomes_bearer_header(self, monkeypatch) -> None:
        """A ZAKURO_API_KEY is sent as Bearer auth, not as X-Zakuro-User."""
        monkeypatch.setenv("ZAKURO_API_KEY", "zk_alice_secret")
        proc = _make_processor()
        proc.connect()
        try:
            assert proc._client is not None
            assert proc._client.headers["Authorization"] == "Bearer zk_alice_secret"
            assert "x-zakuro-user" not in proc._client.headers
            assert proc._user_id == "alice"
        finally:
            proc.disconnect()

    def test_legacy_zakuro_auth_is_still_accepted(self, monkeypatch) -> None:
        """ZAKURO_AUTH remains a fallback for the api key."""
        monkeypatch.setenv("ZAKURO_AUTH", "zk_bob_secret")
        proc = _make_processor()
        proc.connect()
        try:
            assert proc._client is not None
            assert proc._client.headers["Authorization"] == "Bearer zk_bob_secret"
            assert proc._user_id == "bob"
        finally:
            proc.disconnect()

    def test_user_id_from_processor_options_wins_over_key(self, monkeypatch) -> None:
        """An explicit user_id is not overridden by the key-derived one."""
        monkeypatch.setenv("ZAKURO_API_KEY", "zk_alice_secret")
        proc = _make_processor(processor_options={"user_id": "carol"})
        proc.connect()
        try:
            assert proc._user_id == "carol"
        finally:
            proc.disconnect()

    def test_keyless_broker_identifies_with_x_zakuro_user(self, monkeypatch, tmp_path) -> None:
        """Without a key the SDK falls back to ZAKURO_USER and a plain header.

        `ZAKURO_HOME` is pointed at an empty directory because "without a key"
        now means without one in the environment AND without the credentials
        file `zc login` writes. Before the SDK read that file this test passed
        by accident on a logged-out machine and would have failed on a logged-in
        one; isolating the home makes the premise true rather than ambient.
        """
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
        monkeypatch.delenv("ZAKURO_AUTH", raising=False)
        monkeypatch.setenv("ZAKURO_USER", "dave")
        proc = _make_processor()
        proc.connect()
        try:
            assert proc._client is not None
            assert proc._client.headers["X-Zakuro-User"] == "dave"
            assert "authorization" not in proc._client.headers
        finally:
            proc.disconnect()

    def test_malformed_key_does_not_yield_a_user_id(self, monkeypatch) -> None:
        """A zk_ key with no trailing underscore segment cannot be split."""
        monkeypatch.setenv("ZAKURO_API_KEY", "zk_nounderscore")
        monkeypatch.setenv("ZAKURO_USER", "fallback")
        proc = _make_processor()
        proc.connect()
        try:
            assert proc._user_id == "fallback"
        finally:
            proc.disconnect()

    def test_connect_is_idempotent(self, monkeypatch) -> None:
        """A second connect() keeps the same client instead of leaking one."""
        proc = _make_processor()
        proc.connect()
        first = proc._client
        proc.connect()
        try:
            assert proc._client is first
        finally:
            proc.disconnect()

    def test_disconnect_clears_state(self) -> None:
        """disconnect() closes the client and flips is_connected back."""
        proc = _make_processor()
        proc.connect()
        proc.disconnect()
        assert proc._client is None
        assert not proc.is_connected


class TestExecute:
    """execute() packages the call and translates broker responses."""

    def test_requirements_header_carries_the_compute_spec(self) -> None:
        """cpus / memory / gpus and the option overrides ride in one JSON header."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["req"] = json.loads(request.headers["X-Zakuro-Requirements"])
            captured["payload"] = cloudpickle.loads(request.content)
            return httpx.Response(200, content=cloudpickle.dumps(9))

        proc = _wire(
            _make_processor(
                cpus=4,
                memory="2Gi",
                gpus=1,
                processor_options={
                    "estimated_duration_secs": 12.5,
                    "budget": "525",
                    "strategy": "cheapest",
                    "remote_only": 1,
                    "worker_type": "gpu",
                },
            ),
            handler,
        )

        assert proc.execute(cloudpickle.dumps(lambda a, b: a + b), (4,), {"b": 5}) == 9
        assert captured["req"] == {
            "cpus": 4,
            "memory_bytes": 2 * 1024**3,
            "gpus": 1,
            "estimated_duration_secs": 12.5,
            "budget": 525.0,
            "strategy": "cheapest",
            "remote_only": True,
            "worker_type": "gpu",
        }
        assert captured["payload"]["args"] == (4,)
        assert captured["payload"]["kwargs"] == {"b": 5}

    def test_cost_and_balance_are_read_from_response_headers(self) -> None:
        """The billing headers land on last_cost / last_balance."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=cloudpickle.dumps("done"),
                headers={"X-Zakuro-Cost": "37.5", "X-Zakuro-Balance": "14962.5"},
            )

        proc = _wire(_make_processor(memory="1Gi"), handler)
        assert proc.execute(cloudpickle.dumps(lambda: "done"), (), {}) == "done"
        assert proc.last_cost == 37.5
        assert proc.last_balance == 14962.5

    def test_unparsable_billing_headers_reset_to_zero(self) -> None:
        """A broker that sends junk in the cost headers must not break the call."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=cloudpickle.dumps(1),
                headers={"X-Zakuro-Cost": "n/a", "X-Zakuro-Balance": "n/a"},
            )

        proc = _wire(_make_processor(memory="1Gi"), handler)
        proc.last_cost = 5.0
        proc.last_balance = 5.0
        proc.execute(cloudpickle.dumps(lambda: 1), (), {})
        assert proc.last_cost == 0.0
        assert proc.last_balance == 0.0

    def test_worker_exception_is_reraised_client_side(self) -> None:
        """An exception in the payload is raised, not returned."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=cloudpickle.dumps(ValueError("worker said no")))

        proc = _wire(_make_processor(memory="1Gi"), handler)
        with pytest.raises(ValueError, match="worker said no"):
            proc.execute(cloudpickle.dumps(lambda: 1), (), {})

    @pytest.mark.parametrize(
        ("status", "message"),
        [
            (402, "Insufficient balance"),
            (429, "Rate limit exceeded"),
            (503, "No workers available"),
        ],
    )
    def test_broker_status_codes_map_to_readable_errors(self, status, message) -> None:
        """402/429/503 get a plain-English RuntimeError instead of an HTTP error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        proc = _wire(_make_processor(memory="1Gi"), handler)
        with pytest.raises(RuntimeError, match=message):
            proc.execute(cloudpickle.dumps(lambda: 1), (), {})

    def test_budget_required_carries_broker_message_code_and_hint(self) -> None:
        """A 400 BUDGET_REQUIRED is a BrokerError with the broker's text and a fix."""
        body = {"error": "Remote dispatch needs a budget: set one", "code": "BUDGET_REQUIRED"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=body)

        proc = _wire(_make_processor(memory="1Gi"), handler)
        with pytest.raises(BrokerError) as exc:
            proc.execute(cloudpickle.dumps(lambda: 1), (), {})
        assert exc.value.status == 400
        assert exc.value.code == "BUDGET_REQUIRED"
        assert exc.value.message == body["error"]
        assert "Remote dispatch needs a budget" in str(exc.value)
        assert 'processor_options={"budget": 10}' in str(exc.value)
        assert isinstance(exc.value, RuntimeError)

    def test_503_with_json_error_is_not_masked(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "cannot validate API key: hub unavailable"})

        proc = _wire(_make_processor(memory="1Gi"), handler)
        with pytest.raises(BrokerError, match="hub unavailable") as exc:
            proc.execute(cloudpickle.dumps(lambda: 1), (), {})
        assert exc.value.status == 503
        assert exc.value.code is None
        assert "No workers available" not in str(exc.value)

    def test_402_with_json_error_and_raw_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"error": "balance 0 yen", "code": "NO_FUNDS"})

        proc = _wire(_make_processor(memory="1Gi"), handler)
        with pytest.raises(BrokerError, match="balance 0 yen") as exc:
            proc.execute_payload(b"x")
        assert exc.value.code == "NO_FUNDS"

    def test_plain_400_becomes_broker_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="nope")

        proc = _wire(_make_processor(memory="1Gi"), handler)
        with pytest.raises(BrokerError, match="400") as exc:
            proc.execute(cloudpickle.dumps(lambda: 1), (), {})
        assert exc.value.status == 400

    def test_other_error_statuses_raise_http_status_error(self) -> None:
        """Anything else is surfaced by raise_for_status."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        proc = _wire(_make_processor(memory="1Gi"), handler)
        with pytest.raises(httpx.HTTPStatusError):
            proc.execute(cloudpickle.dumps(lambda: 1), (), {})

    def test_execute_requires_a_connection(self) -> None:
        """Using the processor outside its context manager is a clear error."""
        with pytest.raises(RuntimeError, match="not connected"):
            _make_processor(memory="1Gi").execute(b"", (), {})


class TestBalanceApi:
    """The balance / pricing helpers are thin wrappers over broker endpoints."""

    def test_get_balance_targets_the_resolved_user(self) -> None:
        """The balance endpoint is namespaced by the user id resolved at connect."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"user_id": "alice", "balance": 1800.0})

        proc = _wire(_make_processor(), handler)
        assert proc.get_balance() == {"user_id": "alice", "balance": 1800.0}
        assert seen["url"] == "http://broker:9000/balance/alice"

    def test_add_balance_posts_amount_and_description(self) -> None:
        """The deposit body carries both fields the broker ledger expects."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"balance": 3300.0})

        proc = _wire(_make_processor(), handler)
        assert proc.add_balance(1500.0, description="top-up") == {"balance": 3300.0}
        assert seen["url"] == "http://broker:9000/balance/alice/add"
        assert seen["body"] == {"amount": 1500.0, "description": "top-up"}

    def test_list_workers_unwraps_the_workers_key(self) -> None:
        """The broker wraps the fleet in {"workers": [...]}; the SDK unwraps it."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"workers": [{"name": "w1"}]})

        proc = _wire(_make_processor(), handler)
        assert proc.list_workers() == [{"name": "w1"}]

    def test_list_workers_defaults_to_empty(self) -> None:
        """A response without the key is an empty fleet, not a KeyError."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        proc = _wire(_make_processor(), handler)
        assert proc.list_workers() == []

    def test_estimate_price_converts_gib_to_bytes(self) -> None:
        """memory_gib is sent to /price as an integer byte count."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"min_cost": 15.0, "max_cost": 60.0})

        proc = _wire(_make_processor(), handler)
        assert proc.estimate_price(cpus=2.0, memory_gib=1.5, gpus=1, duration_secs=30.0) == {
            "min_cost": 15.0,
            "max_cost": 60.0,
        }
        assert seen["body"] == {
            "cpus": 2.0,
            "memory_bytes": int(1.5 * 1024**3),
            "gpus": 1,
            "estimated_duration_secs": 30.0,
        }

    def test_whoami_returns_the_broker_identity(self) -> None:
        """/me is the authenticated-identity probe used by `zakuro doctor`."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "http://broker:9000/me"
            return httpx.Response(200, json={"user_id": "alice", "local_mode": False})

        proc = _wire(_make_processor(), handler)
        assert proc.whoami()["user_id"] == "alice"

    @pytest.mark.parametrize(
        "method", ["get_balance", "add_balance", "list_workers", "estimate_price", "whoami"]
    )
    def test_balance_api_requires_a_connection(self, method) -> None:
        """Every helper refuses to run before connect()."""
        proc = _make_processor()
        args = (1.0,) if method == "add_balance" else ()
        with pytest.raises(RuntimeError, match="not connected"):
            getattr(proc, method)(*args)


class TestPing:
    """ping() is the health probe and must never raise."""

    def test_true_on_200(self) -> None:
        """A healthy broker answers 200 on /health."""
        proc = _wire(_make_processor(), lambda request: httpx.Response(200))
        assert proc.ping() is True

    def test_false_on_error_status(self) -> None:
        """A non-200 is unhealthy, not an exception."""
        proc = _wire(_make_processor(), lambda request: httpx.Response(500))
        assert proc.ping() is False

    def test_false_when_transport_fails(self) -> None:
        """A connection error is swallowed and reported as unhealthy."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        proc = _wire(_make_processor(), handler)
        assert proc.ping() is False

    def test_false_before_connect(self) -> None:
        """With no client at all the answer is still a plain False."""
        assert _make_processor().ping() is False

"""Tests for zakuro.worker.executor — the in-worker dispatch of cloudpickle payloads."""

from __future__ import annotations

import cloudpickle
import pytest

from zakuro.worker import executor as ex


def _roundtrip(payload: dict) -> object:
    """Run a request dict through execute_function and deserialize the reply."""
    return cloudpickle.loads(ex.execute_function(cloudpickle.dumps(payload)))


class Counter:
    """Stateful class used to exercise create_instance / call_method."""

    def __init__(self, start: int = 0) -> None:
        self.total = start

    def add(self, x: int, times: int = 1) -> int:
        self.total += x * times
        return self.total

    def boom(self) -> None:
        raise ValueError("kaboom")


def test_execute_runs_function_with_args_and_kwargs() -> None:
    """A plain payload with func/args/kwargs returns the function's value."""
    assert _roundtrip({"func": lambda a, b=0: a + b, "args": (2,), "kwargs": {"b": 3}}) == 5


def test_execute_defaults_args_and_kwargs_when_absent() -> None:
    """args/kwargs are optional; a zero-arg function still runs."""
    assert _roundtrip({"func": lambda: "ok"}) == "ok"


def test_explicit_execute_action_is_accepted() -> None:
    """action='execute' takes the same path as an implicit func payload."""
    assert _roundtrip({"action": "execute", "func": lambda: 7}) == 7


def test_function_exception_is_returned_not_raised() -> None:
    """Worker-side exceptions come back as a serialized exception object."""

    def explode() -> None:
        raise KeyError("missing")

    result = _roundtrip({"func": explode})
    assert isinstance(result, KeyError)
    assert "missing" in str(result)


def test_unknown_action_returns_value_error() -> None:
    """An unrecognised action is reported as a ValueError payload."""
    result = _roundtrip({"action": "teleport"})
    assert isinstance(result, ValueError)
    assert "Unknown action: teleport" in str(result)


def test_malformed_payload_returns_exception() -> None:
    """Garbage bytes are caught and returned as an exception, not propagated."""
    result = cloudpickle.loads(ex.execute_function(b"not-a-pickle"))
    assert isinstance(result, Exception)


def test_create_instance_honours_client_supplied_id() -> None:
    """A client-provided instance_id is used verbatim so the broker can pin affinity."""
    result = _roundtrip(
        {
            "action": "create_instance",
            "instance_id": "instance_client_pinned",
            "klass": Counter,
            "kwargs": {"start": 10},
        }
    )
    assert result == {"instance_id": "instance_client_pinned"}
    try:
        assert ex._get_instance("instance_client_pinned").total == 10
    finally:
        ex._cleanup_instance("instance_client_pinned")


def test_create_instance_autogenerates_id_when_client_sends_none() -> None:
    """Without a client id the worker allocates its own monotonic instance id."""
    result = _roundtrip({"action": "create_instance", "klass": Counter, "args": (1,)})
    assert isinstance(result, dict)
    instance_id = result["instance_id"]
    assert instance_id.startswith("instance_")
    try:
        assert ex._get_instance(instance_id).total == 1
    finally:
        ex._cleanup_instance(instance_id)


def test_call_method_mutates_the_stored_instance() -> None:
    """call_method operates on the live instance, so state persists across calls."""
    created = _roundtrip(
        {
            "action": "create_instance",
            "instance_id": "instance_stateful",
            "klass": Counter,
            "kwargs": {"start": 100},
        }
    )
    assert created == {"instance_id": "instance_stateful"}
    try:
        assert (
            _roundtrip(
                {
                    "action": "call_method",
                    "instance_id": "instance_stateful",
                    "method": "add",
                    "args": (2,),
                    "kwargs": {"times": 3},
                }
            )
            == 106
        )
        assert (
            _roundtrip(
                {
                    "action": "call_method",
                    "instance_id": "instance_stateful",
                    "method": "add",
                    "args": (1,),
                }
            )
            == 107
        )
    finally:
        ex._cleanup_instance("instance_stateful")


def test_call_method_on_unknown_instance_returns_value_error() -> None:
    """Calling into an evicted or never-created instance is an error payload."""
    result = _roundtrip(
        {"action": "call_method", "instance_id": "instance_nope", "method": "add", "args": (1,)}
    )
    assert isinstance(result, ValueError)
    assert "Instance not found: instance_nope" in str(result)


def test_call_method_propagates_method_exception_as_payload() -> None:
    """An exception raised inside the method is serialized back to the client."""
    _roundtrip({"action": "create_instance", "instance_id": "instance_boom", "klass": Counter})
    try:
        result = _roundtrip(
            {"action": "call_method", "instance_id": "instance_boom", "method": "boom"}
        )
        assert isinstance(result, ValueError)
        assert "kaboom" in str(result)
    finally:
        ex._cleanup_instance("instance_boom")


def test_store_and_cleanup_instance_helpers() -> None:
    """_store_instance hands out unique ids; _cleanup_instance is idempotent."""
    first = ex._store_instance(object())
    second = ex._store_instance(object())
    assert first != second
    ex._cleanup_instance(first)
    ex._cleanup_instance(first)  # second removal must not raise
    with pytest.raises(ValueError, match="Instance not found"):
        ex._get_instance(first)
    ex._cleanup_instance(second)


@pytest.fixture
def clean_store(monkeypatch: pytest.MonkeyPatch):
    """Isolate the module-global instance store for lifecycle tests."""
    for k in list(ex._instances):
        ex._cleanup_instance(k)
    yield monkeypatch
    for k in list(ex._instances):
        ex._cleanup_instance(k)


def test_create_past_cap_is_refused(clean_store) -> None:
    clean_store.setenv("ZAKURO_CLS_MAX_INSTANCES", "2")
    ex._store_instance(object())
    ex._store_instance(object())
    with pytest.raises(ex.InstanceStoreFullError):
        ex._store_instance(object())
    assert len(ex._instances) == 2
    # replacing an existing id does not grow the store
    existing = next(iter(ex._instances))
    ex._put_instance(existing, object())
    # via the protocol the failure is returned as a pickled exception
    result = _roundtrip({"action": "create_instance", "klass": dict})
    assert isinstance(result, ex.InstanceStoreFullError)


def test_idle_instance_is_evicted_and_not_found(clean_store) -> None:
    clean_store.setenv("ZAKURO_CLS_IDLE_TTL_S", "10")
    now = [1000.0]
    clean_store.setattr(ex.time, "monotonic", lambda: now[0])
    iid = ex._store_instance(object())
    now[0] += 5
    ex._get_instance(iid)  # touch refreshes the idle timer
    now[0] += 9
    ex._get_instance(iid)
    now[0] += 11
    with pytest.raises(ex.InstanceNotFoundError, match="expired or evicted"):
        ex._get_instance(iid)
    assert iid not in ex._instances


def test_expired_evicted_before_cap_refusal(clean_store) -> None:
    clean_store.setenv("ZAKURO_CLS_MAX_INSTANCES", "1")
    clean_store.setenv("ZAKURO_CLS_IDLE_TTL_S", "10")
    now = [0.0]
    clean_store.setattr(ex.time, "monotonic", lambda: now[0])
    old = ex._store_instance(object())
    now[0] += 11
    new = ex._store_instance(object())  # old expired -> evicted, not refused
    assert old not in ex._instances and new in ex._instances


def test_release_instance_action(clean_store) -> None:
    iid = ex._store_instance(object())
    result = _roundtrip({"action": "release_instance", "instance_id": iid})
    assert result == {"released": True}
    assert iid not in ex._instances


def test_a_payload_the_worker_cannot_load_comes_back_flagged():
    # The worker lacks a module the call needs: that is this worker failing,
    # not the user's code, so the SDK may send the call to another worker.
    from zakuro.errors import is_load_failure

    payload = b"cno_such_module_zk\nThing\n."  # a pickle naming a module this worker lacks
    exc = cloudpickle.loads(ex.execute_function(payload))
    assert isinstance(exc, ModuleNotFoundError)
    assert is_load_failure(exc)

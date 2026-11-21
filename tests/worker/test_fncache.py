"""The worker loads each function once, keyed by its bytes (Wave 0, task 3).

The SDK embeds the function's pickled bytes as a ``pickle.loads(bytes)``
reduction (``zakuro.processors._payload``). The worker's unpickler sends that
call to a cache keyed by the hash of the bytes, so an unchanged function is not
unpickled on every call. An old-style payload (the function object itself) and
a change to the function's globals (new bytes) still load as before.
"""

from __future__ import annotations

import cloudpickle
import pytest

from zakuro.processors._payload import build_execute_payload
from zakuro.worker import fncache
from zakuro.worker.executor import execute_function


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    monkeypatch.delenv("ZAKURO_FN_CACHE", raising=False)
    monkeypatch.delenv("ZAKURO_FN_CACHE_SIZE", raising=False)
    fncache.clear()
    yield
    fncache.clear()


def _by_value(source: str, name: str, **globals_: object):
    """A function pickled by value with its own globals, as one defined in a
    notebook or a script's ``__main__`` is."""
    ns: dict[str, object] = {"__name__": "zc_fncache_test_dynamic", **globals_}
    exec(source, ns)
    return ns[name]


def _add(a: int, b: int) -> int:
    return a + b


def _run(payload: bytes):
    data = fncache.loads_payload(payload)
    return data["func"](*data["args"], **data["kwargs"])


def test_the_same_function_bytes_load_once():
    func_bytes = cloudpickle.dumps(_add)
    assert _run(build_execute_payload(func_bytes, (2, 3), {})) == 5
    assert _run(build_execute_payload(func_bytes, (4, 5), {})) == 9
    assert fncache.cache_info() == (1, 1)


def test_changed_globals_are_a_different_function():
    over = _by_value("def over(x):\n    return x > THRESHOLD\n", "over", THRESHOLD=5)
    assert _run(build_execute_payload(cloudpickle.dumps(over), (3,), {})) is False
    over.__globals__["THRESHOLD"] = 1
    assert _run(build_execute_payload(cloudpickle.dumps(over), (3,), {})) is True
    assert fncache.cache_info() == (0, 2)


def test_an_old_style_payload_still_runs():
    payload = cloudpickle.dumps({"func": _add, "args": (2, 3), "kwargs": {}})
    assert _run(payload) == 5
    assert fncache.cache_info() == (0, 0)


def test_a_function_keeps_its_globals_between_calls(monkeypatch):
    bump = _by_value(
        "def bump():\n    global COUNT\n    COUNT += 1\n    return COUNT\n", "bump", COUNT=0
    )
    payload = build_execute_payload(cloudpickle.dumps(bump), (), {})
    assert _run(payload) == 1
    assert _run(payload) == 2  # one cached function, one globals dict, as Ray keeps it
    monkeypatch.setenv("ZAKURO_FN_CACHE", "0")
    assert _run(payload) == 1  # a fresh load per call, as before
    assert _run(payload) == 1


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setenv("ZAKURO_FN_CACHE_SIZE", "2")
    funcs = [_by_value(f"def f{i}():\n    return {i}\n", f"f{i}") for i in range(3)]
    payloads = [build_execute_payload(cloudpickle.dumps(f), (), {}) for f in funcs]
    for p in payloads:
        _run(p)
    assert fncache.cache_info() == (0, 3)
    _run(payloads[0])  # evicted: loads again
    assert fncache.cache_info() == (0, 4)


def test_the_executor_uses_the_cache():
    payload = build_execute_payload(cloudpickle.dumps(_add), (2, 3), {})
    assert cloudpickle.loads(execute_function(payload)) == 5
    assert cloudpickle.loads(execute_function(payload)) == 5
    assert fncache.cache_info() == (1, 1)

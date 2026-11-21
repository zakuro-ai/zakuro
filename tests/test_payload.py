"""The function travels as the bytes the SDK pickled (Wave 0, task 2).

``BrokerProcessor.execute`` used to unpickle the function it was handed and
pickle it again. The payload now embeds those bytes as they are, in a form
any worker loads with a plain ``cloudpickle.loads``.
"""

from __future__ import annotations

import cloudpickle
import pytest

from tests.test_http_pool import _Stub
from zakuro.processors import _http_pool
from zakuro.processors._payload import build_execute_payload


def _add(a: int, b: int) -> int:
    return a + b


@pytest.fixture(autouse=True)
def _fresh_pool():
    _http_pool.close_all()
    yield
    _http_pool.close_all()


def test_an_old_worker_loads_the_payload_unchanged():
    payload = build_execute_payload(cloudpickle.dumps(_add), (2, 3), {})
    data = cloudpickle.loads(payload)
    assert data["func"](*data["args"], **data["kwargs"]) == 5


def test_the_function_bytes_travel_verbatim():
    func_bytes = cloudpickle.dumps(_add)
    assert func_bytes in build_execute_payload(func_bytes, (2, 3), {})


def test_execute_does_not_unpickle_the_function(monkeypatch):
    import zakuro.mesh
    import zakuro.processors.broker as broker
    from zakuro.compute import Compute
    from zakuro.processors.base import ProcessorConfig

    monkeypatch.setattr(zakuro.mesh, "api_key", lambda: None)
    func_bytes = cloudpickle.dumps(_add)
    real_loads = cloudpickle.loads

    def loads(data: bytes, *a, **kw):
        if data == func_bytes:
            raise AssertionError("the function was unpickled in the SDK")
        return real_loads(data, *a, **kw)

    monkeypatch.setattr(broker.cloudpickle, "loads", loads)
    stub = _Stub()
    try:
        config = ProcessorConfig.from_uri(f"zc://127.0.0.1:{stub.port}")
        proc = broker.BrokerProcessor(
            config, Compute(uri=f"zc://127.0.0.1:{stub.port}", verify=False)
        )
        with proc:
            assert proc.execute(func_bytes, (2, 3), {}) == 5
    finally:
        stub.stop()

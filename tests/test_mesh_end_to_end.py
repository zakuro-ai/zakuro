"""End-to-end: `@zk.fn` → `.to(Compute(uri="zc://node-<fp>"))` → a real answer.

`test_mesh_nodes.py` pins the pieces in isolation. These pin the thing the
feature actually promises, over a real socket, against stub brokers that speak
what a node's broker speaks:

    import zakuro as zk
import zakuro.mesh  # noqa: F401  (for zk.mesh.reset_cache)

    @zk.fn
    def square(x): return x * x

    square.to(zk.Compute(uri=node.uri))(7)   # -> 49

Nothing here touches the mesh. Two stub brokers bind loopback, a temporary
roster points at them, and `ZAKURO_HOME` is redirected — so CI gets the same
answer as a developer with a live mesh, and a developer who happens to be
logged in does not get a different one.

The guarantee worth defending is **which node ran it**. A worker is not
addressable, and asking a broker to pin with `target_node` is refused for a
peer and silently ignored for your own node. Addressing the node's own broker
is what makes placement real, so `test_addressing_node_b_reaches_b_not_a`
is the load-bearing test in this file.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import cloudpickle
import pytest

import zakuro as zk


class _StubBroker:
    """A node's broker, reduced to the three routes the SDK uses.

    `/execute` really runs the payload, so a passing test means the whole
    round trip worked -- pickled in, executed, pickled out -- rather than that
    a mock was called.
    """

    def __init__(
        self, name: str, fingerprint: str, version: str = "0.0.34", owner: str | None = "acct-1"
    ) -> None:
        self.name = name
        self.fingerprint = fingerprint
        self.version = version
        self.owner = owner
        self.requests: list[dict] = []
        self.instances: dict[str, object] = {}

        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # Name the worker, as a broker does: this is what a caller
                # checks a placement against.
                self.send_header("X-Zakuro-Worker", f"{stub.name}-w0")
                self.send_header("X-Zakuro-Cost", "0.001234")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/health"):
                    health = {
                        "service": "zakuro-broker",
                        "status": "healthy",
                        "node_id": f"zc://node-{stub.fingerprint}",
                        "node_name": stub.name,
                        "owner_user_id": stub.owner,
                    }
                    if stub.version is not None:
                        health["build"] = {"version": stub.version, "git": "abc1234"}
                    self._send(json.dumps(health).encode(), "application/json")
                elif self.path.startswith("/workers"):
                    self._send(
                        json.dumps(
                            {
                                "total": 1,
                                "workers": [
                                    {
                                        "name": f"{stub.name}-w0",
                                        "node": f"zc://node-{stub.fingerprint}",
                                        "status": "healthy",
                                    }
                                ],
                            }
                        ).encode(),
                        "application/json",
                    )
                else:
                    self._send(b"{}", "application/json", 404)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length)
                stub.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "user": self.headers.get("X-Zakuro-User"),
                        "requirements": json.loads(
                            self.headers.get("X-Zakuro-Requirements") or "{}"
                        ),
                        "payload": payload,
                    }
                )
                call = cloudpickle.loads(payload)
                action = call.get("action")
                if action == "create_instance":
                    # Instance affinity, as the broker implements it: the
                    # object is built once and kept, keyed by instance_id, so
                    # later calls land on the same one.
                    iid = call["instance_id"]
                    stub.instances[iid] = call["klass"](*call["args"], **call.get("kwargs", {}))
                    self._send(
                        cloudpickle.dumps({"instance_id": iid}),
                        "application/octet-stream",
                    )
                    return
                if action == "call_method":
                    obj = stub.instances.get(call["instance_id"])
                    if obj is None:
                        self._send(
                            cloudpickle.dumps(KeyError(f"unknown instance {call['instance_id']}")),
                            "application/octet-stream",
                        )
                        return
                    # The contract is a cloudpickle of a result OR an
                    # exception; a worker never lets one escape as a 500.
                    try:
                        method = getattr(obj, call["method"])
                        out = method(*call["args"], **call.get("kwargs", {}))
                    except Exception as exc:  # noqa: BLE001 - mirrors the worker
                        out = exc
                    self._send(cloudpickle.dumps(out), "application/octet-stream")
                    return
                result = call["func"](*call["args"], **call.get("kwargs", {}))
                self._send(cloudpickle.dumps(result), "application/octet-stream")

            def log_message(self, *a):  # noqa: A002 - silence the test log
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"127.0.0.1:{self._server.server_address[1]}"
        # A short poll interval so `shutdown()` returns promptly: the 0.5s
        # default is waited out per server on every teardown, which on this
        # file alone was ten seconds of CI doing nothing.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    @property
    def uri(self) -> str:
        return f"zc://node-{self.fingerprint}"

    def compute_for_test(self, **kwargs):
        """A Compute addressing this stub, with no discovery round trip."""
        from zakuro.mesh import NodeInfo

        node = NodeInfo(
            endpoint=self.endpoint,
            node_id=f"zc://node-{self.fingerprint}",
            name=self.name,
        )
        return node.compute(**kwargs)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def mesh(monkeypatch, tmp_path):
    """Two stub nodes, a roster naming them, and an isolated ZAKURO_HOME."""
    a = _StubBroker("node-alpha", "aaaaaaaaaaaaaaaa")
    b = _StubBroker("node-beta", "bbbbbbbbbbbbbbbb")
    (tmp_path / "roster.json").write_text(
        json.dumps(
            {
                "entries": [],
                "endpoints": [["pk-a", a.endpoint], ["pk-b", b.endpoint]],
            }
        )
    )
    (tmp_path / "credentials").write_text("api_key: zk_tester_secret\n")
    monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
    monkeypatch.delenv("ZAKURO_AUTH", raising=False)
    # `_endpoints` probes loopback 9000-9004 for a local broker before the
    # roster. On a developer machine running one, that would add a real node
    # to a test meant to see exactly two.
    monkeypatch.setattr("zakuro.mesh._LOCAL_BROKER_PORTS", ())
    # Discovery is cached module-wide, so one test's stub nodes would
    # otherwise still be "reachable" in the next one.
    zk.mesh.reset_cache()
    try:
        yield a, b
    finally:
        zk.mesh.reset_cache()
        a.stop()
        b.stop()


@zk.fn
def square(x: int) -> int:
    return x * x


@zk.fn
def add(a: int, b: int = 0) -> int:
    return a + b


class TestDiscovery:
    def test_nodes_reports_what_each_broker_discloses(self, mesh):
        a, b = mesh
        found = {n.name: n for n in zk.nodes()}

        assert set(found) == {"node-alpha", "node-beta"}
        alpha = found["node-alpha"]
        assert alpha.fingerprint == a.fingerprint
        assert alpha.zc_version == "0.0.34"
        assert alpha.owner_user_id == "acct-1"
        # Its own workers, which is what a placement is checked against.
        assert alpha.workers == {"node-alpha-w0"}

    def test_a_node_disclosing_no_build_is_dated_not_blank(self, monkeypatch, tmp_path):
        old = _StubBroker("ancient", "cccccccccccccccc", version=None)
        (tmp_path / "roster.json").write_text(
            json.dumps(
                {
                    "endpoints": [["pk", old.endpoint]],
                }
            )
        )
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        monkeypatch.setattr("zakuro.mesh._LOCAL_BROKER_PORTS", ())
        zk.mesh.reset_cache()
        try:
            (node,) = zk.nodes()
            # Absence of a build block places the node before the release that
            # added one. "unknown" would throw that away.
            assert node.zc_version == "<pre-#305>"
        finally:
            old.stop()


class TestTheDocumentedOneLiner:
    def test_square_seven_is_forty_nine(self, mesh):
        a, _ = mesh
        assert square.to(zk.Compute(uri=a.uri))(7) == 49

    def test_args_and_kwargs_both_survive_the_round_trip(self, mesh):
        a, _ = mesh
        assert add.to(zk.Compute(uri=a.uri))(3, b=4) == 7

    def test_the_call_is_a_cloudpickled_func_args_kwargs(self, mesh):
        a, _ = mesh
        add.to(zk.Compute(uri=a.uri))(2, b=5)
        sent = cloudpickle.loads(a.requests[-1]["payload"])
        assert set(sent) >= {"func", "args", "kwargs"}
        assert sent["args"] == (2,)
        assert sent["kwargs"] == {"b": 5}


class TestPlacement:
    """The guarantee: the node you address is the node that runs it."""

    def test_addressing_node_b_reaches_b_not_a(self, mesh):
        a, b = mesh

        square.to(zk.Compute(uri=b.uri))(5)

        assert len(b.requests) == 1, "the addressed node must receive it"
        assert a.requests == [], "the other node must not"
        assert b.requests[0]["path"] == "/execute"

    def test_each_node_can_be_addressed_in_turn(self, mesh):
        a, b = mesh
        for node in zk.nodes():
            square.to(zk.Compute(uri=node.uri))(3)
        assert len(a.requests) == 1
        assert len(b.requests) == 1

    def test_an_unknown_fingerprint_names_what_is_reachable(self, mesh):
        # The usual cause is a fingerprint copied from a stale listing, so the
        # error is only useful if it carries the current one.
        with pytest.raises(LookupError) as excinfo:
            zk.Compute(uri="zc://node-" + "f" * 16)
        message = str(excinfo.value)
        assert "node-alpha=aaaaaaaaaaaa" in message
        assert "zakuro.nodes()" in message


class TestResolutionCost:
    """Resolving must not rescan the mesh per task, or be flaky when it does."""

    def test_a_loop_over_nodes_does_not_rescan_per_task(self, mesh):
        a, b = mesh
        zk.mesh.reset_cache()
        scans = {"n": 0}
        real = zk.mesh._endpoints

        def counted():
            scans["n"] += 1
            return real()

        import zakuro.mesh as m

        m._endpoints = counted
        try:
            found = zk.nodes()
            for node in found:
                square.to(zk.Compute(uri=node.uri))(1)
        finally:
            m._endpoints = real

        # One scan for the listing; the per-task resolves come from the cache.
        # Before caching this was one full mesh scan PER TASK.
        assert scans["n"] == 1, f"rescanned {scans['n']} times"

    def test_a_node_handed_to_you_needs_no_lookup_at_all(self, mesh):
        a, b = mesh
        zk.mesh.reset_cache()
        found = zk.nodes()

        import zakuro.mesh as m

        def explode():  # pragma: no cover - must not be reached
            raise AssertionError("NodeInfo.compute() must not trigger discovery")

        real = m._endpoints
        m._endpoints = explode
        try:
            zk.mesh.reset_cache()
            # A NodeInfo already carries its endpoint. This is the loop-safe
            # form: no discovery, so a transient probe timeout cannot make a
            # node you are already holding "disappear".
            assert square.to(found[0].compute())(7) == 49
        finally:
            m._endpoints = real

    def test_a_node_that_appears_late_is_found_on_a_rescan(self, mesh, monkeypatch, tmp_path):
        a, b = mesh
        zk.mesh.reset_cache()
        zk.nodes()  # warm the cache with two nodes

        late = _StubBroker("node-late", "dddddddddddddddd")
        try:
            (tmp_path / "roster.json").write_text(
                json.dumps({"endpoints": [["pk-a", a.endpoint], ["pk-late", late.endpoint]]})
            )
            # Not in the cached view. `resolve_node` rescans once before
            # declaring it unknown, because "joined a moment ago" and "does
            # not exist" are different answers.
            assert square.to(zk.Compute(uri=late.uri))(9) == 81
        finally:
            late.stop()


class TestWhatGoesOnTheWire:
    def test_the_credentials_file_authenticates_the_call(self, mesh):
        a, _ = mesh
        square.to(zk.Compute(uri=a.uri))(2)
        # Read from ZAKURO_HOME/credentials, with nothing in the environment.
        # Without this the broker sees an anonymous caller, and on a
        # billing-enabled fleet that is refused or stalls.
        assert a.requests[-1]["authorization"] == "Bearer zk_tester_secret"

    def test_a_compute_with_no_memory_still_sends_a_requirement(self, mesh):
        a, _ = mesh
        # The regression this guards: `Compute.memory` defaults to None and
        # `memory_bytes()` raises on None, so every `zc://` Compute without an
        # explicit memory= died before sending anything.
        square.to(zk.Compute(uri=a.uri))(4)
        requirements = a.requests[-1]["requirements"]
        assert requirements["memory_bytes"] > 0
        assert requirements["cpus"] > 0

    def test_an_explicit_memory_is_what_gets_sent(self, mesh):
        a, _ = mesh
        square.to(zk.Compute(uri=a.uri, memory="1Gi"))(4)
        assert a.requests[-1]["requirements"]["memory_bytes"] == 1024**3

    def test_a_loopback_node_is_not_dialled_through_the_mesh_proxy(self, mesh, monkeypatch):
        a, _ = mesh
        # If loopback were proxied, this unroutable proxy would break the call.
        # Talking to a local broker must not leave the machine.
        monkeypatch.setenv("ZC_MESH_PROXY", "127.0.0.1:1")
        assert square.to(zk.Compute(uri=a.uri))(6) == 36


@zk.cls
class Counter:
    """Stateful on purpose: `n` surviving between calls is the whole feature."""

    def __init__(self, label: str):
        self.label = label
        self.n = 0

    def bump(self, by: int = 1) -> int:
        self.n += by
        return self.n

    def name(self) -> str:
        return self.label


class TestRemoteClasses:
    """`@zk.cls` over a broker -- which used to raise NotImplementedError.

    The zc broker has always spoken instance affinity (`X-Zakuro-Instance-
    Action` / `-Id`, pinned to the worker that created the object). Only the
    SDK side was missing, so a class worked over a direct HTTP worker and not
    over a broker -- which is every mesh node.
    """

    def test_a_class_can_be_instantiated_on_a_node(self, mesh):
        a, _ = mesh
        counter = Counter.to(a.compute_for_test())("alpha")
        assert counter.name() == "alpha"

    def test_state_survives_between_calls(self, mesh):
        a, _ = mesh
        counter = Counter.to(a.compute_for_test())("alpha")
        # If each call were placed independently this stays at 1 forever.
        assert counter.bump() == 1
        assert counter.bump() == 2
        assert counter.bump(by=5) == 7

    def test_two_instances_do_not_share_state(self, mesh):
        a, _ = mesh
        one = Counter.to(a.compute_for_test())("one")
        two = Counter.to(a.compute_for_test())("two")
        one.bump()
        one.bump()
        # Affinity is per instance id, not per worker: a second object on the
        # same node must be a different object.
        assert two.bump() == 1
        assert one.bump() == 3

    def test_the_instance_lives_on_the_node_it_was_addressed_to(self, mesh):
        a, b = mesh
        counter = Counter.to(b.compute_for_test())("beta")
        counter.bump()
        # Creation and every method call go to B, and A sees nothing.
        assert a.requests == []
        actions = [r["path"] for r in b.requests]
        assert len(actions) >= 2

    def test_the_compute_spec_reaches_the_broker(self, mesh):
        a, _ = mesh
        Counter.to(a.compute_for_test(gpus=2, memory="2Gi"))("spec")
        # Requirements used to be omitted entirely on the class path, so
        # `Compute(gpus=1)` was silently ignored and an instance could land on
        # a CPU-only worker.
        requirements = a.requests[-1]["requirements"]
        assert requirements["gpus"] == 2
        assert requirements["memory_bytes"] == 2 * 1024**3

    def test_a_method_that_raises_comes_back_as_an_exception(self, mesh):
        a, _ = mesh
        counter = Counter.to(a.compute_for_test())("alpha")
        with pytest.raises(AttributeError):
            counter.no_such_method()

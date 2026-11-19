"""Addressing nodes: `zc://node-<fingerprint>` and `zakuro.nodes()`.

A worker is not addressable on this mesh -- it binds loopback, and the
`<peer-ip>:3960` address discovery invents for it is a guess that was wrong on
three of ten nodes measured. Asking a broker to pin work with `target_node`
does not help either: refused for a peer, silently ignored for your own node.
What is addressable is a node, through its own broker, which is what these pin.
"""

from __future__ import annotations

import json

import pytest

from zakuro.compute import Compute, _is_node_uri
from zakuro.mesh import NodeInfo, api_key, resolve_node


class TestNodeUriShape:
    """A node id must be told apart from a hostname without a network call."""

    def test_a_fingerprint_is_a_node_uri(self):
        assert _is_node_uri("zc://node-e18d8c0ed6ec7b5a")
        assert _is_node_uri("zc://node-e18d8c0ed6ec7b5a/")

    def test_an_address_is_not(self):
        assert not _is_node_uri("zc://localhost:9000")
        assert not _is_node_uri("zc://10.13.13.215:9000")
        assert not _is_node_uri("quic://worker:4433")

    def test_a_host_merely_called_node_something_is_not(self):
        # Matched on the SHAPE of the fingerprint: 16 hex characters. A machine
        # named "node-alpha" is a host, and treating it as a node id would send
        # a perfectly good hostname through a lookup that must fail.
        assert not _is_node_uri("zc://node-alpha")
        assert not _is_node_uri("zc://node-")
        assert not _is_node_uri("zc://node-notavalidhexx")
        assert not _is_node_uri("zc://node-e18d8c0ed6ec7b5")  # 15 chars
        assert not _is_node_uri("zc://node-e18d8c0ed6ec7b5aa")  # 17 chars

    def test_uppercase_hex_is_not_a_fingerprint(self):
        # zc prints fingerprints lowercase; accepting both spellings would mean
        # two ids for one node and a cache that misses half the time.
        assert not _is_node_uri("zc://node-E18D8C0ED6EC7B5A")


class TestResolution:
    """Resolving a node id to the broker that can actually run the work."""

    def test_a_node_uri_becomes_that_nodes_broker(self, monkeypatch):
        node = NodeInfo(
            endpoint="10.13.13.215:9000",
            node_id="zc://node-e18d8c0ed6ec7b5a",
            name="zc-worker-alpha-1",
        )
        monkeypatch.setattr("zakuro.mesh.nodes", lambda timeout=4.0, refresh=False: [node])

        c = Compute(uri="zc://node-e18d8c0ed6ec7b5a")

        assert c.host == "10.13.13.215"
        assert c.port == 9000
        # The URI is rewritten too: everything downstream re-parses it, and a
        # fingerprint left in the authority is read as a hostname -- which
        # failed as a DNS error naming the user's perfectly valid node id.
        assert c.uri == "zc://10.13.13.215:9000"
        assert c._node is node

    def test_an_unknown_fingerprint_says_what_is_reachable(self, monkeypatch):
        monkeypatch.setattr(
            "zakuro.mesh.nodes",
            lambda timeout=4.0, refresh=False: [
                NodeInfo(
                    endpoint="10.13.13.215:9000",
                    node_id="zc://node-e18d8c0ed6ec7b5a",
                    name="alpha-1",
                )
            ],
        )
        # The usual cause is a stale fingerprint copied from an older listing,
        # so the error is only useful if it carries the current one.
        with pytest.raises(LookupError) as e:
            resolve_node("zc://node-0000000000000000")
        assert "alpha-1=e18d8c0ed6ec" in str(e.value)
        assert "zakuro.nodes()" in str(e.value)

    def test_an_address_uri_is_left_alone(self, monkeypatch):
        def explode(**_):  # pragma: no cover - must not be reached
            raise AssertionError("an address must not be resolved as a node")

        monkeypatch.setattr("zakuro.mesh.nodes", explode)
        c = Compute(uri="zc://127.0.0.1:9000", verify=False)
        assert c.host == "127.0.0.1"
        assert c._node is None


class TestAutoWiring:
    """What makes `import zakuro` enough in a plain shell."""

    def test_the_api_key_is_read_from_the_credentials_file(self, monkeypatch, tmp_path):
        # Without this the broker sees an anonymous caller, and on a
        # billing-enabled fleet every request to a priced worker is refused --
        # which reads as a dead mesh rather than a missing key.
        monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
        monkeypatch.delenv("ZAKURO_AUTH", raising=False)
        (tmp_path / "credentials").write_text(
            "api_url: https://stg.api.zakuro-ai.com\napi_key: zk_secret_value\n"
        )
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        assert api_key() == "zk_secret_value"

    def test_the_environment_wins_over_the_file(self, monkeypatch, tmp_path):
        (tmp_path / "credentials").write_text("api_key: from_file\n")
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        monkeypatch.setenv("ZAKURO_API_KEY", "from_env")
        assert api_key() == "from_env"

    def test_no_credentials_is_none_not_an_empty_string(self, monkeypatch, tmp_path):
        # An empty Bearer header is rejected by the broker; None falls through
        # to the keyless path instead, which is a different and working thing.
        monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
        monkeypatch.delenv("ZAKURO_AUTH", raising=False)
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        assert api_key() is None

    def test_loopback_never_goes_through_the_mesh_proxy(self, monkeypatch):
        from zakuro import mesh

        monkeypatch.setattr(mesh, "mesh_proxy_url", lambda: "http://127.0.0.1:18888")
        # Routing loopback through the sidecar means leaving the host and
        # coming back -- slower, and broken when the sidecar is down, for the
        # one node reachable without a mesh at all.
        assert mesh.proxy_for("127.0.0.1") is None
        assert mesh.proxy_for("localhost") is None
        assert mesh.proxy_for("10.13.13.215") == "http://127.0.0.1:18888"


class TestBrokerRequirements:
    """The request the broker processor actually sends."""

    def test_a_compute_with_no_memory_still_asks_for_some(self):
        # `Compute.memory` defaults to None and `memory_bytes()` raises on None
        # by design. The broker processor called it unconditionally, so EVERY
        # `Compute(uri="zc://...")` without an explicit memory= died with
        # "Compute.memory is not set" before sending anything.
        from zakuro.processors.broker import DEFAULT_MEMORY_BYTES

        c = Compute(uri="zc://127.0.0.1:9000", verify=False)
        assert c.memory is None
        assert DEFAULT_MEMORY_BYTES > 0

    def test_an_explicit_memory_is_honoured(self):
        c = Compute(uri="zc://127.0.0.1:9000", memory="1Gi", verify=False)
        assert c.memory_bytes() == 1024**3


class TestNodeInfo:
    def test_a_node_addresses_itself_by_fingerprint(self):
        n = NodeInfo(
            endpoint="10.13.13.215:9000", node_id="zc://node-e18d8c0ed6ec7b5a", name="alpha-1"
        )
        assert n.fingerprint == "e18d8c0ed6ec7b5a"
        assert n.uri == "zc://node-e18d8c0ed6ec7b5a"

    def test_a_node_disclosing_no_build_is_dated_not_unknown(self):
        # Absence of a build block places the node before the release that
        # added one. That is an answer; "unknown" would throw it away.
        from zakuro import mesh

        health = {
            "service": "zakuro-broker",
            "node_id": "zc://node-" + "a" * 16,
            "node_name": "old",
            "build": None,
        }
        version = (health.get("build") or {}).get("version") or "<pre-#305>"
        assert version == "<pre-#305>"
        assert mesh.NodeInfo(endpoint="x", zc_version=version).zc_version == "<pre-#305>"


def test_roster_is_read_from_the_cache_so_this_works_offline(monkeypatch, tmp_path):
    (tmp_path / "roster.json").write_text(
        json.dumps(
            {
                "entries": [],
                "endpoints": [["pk1", "10.13.13.215:9000"], ["pk2", ""]],
            }
        )
    )
    monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
    from zakuro import mesh

    monkeypatch.setattr(mesh, "_get_json", lambda *a, **k: None)
    # The empty endpoint is dropped, not probed: an authorized node with no
    # tunnel up is normal, not malformed.
    assert mesh._endpoints() == ["10.13.13.215:9000"]


def test_endpoints_are_deduplicated_in_order(monkeypatch, tmp_path):
    (tmp_path / "roster.json").write_text(
        json.dumps(
            {
                "endpoints": [
                    ["a", "10.0.0.2:9000"],
                    ["b", "10.0.0.1:9000"],
                    ["c", "10.0.0.2:9000"],
                ],
            }
        )
    )
    monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
    from zakuro import mesh

    monkeypatch.setattr(mesh, "_get_json", lambda *a, **k: None)
    assert mesh._endpoints() == ["10.0.0.2:9000", "10.0.0.1:9000"]


class TestHubRoster:
    """A fresh install has no local broker and no roster.json.

    After `zc login` + `zc connect` the only list of nodes is the hub's roster,
    the source `zc brokers` reads first. Without it `nodes()` was empty, so the
    README's "`zk.nodes()` lists them" failed on every new machine.
    """

    ROWS = [
        {"node_pubkey": "a", "mesh_endpoint": "10.13.13.40:9000", "revoked": False},
        {"node_pubkey": "b", "mesh_endpoint": "10.13.13.41:9000"},
        {"node_pubkey": "c", "mesh_endpoint": "10.13.13.42:9000", "revoked": True},
        {"node_pubkey": "d", "mesh_endpoint": ""},
        {"node_pubkey": "e", "mesh_endpoint": None},
        {"node_pubkey": "f", "mesh_endpoint": "10.13.13.40:9000"},
    ]

    @pytest.fixture
    def home(self, monkeypatch, tmp_path):
        for var in ("ZAKURO_API_KEY", "ZAKURO_AUTH", "ZAKURO_API_URL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        return tmp_path

    def fake_hub(self, monkeypatch, rows):
        from zakuro import mesh

        calls = []

        def get_json(url, timeout, headers=None):
            calls.append((url, headers))
            return rows if url.endswith("/api/broker/node/roster") else None

        monkeypatch.setattr(mesh, "_get_json", get_json)
        return calls

    def test_signed_in_a_fresh_machine_finds_the_nodes(self, monkeypatch, home):
        (home / "credentials").write_text("api_key=zk_test\napi_url=https://stg.example.test\n")
        calls = self.fake_hub(monkeypatch, self.ROWS)
        from zakuro import mesh

        # Revoked, tunnel-less and repeated rows are dropped.
        assert mesh._endpoints() == ["10.13.13.40:9000", "10.13.13.41:9000"]
        url, headers = calls[-1]
        assert url == "https://stg.example.test/api/broker/node/roster"
        assert headers == {"X-Broker-Api-Key": "zk_test"}

    def test_the_hub_comes_before_the_cache_and_both_are_kept(self, monkeypatch, home):
        (home / "credentials").write_text("api_key=zk_test\n")
        (home / "roster.json").write_text(
            json.dumps({"endpoints": [["x", "10.13.13.17:9000"], ["y", "10.13.13.41:9000"]]})
        )
        calls = self.fake_hub(monkeypatch, self.ROWS)
        from zakuro import mesh

        assert mesh._endpoints() == [
            "10.13.13.40:9000",
            "10.13.13.41:9000",
            "10.13.13.17:9000",
        ]
        # No api_url saved: the production dashboard, as zc defaults to.
        assert calls[-1][0] == "https://hub.zakuro-ai.com/api/broker/node/roster"

    def test_signed_out_the_hub_is_not_asked(self, monkeypatch, home):
        calls = self.fake_hub(monkeypatch, self.ROWS)
        from zakuro import mesh

        assert mesh._endpoints() == []
        assert not [u for u, _ in calls if "roster" in u]

    @pytest.mark.parametrize("body", [None, {"error": "nope"}, ["junk", 3]])
    def test_an_unusable_hub_answer_is_no_endpoints(self, monkeypatch, home, body):
        (home / "credentials").write_text("api_key=zk_test\n")
        self.fake_hub(monkeypatch, body)
        from zakuro import mesh

        assert mesh._endpoints() == []


class TestDirectAddressingIsRefused:
    """Work enters the mesh through a broker, or not at all.

    A `spark://` or `ray://` URI dials a cluster port from the client, so the
    job is never routed, billed or attributed -- the node that did the work is
    not paid for it. The clusters bind loopback and only brokers transit the
    mesh, so such a URI cannot work off-host anyway; refusing it turns a
    confusing connection error into a statement of the rule.
    """

    @pytest.mark.parametrize(
        "uri",
        [
            "spark://master:7077",
            "ray://head:10001",
            "dask://scheduler:8786",
            "tcp://scheduler:8786",
            "quic://worker:4433",
            "zakuro://worker:3960",
            "http://worker:3960",
        ],
    )
    def test_a_framework_or_worker_uri_is_refused(self, uri):
        with pytest.raises(ValueError) as e:
            Compute(uri=uri, verify=False)
        assert "through a broker" in str(e.value)

    def test_addressing_a_node_by_ip_is_refused(self):
        # An address is not an identity, and the roster authorises fingerprints.
        with pytest.raises(ValueError) as e:
            Compute(uri="zc://10.13.13.215:9000", verify=False)
        assert "not an identity" in str(e.value)

    def test_your_own_broker_on_loopback_is_allowed(self):
        # Talking to the broker beside you is not addressing a node, and it is
        # how a container reaches its own.
        for uri in ("zc://127.0.0.1:9000", "zc://localhost:9002"):
            assert Compute(uri=uri, verify=False).uri == uri

    def test_a_node_fingerprint_is_allowed(self, monkeypatch):
        node = NodeInfo(
            endpoint="10.13.13.215:9000",
            node_id="zc://node-e18d8c0ed6ec7b5a",
            name="alpha-1",
        )
        monkeypatch.setattr("zakuro.mesh.nodes", lambda timeout=4.0, refresh=False: [node])
        assert Compute(uri="zc://node-e18d8c0ed6ec7b5a").host == "10.13.13.215"

    def test_the_escape_hatch_is_explicit(self, monkeypatch):
        # A single-machine dev loop with no broker still needs to work, but it
        # has to be asked for rather than being what happens by default.
        monkeypatch.setenv("ZAKURO_ALLOW_DIRECT", "1")
        assert Compute(uri="spark://master:7077", verify=False).uri == "spark://master:7077"

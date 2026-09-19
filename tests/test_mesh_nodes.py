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
        assert not _is_node_uri("zc://10.13.13.15:9000")
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
            endpoint="10.13.13.15:9000",
            node_id="zc://node-e18d8c0ed6ec7b5a",
            name="zc-worker-i9-1",
        )
        monkeypatch.setattr("zakuro.mesh.nodes", lambda timeout=4.0: [node])

        c = Compute(uri="zc://node-e18d8c0ed6ec7b5a")

        assert c.host == "10.13.13.15"
        assert c.port == 9000
        # The URI is rewritten too: everything downstream re-parses it, and a
        # fingerprint left in the authority is read as a hostname -- which
        # failed as a DNS error naming the user's perfectly valid node id.
        assert c.uri == "zc://10.13.13.15:9000"
        assert c._node is node

    def test_an_unknown_fingerprint_says_what_is_reachable(self, monkeypatch):
        monkeypatch.setattr(
            "zakuro.mesh.nodes",
            lambda timeout=4.0: [
                NodeInfo(
                    endpoint="10.13.13.15:9000", node_id="zc://node-e18d8c0ed6ec7b5a", name="i9-1"
                )
            ],
        )
        # The usual cause is a stale fingerprint copied from an older listing,
        # so the error is only useful if it carries the current one.
        with pytest.raises(LookupError) as e:
            resolve_node("zc://node-0000000000000000")
        assert "i9-1=e18d8c0ed6ec" in str(e.value)
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
        assert mesh.proxy_for("10.13.13.15") == "http://127.0.0.1:18888"


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
        n = NodeInfo(endpoint="10.13.13.15:9000", node_id="zc://node-e18d8c0ed6ec7b5a", name="i9-1")
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
                "endpoints": [["pk1", "10.13.13.15:9000"], ["pk2", ""]],
            }
        )
    )
    monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
    from zakuro import mesh

    monkeypatch.setattr(mesh, "_get_json", lambda *a, **k: None)
    # The empty endpoint is dropped, not probed: an authorized node with no
    # tunnel up is normal, not malformed.
    assert mesh._endpoints() == ["10.13.13.15:9000"]

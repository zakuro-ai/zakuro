"""Tests for zakuro.dataset — the zk.dataset(ref).load() SDK surface.

Every HTTP call is faked at ``httpx.get``, the same seam ``test_model.py``
uses for ``httpx.post``. What is under test is the client's own behaviour:
which routes it picks for which reference shape, that it verifies what it
downloaded, that it caches by digest, and that it parses each format into
rows rather than handing back bytes.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

import zakuro as zk
from zakuro.dataset import (
    PROD_API_URL,
    STAGING_API_URL,
    Dataset,
    DatasetFormatError,
    DatasetNotFoundError,
    resolve_api_url,
)

UUID = "e629e662-1d32-4e52-88e9-b0e83416c852"
DIGEST = "f" * 64
CSV = b"text,label\nThe film was fantastic,positive\nTerrible service,negative\n"


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class _Resp:
    def __init__(self, status_code: int, payload=None, content: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _routes(files=None, body=CSV, name="sentiment-mini"):
    """A fake marketplace, plus the call log, for one single-file dataset."""
    files = files or [{"path": "data.csv", "sha256": _sha(body), "size_bytes": len(body)}]
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/resolve" in url:
            return _Resp(
                200,
                {
                    "canonical": f"zc://alice/{name}@sha256:{DIGEST}",
                    "dataset_id": UUID,
                    "digest": DIGEST,
                    "size_bytes": len(body),
                    "owner": "alice",
                    "name": name,
                },
            )
        if url.endswith(f"/api/datasets/{UUID}"):
            return _Resp(
                200,
                {
                    "id": UUID,
                    "name": name,
                    "owner_handle": "alice",
                    "latest_version": {"digest": DIGEST, "size_bytes": len(body), "files": files},
                },
            )
        if f"/versions/{DIGEST}/files/" in url:
            return _Resp(200, content=body)
        if f"/versions/{DIGEST}" in url:
            return _Resp(200, {"digest": DIGEST, "files": files})
        return _Resp(404, {"detail": {"errors": ["not found"]}})

    return fake_get, calls


class TestApiUrlResolution:
    def test_defaults_to_production(self, monkeypatch):
        monkeypatch.delenv("ZAKURO_API_URL", raising=False)
        monkeypatch.delenv("ZAKURO_ENV", raising=False)
        assert resolve_api_url(None) == PROD_API_URL

    def test_production_is_a_host_that_actually_resolves(self):
        # zc's credentials.rs names my.zakuro-ai.com, which is NXDOMAIN. A
        # default that cannot resolve fails before it can even report an auth
        # error, so pin the one that is live.
        assert PROD_API_URL == "https://hub.zakuro-ai.com"

    def test_zakuro_env_selects_staging(self, monkeypatch):
        monkeypatch.delenv("ZAKURO_API_URL", raising=False)
        monkeypatch.setenv("ZAKURO_ENV", "staging")
        assert resolve_api_url(None) == STAGING_API_URL

    def test_an_explicit_api_url_env_var_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("ZAKURO_ENV", "staging")
        monkeypatch.setenv("ZAKURO_API_URL", "http://localhost:8000")
        assert resolve_api_url(None) == "http://localhost:8000"

    def test_an_argument_beats_every_environment_variable(self, monkeypatch):
        monkeypatch.setenv("ZAKURO_API_URL", "http://localhost:8000")
        assert resolve_api_url("http://other:9000") == "http://other:9000"

    def test_a_trailing_slash_is_trimmed(self):
        assert resolve_api_url("http://x/") == "http://x"


class TestReferenceRouting:
    def test_a_named_reference_goes_through_resolve(self, monkeypatch, tmp_path):
        fake_get, calls = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        zk.dataset("zc://alice/sentiment-mini", api_url="http://x", cache_dir=tmp_path).load()
        assert any("/resolve" in c for c in calls)

    def test_a_uuid_reference_never_calls_resolve(self, monkeypatch, tmp_path):
        # The marketplace's _REF_RE requires owner/name and answers 422 for a
        # bare uuid, so this shape has to take the detail route instead.
        fake_get, calls = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        zk.dataset(f"zc://{UUID}", api_url="http://x", cache_dir=tmp_path).load()
        assert not any("/resolve" in c for c in calls)

    def test_a_bare_uuid_is_accepted(self, monkeypatch, tmp_path):
        fake_get, calls = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        rows = zk.dataset(UUID, api_url="http://x", cache_dir=tmp_path).load()
        assert len(rows) == 2

    def test_a_reference_that_is_not_one_is_rejected(self):
        with pytest.raises(ValueError):
            Dataset("https://example.com/x")


class TestLoad:
    def test_csv_becomes_a_list_of_dicts(self, monkeypatch, tmp_path):
        fake_get, _ = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        rows = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load()
        assert rows == [
            {"text": "The film was fantastic", "label": "positive"},
            {"text": "Terrible service", "label": "negative"},
        ]

    def test_tsv_is_split_on_tabs(self, monkeypatch, tmp_path):
        body = b"id\ttext\n1\thi there\n"
        fake_get, _ = _routes(
            files=[{"path": "d.tsv", "sha256": _sha(body), "size_bytes": len(body)}], body=body
        )
        monkeypatch.setattr(httpx, "get", fake_get)
        rows = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load()
        assert rows == [{"id": "1", "text": "hi there"}]

    def test_jsonl_keeps_native_types(self, monkeypatch, tmp_path):
        # Unlike the hub preview, which stringifies for display, the SDK hands
        # back what was in the file.
        body = b'{"id": 1, "ok": true}\n{"id": 2, "ok": false}\n'
        fake_get, _ = _routes(
            files=[{"path": "d.jsonl", "sha256": _sha(body), "size_bytes": len(body)}], body=body
        )
        monkeypatch.setattr(httpx, "get", fake_get)
        rows = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load()
        assert rows == [{"id": 1, "ok": True}, {"id": 2, "ok": False}]

    def test_a_json_array_loads(self, monkeypatch, tmp_path):
        body = b'[{"id": 1}, {"id": 2}]'
        fake_get, _ = _routes(
            files=[{"path": "d.json", "sha256": _sha(body), "size_bytes": len(body)}], body=body
        )
        monkeypatch.setattr(httpx, "get", fake_get)
        rows = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load()
        assert rows == [{"id": 1}, {"id": 2}]

    def test_limit_truncates_the_returned_rows(self, monkeypatch, tmp_path):
        fake_get, _ = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        rows = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load(limit=1)
        assert len(rows) == 1

    def test_an_unparseable_format_raises_rather_than_returning_nothing(
        self, monkeypatch, tmp_path
    ):
        # The SDK caller asked for rows; silently handing back [] would be a
        # worse answer than saying the format has no reader.
        body = b"PAR1\x00\x00"
        fake_get, _ = _routes(
            files=[{"path": "d.parquet", "sha256": _sha(body), "size_bytes": len(body)}], body=body
        )
        monkeypatch.setattr(httpx, "get", fake_get)
        with pytest.raises(DatasetFormatError):
            zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load()

    def test_a_named_file_can_be_chosen(self, monkeypatch, tmp_path):
        body = CSV
        files = [
            {"path": "b.csv", "sha256": _sha(body), "size_bytes": len(body)},
            {"path": "a.csv", "sha256": _sha(body), "size_bytes": len(body)},
        ]
        fake_get, _ = _routes(files=files, body=body)
        monkeypatch.setattr(httpx, "get", fake_get)
        rows = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load(path="b.csv")
        assert len(rows) == 2

    def test_an_unknown_file_name_is_an_error(self, monkeypatch, tmp_path):
        fake_get, _ = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        with pytest.raises(FileNotFoundError):
            zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load(path="nope.csv")


class TestIntegrity:
    def test_a_corrupted_download_is_refused(self, monkeypatch, tmp_path):
        fake_get, _ = _routes(files=[{"path": "d.csv", "sha256": "0" * 64, "size_bytes": len(CSV)}])
        monkeypatch.setattr(httpx, "get", fake_get)
        with pytest.raises(OSError, match="sha256"):
            zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).download()


class TestCaching:
    def test_a_second_load_makes_no_new_requests(self, monkeypatch, tmp_path):
        fake_get, calls = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        d = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path)
        d.load()
        after_first = len(calls)
        d.load()
        assert len(calls) == after_first

    def test_a_fresh_handle_reuses_the_cached_bytes(self, monkeypatch, tmp_path):
        fake_get, calls = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load()
        downloads = [c for c in calls if "/files/" in c]
        zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path).load()
        # Metadata is fetched again; the bytes are not.
        assert [c for c in calls if "/files/" in c] == downloads


class TestNotFound:
    def test_a_missing_dataset_says_it_may_be_private(self, monkeypatch, tmp_path):
        def fake_get(url, **kwargs):
            return _Resp(404, {"detail": {"errors": ["not found"]}})

        monkeypatch.setattr(httpx, "get", fake_get)
        with pytest.raises(DatasetNotFoundError, match="private"):
            zk.dataset("zc://alice/nope", api_url="http://x", cache_dir=tmp_path).load()


class TestMetadata:
    def test_exposes_the_resolved_facts(self, monkeypatch, tmp_path):
        fake_get, _ = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        d = zk.dataset("zc://alice/sentiment-mini", api_url="http://x", cache_dir=tmp_path)
        assert d.name == "sentiment-mini"
        assert d.digest == DIGEST
        assert [f["path"] for f in d.files] == ["data.csv"]

    def test_columns_come_from_the_first_row(self, monkeypatch, tmp_path):
        fake_get, _ = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        d = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path)
        assert d.columns == ["text", "label"]


class TestPandas:
    def test_to_pandas_explains_the_missing_dependency(self, monkeypatch, tmp_path):
        fake_get, _ = _routes()
        monkeypatch.setattr(httpx, "get", fake_get)
        import builtins

        real_import = builtins.__import__

        def no_pandas(name, *args, **kwargs):
            if name == "pandas":
                raise ImportError("no pandas")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pandas)
        d = zk.dataset("zc://alice/d", api_url="http://x", cache_dir=tmp_path)
        with pytest.raises(ImportError, match="pip install"):
            d.to_pandas()


class TestExports:
    def test_dataset_is_on_the_top_level_namespace(self):
        assert hasattr(zk, "dataset")
        assert "dataset" in zk.__all__

    def test_dataset_is_on_the_stable_public_surface(self):
        from zakuro import public

        assert hasattr(public, "dataset")

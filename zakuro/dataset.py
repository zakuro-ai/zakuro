"""Loading a marketplace dataset — ``zk.dataset("zc://owner/name").load()``.

The models half of the marketplace has had :func:`zakuro.model.model` for a
while; datasets had nothing, so the hub's dataset page printed a ``requests``
snippet with a bearer token in it. This is what that page can point at instead.

    >>> import zakuro as zk
    >>> rows = zk.dataset("zc://alice/sentiment-mini").load()
    >>> rows[0]
    {'text': 'The film was fantastic and moving', 'label': 'positive'}

Public datasets only, deliberately. The marketplace accepts a browser session
and nothing else on its dataset routes — an API key is refused there on
purpose — so there is no credential this could send that would unlock a
private dataset. :class:`DatasetNotFound` says that rather than pretending.

Parsing is the standard library's ``csv`` and ``json``: this module adds no
dependency to the SDK's core install, which is the reason :meth:`Dataset.load`
returns plain dicts and pandas stays an optional extra.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

#: Production marketplace API.
#:
#: NOT `my.zakuro-ai.com`, which zc's `credentials.rs` still names as its
#: PROD_API_URL: that host has no DNS record at all (NXDOMAIN, verified
#: 2026-09-06 against 1.1.1.1), so anything defaulting to it fails to resolve
#: rather than failing to authenticate. `hub.zakuro-ai.com` is the live prod
#: host -- Cloudflare-proxied, publicly reachable, and serving
#: `/api/datasets` -- and it is the prod twin of the `stg.hub` that staging
#: moved to when stg-my was decommissioned.
PROD_API_URL = "https://hub.zakuro-ai.com"
#: Staging. stg.hub serves the whole API in-process; stg-my is decommissioned.
STAGING_API_URL = "https://stg.hub.zakuro-ai.com"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)

_DELIMITERS = {".csv": ",", ".tsv": "\t", ".tab": "\t"}


class DatasetError(RuntimeError):
    """Base class for every failure in this module."""


class DatasetNotFound(DatasetError):
    """No public dataset at that reference."""


class DatasetFormatError(DatasetError):
    """The file has no reader here.

    Raised rather than returning ``[]``: the caller asked for rows, and an
    empty list would claim the dataset is empty when in truth it is parquet.
    """


def resolve_api_url(explicit: str | None = None) -> str:
    """The marketplace base URL, in zc's precedence order.

    ``explicit`` argument, then ``ZAKURO_API_URL``, then ``ZAKURO_ENV``, then
    production. Kept identical to zc's `default_api_url` so that
    ``ZAKURO_ENV=staging`` moves the CLI and the SDK together -- a split there
    is the kind of thing that gets debugged for an hour.
    """
    if explicit:
        return explicit.rstrip("/")
    env_url = os.environ.get("ZAKURO_API_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    if os.environ.get("ZAKURO_ENV", "").strip().lower() in {"staging", "stg", "stage"}:
        return STAGING_API_URL
    return PROD_API_URL


def _default_cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(root) / "zakuro" / "datasets"


def _not_found(reference: str) -> DatasetNotFound:
    return DatasetNotFound(
        f"No public dataset at {reference!r}. It may not exist, or it may be "
        "private -- the SDK can only fetch public datasets. Download a private "
        "one from the hub in a browser."
    )


def _parse_reference(reference: str) -> tuple[str | None, str | None]:
    """``(canonical_ref, None)`` to resolve by name, or ``(None, uuid)``.

    The two forms are not interchangeable: the marketplace's ``/resolve``
    route requires an ``owner/name`` pair and answers 422 for a bare uuid,
    which is exactly the form the hub shows for an owner with no handle. They
    have to take different routes.
    """
    if not isinstance(reference, str) or not reference:
        raise ValueError("a dataset reference is required")
    body = reference[len("zc://"):] if reference.startswith("zc://") else reference
    if _UUID_RE.match(body):
        return None, body.lower()
    if not reference.startswith("zc://") or "/" not in body:
        raise ValueError(
            f"not a dataset reference: {reference!r}. "
            "Expected zc://<owner>/<name> or zc://<uuid>."
        )
    return reference, None


def _get(url: str, **kwargs: Any) -> httpx.Response:
    return httpx.get(url, timeout=_TIMEOUT, follow_redirects=True, **kwargs)


def _rows_from_bytes(path: str, payload: bytes) -> list[dict]:
    """Parse one file's bytes into rows.

    Native types where the format has them: a JSON ``true`` comes back as
    ``True``, not ``"true"``. That is the opposite of the hub's preview, which
    stringifies for display -- here the caller is going to compute on these.
    """
    suffix = Path(path).suffix.lower()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetFormatError(
            f"{path} is not UTF-8 text; no reader for it here."
        ) from exc

    if suffix in _DELIMITERS:
        reader = csv.DictReader(io.StringIO(text), delimiter=_DELIMITERS[suffix])
        return [dict(row) for row in reader]

    if suffix in {".jsonl", ".ndjson"}:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                rows.append(record)
        return rows

    if suffix == ".json":
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        raise DatasetFormatError(f"{path} is JSON, but not rows.")

    raise DatasetFormatError(
        f"No reader for {path}. load() handles .csv, .tsv, .json and .jsonl; "
        "use download() to fetch the file and parse it yourself."
    )


class Dataset:
    """A handle on one marketplace dataset.

    Metadata is fetched once, lazily, on the first attribute that needs it.
    Bytes are cached on disk under the version digest, which is content-
    addressed -- so a cache hit is the same bytes by construction, and a new
    version simply lands beside the old one rather than invalidating it.
    """

    def __init__(self, reference: str, api_url: str | None = None,
                 cache_dir: str | Path | None = None) -> None:
        self._reference = reference
        self._canonical, self._uuid = _parse_reference(reference)
        self._api_url = resolve_api_url(api_url)
        self._cache_root = Path(cache_dir) if cache_dir else _default_cache_dir()
        self._meta: dict | None = None

    # -- metadata ---------------------------------------------------------

    def _resolved(self) -> dict:
        if self._meta is not None:
            return self._meta
        self._meta = (self._resolve_by_uuid() if self._uuid
                      else self._resolve_by_name())
        return self._meta

    def _resolve_by_name(self) -> dict:
        r = _get(f"{self._api_url}/api/datasets/resolve",
                 params={"ref": self._canonical})
        if r.status_code in (404, 422):
            raise _not_found(self._reference)
        if r.status_code != 200:
            raise DatasetError(f"resolve failed ({r.status_code})")
        body = r.json()
        dataset_id, digest = body["dataset_id"], body["digest"]
        files = self._version_files(dataset_id, digest)
        return {"dataset_id": dataset_id, "digest": digest,
                "name": body.get("name", "dataset"), "files": files,
                "size_bytes": body.get("size_bytes", 0)}

    def _resolve_by_uuid(self) -> dict:
        r = _get(f"{self._api_url}/api/datasets/{self._uuid}")
        if r.status_code == 404:
            raise _not_found(self._reference)
        if r.status_code != 200:
            raise DatasetError(f"lookup failed ({r.status_code})")
        body = r.json()
        latest = body.get("latest_version")
        if not latest:
            raise DatasetError(f"dataset {self._uuid} has no published version")
        return {"dataset_id": body["id"], "digest": latest["digest"],
                "name": body.get("name", "dataset"),
                "files": latest.get("files", []),
                "size_bytes": latest.get("size_bytes", 0)}

    def _version_files(self, dataset_id: str, digest: str) -> list[dict]:
        r = _get(f"{self._api_url}/api/datasets/{dataset_id}/versions/{digest}")
        if r.status_code != 200:
            raise DatasetError(f"could not list files ({r.status_code})")
        return r.json().get("files", [])

    @property
    def name(self) -> str:
        return self._resolved()["name"]

    @property
    def digest(self) -> str:
        """The version digest -- the thing that makes the cache safe."""
        return self._resolved()["digest"]

    @property
    def size_bytes(self) -> int:
        return self._resolved()["size_bytes"]

    @property
    def files(self) -> list[dict]:
        return list(self._resolved()["files"])

    @property
    def columns(self) -> list[str]:
        rows = self.load(limit=1)
        return list(rows[0]) if rows else []

    # -- bytes ------------------------------------------------------------

    def _pick(self, path: str | None) -> dict:
        files = self._resolved()["files"]
        if not files:
            raise DatasetError(f"{self._reference} has no files")
        if path is None:
            # Lowest-sorting path, the same tie-break the marketplace's own
            # download route uses, so a single-file dataset always means its
            # one file and a multi-file one is at least predictable.
            return min(files, key=lambda f: f["path"])
        for f in files:
            if f["path"] == path:
                return f
        raise FileNotFoundError(
            f"{path!r} is not in {self._reference}. "
            f"Available: {sorted(f['path'] for f in files)}"
        )

    def _fetch(self, file: dict) -> bytes:
        """This file's bytes, from the cache when they are already there."""
        target = self._cache_root / self.digest / file["path"]
        if target.exists():
            return target.read_bytes()

        url = (f"{self._api_url}/api/datasets/{self._resolved()['dataset_id']}"
               f"/versions/{self.digest}/files/{file['path']}")
        r = _get(url)
        if r.status_code == 404:
            raise _not_found(self._reference)
        if r.status_code != 200:
            raise DatasetError(f"{file['path']}: HTTP {r.status_code}")
        payload = r.content

        expected = (file.get("sha256") or "").lower()
        if expected:
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                # The catalogue hands us the hash for free; not checking it
                # would be choosing not to notice a truncated download.
                raise IOError(
                    f"{file['path']}: sha256 mismatch "
                    f"(expected {expected}, got {actual})"
                )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return payload

    def download(self, dest: str | Path | None = None) -> Path:
        """Fetch every file, returning the directory holding them."""
        out = Path(dest) if dest else self._cache_root / self.digest
        out.mkdir(parents=True, exist_ok=True)
        for file in self._resolved()["files"]:
            payload = self._fetch(file)
            target = out / file["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(payload)
        return out

    # -- rows -------------------------------------------------------------

    def load(self, limit: int | None = None, path: str | None = None) -> list[dict]:
        """The dataset's rows.

        ``limit`` truncates what is returned; it does not make the download
        smaller. The whole file is fetched either way, because the cache is
        keyed by digest and storing a partial fetch under it would poison
        every later read.
        """
        file = self._pick(path)
        rows = _rows_from_bytes(file["path"], self._fetch(file))
        return rows[:limit] if limit is not None else rows

    def to_pandas(self, path: str | None = None):
        """The rows as a ``pandas.DataFrame``.

        pandas is not a dependency of the SDK -- the core install is five
        light packages and this module deliberately does not add a sixth -- so
        this asks for it only when called.
        """
        try:
            import pandas
        except ImportError as exc:
            raise ImportError(
                "to_pandas() needs pandas, which zakuro-ai does not install. "
                "pip install pandas"
            ) from exc
        return pandas.DataFrame(self.load(path=path))

    def __repr__(self) -> str:
        return f"Dataset({self._reference!r})"


def dataset(reference: str, api_url: str | None = None,
            cache_dir: str | Path | None = None) -> Dataset:
    """A handle on a public marketplace dataset.

    >>> rows = dataset("zc://alice/sentiment-mini").load()
    """
    return Dataset(reference, api_url=api_url, cache_dir=cache_dir)

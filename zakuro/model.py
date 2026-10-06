"""Native model inference, spanning a broker endpoint and a hub endpoint.

A zc broker exposes ``POST {broker}/infer`` for routing chat-style inference
requests to whichever provider (local worker, remote peer, etc.) is currently
serving a given model. The Zakuro hub separately exposes
``POST {hub}/api/models/{model_id}/transcribe`` for speech-to-text, billed
per second of audio. The two live on different services — the broker picks a
live provider for a running model, while the hub bills and meters a specific
model endpoint directly — so this module carries both:

    >>> import zakuro as zk
    >>> result = zk.model("zc://<uuid>").chat([{"role": "user", "content": "hi"}])
    >>> result.content
    'Hello!'
    >>> transcript = zk.model("zc://<uuid>").transcribe("clip.wav")
    >>> transcript.text
    '...'

Routing of :meth:`Model.chat`:
    - With ``broker=`` or the ``ZAKURO_BROKER`` environment variable set, the
      request goes to ``{broker}/infer`` (a local ``zc`` broker). ``broker=`` is
      used verbatim (an http(s) base URL, e.g. ``"http://127.0.0.1:9000"``).
    - Otherwise it goes to the hub, ``POST {hub}/api/models/{uuid}/infer``,
      authenticated with ``ZAKURO_API_KEY``. There is no public broker.

Hub resolution (used by :meth:`Model.chat` and :meth:`Model.transcribe`):
    - ``hub=`` passed explicitly to :func:`model` / :class:`Model` is used
      verbatim.
    - Otherwise :func:`zakuro.hub.resolve_hub_url`: ``ZAKURO_API_URL``, then
      ``ZAKURO_ENV`` (``staging`` -> the staging API), then the configured
      ``hub_url`` from :class:`zakuro.config.Config` (settable via
      ``ZAKURO_HUB_URL`` or the config file's ``hub.url``), which defaults to
      ``"https://hub.zakuro-ai.com"``.

Transport safety (ZAKURO-0017):
    The API key is never attached to a plain ``http://`` request to a
    non-loopback, non-mesh host: :class:`zakuro.security.InsecureTransportError`
    is raised instead. Set ``ZAKURO_ALLOW_INSECURE_HTTP=1`` to opt in (e.g. a
    self-hosted plain-http hub). Loopback and mesh addresses are always allowed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from zakuro.security import ensure_secure_for_key

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Request timeout for /infer and /transcribe calls. Both can be slow (large
# prompts or long audio, cold providers), and the hub allows up to 300s for
# transcription, so this mirrors ZakuroClient's generous read timeout rather
# than httpx's short default.
_INFER_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)


class ModelInferenceError(RuntimeError):
    """Raised when a hub or broker inference call fails (non-2xx response)."""


@dataclass
class ChatResult:
    """Result of a :meth:`Model.chat` call."""

    content: str
    usage: dict = field(default_factory=dict)
    charged_zkcr: float = 0.0
    provider: str = ""


@dataclass
class TranscriptResult:
    """Result of a :meth:`Model.transcribe` call."""

    text: str
    language: str = ""
    duration_s: float = 0.0
    billed_seconds: int = 0
    truncated: bool = False
    charged_zkcr: float = 0.0
    balance: float = 0.0


def _coerce_float(value: Any, default: float = 0.0) -> float:
    """Best-effort coercion of a wire value (often a string) to a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _model_uuid(uri: str) -> str:
    """Strip ``zc://`` and ``model-`` from ``uri``, returning the bare uuid.

    ``uri`` is expected to already be a recognized model reference (see
    :func:`_is_model_uri`); this does not re-validate it.
    """
    candidate = uri
    if candidate.startswith("zc://"):
        candidate = candidate[len("zc://") :]
        if candidate.startswith("model-"):
            candidate = candidate[len("model-") :]
    return candidate


def _is_model_uri(uri: str) -> bool:
    """Return True if ``uri`` looks like a model reference.

    Accepted forms:
        - ``zc://<uuid>``
        - ``zc://model-<uuid>``
        - a bare ``<uuid>``
    """
    if not isinstance(uri, str) or not uri:
        return False

    return bool(_UUID_RE.match(_model_uuid(uri)))


class Model:
    """A handle to a model served somewhere on the mesh, addressed by uuid.

    Args:
        uri: A model reference — ``zc://<uuid>``, ``zc://model-<uuid>``, or a
            bare uuid.
        broker: Optional broker HTTP base URL (e.g. ``"http://host:9000"``).
            When omitted, resolved from :class:`zakuro.config.Config`.
        hub: Optional hub HTTP base URL (e.g. ``"https://hub.zakuro-ai.com"``).
            When omitted, resolved by :func:`zakuro.hub.resolve_hub_url`.
        api_key: Optional API key sent as ``Authorization: Bearer <key>``.
            A broker running with auth enabled (the staging/production
            fleets) rejects ``/infer`` without one; a keyless local broker
            ignores it. Falls back to the ``ZAKURO_API_KEY`` environment
            variable, so notebooks and CI never have to inline the secret.
            The hub uses the same key.

    Raises:
        ValueError: If ``uri`` is not a recognized model reference.
    """

    def __init__(
        self,
        uri: str,
        broker: str | None = None,
        hub: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if not _is_model_uri(uri):
            raise ValueError(
                f"Invalid model uri: {uri!r}. Expected 'zc://<uuid>', "
                "'zc://model-<uuid>', or a bare uuid."
            )
        self.uri = uri
        self._broker_override = broker
        self._hub_override = hub
        self._api_key = api_key

    @property
    def broker_url(self) -> str:
        """Resolve the broker's HTTP base URL for this model."""
        if self._broker_override is not None:
            return self._broker_override.rstrip("/")
        env_broker = (os.environ.get("ZAKURO_BROKER") or "").strip()
        if env_broker:
            return env_broker.rstrip("/")

        from zakuro.config import Config

        config = Config.load()
        return f"http://{config.default_host}:{config.default_port}"

    @property
    def hub_url(self) -> str:
        """Resolve the hub's HTTP base URL for this model."""
        from zakuro.hub import resolve_hub_url

        return resolve_hub_url(self._hub_override)

    def _uses_broker(self) -> bool:
        """True when a broker was chosen explicitly (``broker=`` / ``ZAKURO_BROKER``)."""
        return self._broker_override is not None or bool(
            (os.environ.get("ZAKURO_BROKER") or "").strip()
        )

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 256,
        **params: Any,
    ) -> ChatResult:
        """Send a chat-style inference request.

        Goes to the hub by default (needs ``ZAKURO_API_KEY``); goes to a
        broker's ``/infer`` when ``broker=`` or ``ZAKURO_BROKER`` is set.

        Args:
            messages: Chat messages, e.g. ``[{"role": "user", "content": "hi"}]``.
            max_tokens: Maximum tokens to generate.
            **params: Additional provider-specific parameters forwarded as-is
                in the request body.

        Returns:
            The broker's response as a :class:`ChatResult`.

        Raises:
            ModelInferenceError: If the hub/broker returns a non-2xx response,
                or no API key is available on the hub path.
        """
        if not self._uses_broker():
            return self._chat_hub(messages, max_tokens, params)

        body = {
            "model": self.uri,
            "messages": messages,
            "max_tokens": max_tokens,
            **params,
        }

        headers = {}
        api_key = self._api_key or os.environ.get("ZAKURO_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        broker_url = self.broker_url
        if api_key:
            ensure_secure_for_key(broker_url)
        try:
            response = httpx.post(
                f"{broker_url}/infer",
                json=body,
                headers=headers,
                timeout=_INFER_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ModelInferenceError(
                f"Failed to reach broker at {broker_url} for model {self.uri}: {exc}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            message = _extract_error_message(response)
            raise ModelInferenceError(
                f"Inference request for model {self.uri} failed ({response.status_code}): {message}"
            )

        data = response.json()
        return ChatResult(
            content=data.get("content", ""),
            usage=data.get("usage", {}),
            charged_zkcr=data.get("charged_zkcr", 0.0),
            provider=data.get("provider", ""),
        )

    def _chat_hub(self, messages: list[dict], max_tokens: int, params: dict) -> ChatResult:
        api_key = self._api_key or os.environ.get("ZAKURO_API_KEY")
        if not api_key:
            raise ModelInferenceError(
                "No API key: set the ZAKURO_API_KEY environment variable "
                "(or pass api_key=) to call a model through the hub."
            )
        hub_url = self.hub_url
        url = f"{hub_url}/api/models/{_model_uuid(self.uri)}/infer"
        body = {"messages": messages, "max_tokens": max_tokens, **params}
        try:
            response = httpx.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=_INFER_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ModelInferenceError(
                f"Failed to reach hub at {hub_url} for model {self.uri}: {exc}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            message = _extract_error_message(response)
            raise ModelInferenceError(
                f"Inference request for model {self.uri} failed ({response.status_code}): {message}"
            )

        data = response.json()
        return ChatResult(
            content=data.get("content", ""),
            usage=data.get("usage", {}),
            charged_zkcr=_coerce_float(data.get("charged_zkcr", 0.0)),
            provider="hub",
        )

    def transcribe(
        self,
        audio: str | Path | BinaryIO,
        language: str | None = None,
        **params: Any,
    ) -> TranscriptResult:
        """Transcribe an audio clip via the hub's speech-to-text endpoint.

        Args:
            audio: Audio to transcribe — a path (``str`` or
                :class:`~pathlib.Path`) to a local file, or an already-open
                binary file object. A path is opened in binary mode and sent
                under its basename; a file object is sent under its ``name``
                attribute when it has one, else the literal ``"audio"``.
            language: Optional 2-3 letter lowercase language code. Omitted
                from the request entirely when ``None``, letting the hub
                auto-detect.
            **params: Additional fields forwarded as-is in the multipart
                form body.

        Returns:
            The hub's response as a :class:`TranscriptResult`.

        Raises:
            ModelInferenceError: If the hub returns a non-2xx response.
        """
        data = dict(params)
        if language is not None:
            data["language"] = language

        headers = {}
        api_key = self._api_key or os.environ.get("ZAKURO_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        hub_url = self.hub_url
        if api_key:
            ensure_secure_for_key(hub_url)
        model_id = _model_uuid(self.uri)
        url = f"{hub_url}/api/models/{model_id}/transcribe"

        # The handle is handed to httpx, not read into memory: an audio clip
        # can run to the hub's 25 MB limit, and there is no reason to hold all
        # of it at once. httpx reads it while the request is being sent, so
        # the path branch stays inside the `with`.
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            with open(path, "rb") as fh:
                return self._post_transcribe(url, fh, path.name, data, headers)

        filename = getattr(audio, "name", None) or "audio"
        filename = Path(filename).name
        return self._post_transcribe(url, audio, filename, data, headers)

    def _post_transcribe(
        self,
        url: str,
        stream: BinaryIO,
        filename: str,
        data: dict,
        headers: dict,
    ) -> TranscriptResult:
        try:
            response = httpx.post(
                url,
                files={"file": (filename, stream)},
                data=data,
                headers=headers,
                timeout=_INFER_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ModelInferenceError(
                f"Failed to reach hub at {self.hub_url} for model {self.uri}: {exc}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            message = _extract_error_message(response)
            raise ModelInferenceError(
                f"Transcription request for model {self.uri} failed "
                f"({response.status_code}): {message}"
            )

        result = response.json()
        return TranscriptResult(
            text=result.get("text", ""),
            language=result.get("language", ""),
            duration_s=_coerce_float(result.get("duration_s", 0.0)),
            billed_seconds=int(result.get("billed_seconds", 0) or 0),
            truncated=bool(result.get("truncated", False)),
            charged_zkcr=_coerce_float(result.get("charged_zkcr", 0.0)),
            balance=_coerce_float(result.get("balance", 0.0)),
        )

    def __repr__(self) -> str:
        return f"Model(uri={self.uri!r}, broker={self.broker_url!r})"


def _extract_error_message(response: httpx.Response) -> str:
    """Best-effort extraction of the server's error message.

    Understands the broker's ``{"error": "..."}``, the hub's
    ``{"detail": {"errors": [...]}}``, and a plain ``{"detail": "..."}``.
    """
    try:
        data = response.json()
    except ValueError:
        return response.text
    if not isinstance(data, dict):
        return response.text
    if "error" in data:
        return str(data["error"])
    detail = data.get("detail")
    if isinstance(detail, dict) and "errors" in detail:
        errors = detail["errors"]
        if isinstance(errors, list):
            return "; ".join(str(e) for e in errors)
        return str(errors)
    if isinstance(detail, str):
        return detail
    return response.text


def model(
    uri: str,
    broker: str | None = None,
    hub: str | None = None,
    api_key: str | None = None,
) -> Model:
    """Construct a :class:`Model` handle for ``uri``.

    See :class:`Model` for argument details.
    """
    return Model(uri, broker=broker, hub=hub, api_key=api_key)

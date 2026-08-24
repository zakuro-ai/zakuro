"""Native model inference against a zc broker's ``/infer`` endpoint.

A zc broker exposes ``POST {broker}/infer`` for routing chat-style inference
requests to whichever provider (local worker, remote peer, etc.) is currently
serving a given model. This module is the thin SDK surface over that
endpoint:

    >>> import zakuro as zk
    >>> result = zk.model("zc://<uuid>").chat([{"role": "user", "content": "hi"}])
    >>> result.content
    'Hello!'

Broker resolution:
    - ``broker=`` passed explicitly to :func:`model` / :class:`Model` is used
      verbatim (expected to already be an http(s) base URL, e.g.
      ``"http://127.0.0.1:9000"``).
    - Otherwise the default broker is derived from :class:`zakuro.config.Config`
      (``default_host`` / ``default_port``), the same source
      :class:`zakuro.compute.Compute` uses for its ``zc://`` default, built as
      ``http://{default_host}:{default_port}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Request timeout for /infer calls. Inference can be slow (large prompts, cold
# providers), so this mirrors ZakuroClient's generous read timeout rather than
# httpx's short default.
_INFER_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)


class ModelInferenceError(RuntimeError):
    """Raised when a broker's ``/infer`` call fails (non-2xx response)."""


@dataclass
class ChatResult:
    """Result of a :meth:`Model.chat` call."""

    content: str
    usage: dict = field(default_factory=dict)
    charged_zkcr: float = 0.0
    provider: str = ""


def _is_model_uri(uri: str) -> bool:
    """Return True if ``uri`` looks like a model reference.

    Accepted forms:
        - ``zc://<uuid>``
        - ``zc://model-<uuid>``
        - a bare ``<uuid>``
    """
    if not isinstance(uri, str) or not uri:
        return False

    candidate = uri
    if candidate.startswith("zc://"):
        candidate = candidate[len("zc://") :]
        if candidate.startswith("model-"):
            candidate = candidate[len("model-") :]

    return bool(_UUID_RE.match(candidate))


class Model:
    """A handle to a model served somewhere on the mesh, addressed by uuid.

    Args:
        uri: A model reference — ``zc://<uuid>``, ``zc://model-<uuid>``, or a
            bare uuid.
        broker: Optional broker HTTP base URL (e.g. ``"http://host:9000"``).
            When omitted, resolved from :class:`zakuro.config.Config`.

    Raises:
        ValueError: If ``uri`` is not a recognized model reference.
    """

    def __init__(self, uri: str, broker: str | None = None) -> None:
        if not _is_model_uri(uri):
            raise ValueError(
                f"Invalid model uri: {uri!r}. Expected 'zc://<uuid>', "
                "'zc://model-<uuid>', or a bare uuid."
            )
        self.uri = uri
        self._broker_override = broker

    @property
    def broker_url(self) -> str:
        """Resolve the broker's HTTP base URL for this model."""
        if self._broker_override is not None:
            return self._broker_override.rstrip("/")

        from zakuro.config import Config

        config = Config.load()
        return f"http://{config.default_host}:{config.default_port}"

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 256,
        **params: Any,
    ) -> ChatResult:
        """Send a chat-style inference request to the broker.

        Args:
            messages: Chat messages, e.g. ``[{"role": "user", "content": "hi"}]``.
            max_tokens: Maximum tokens to generate.
            **params: Additional provider-specific parameters forwarded as-is
                in the request body.

        Returns:
            The broker's response as a :class:`ChatResult`.

        Raises:
            ModelInferenceError: If the broker returns a non-2xx response.
        """
        body = {
            "model": self.uri,
            "messages": messages,
            "max_tokens": max_tokens,
            **params,
        }

        broker_url = self.broker_url
        try:
            response = httpx.post(
                f"{broker_url}/infer",
                json=body,
                timeout=_INFER_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ModelInferenceError(
                f"Failed to reach broker at {broker_url} for model {self.uri}: {exc}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            message = _extract_error_message(response)
            raise ModelInferenceError(
                f"Inference request for model {self.uri} failed "
                f"({response.status_code}): {message}"
            )

        data = response.json()
        return ChatResult(
            content=data.get("content", ""),
            usage=data.get("usage", {}),
            charged_zkcr=data.get("charged_zkcr", 0.0),
            provider=data.get("provider", ""),
        )

    def __repr__(self) -> str:
        return f"Model(uri={self.uri!r}, broker={self.broker_url!r})"


def _extract_error_message(response: httpx.Response) -> str:
    """Best-effort extraction of the server's {"error": "..."} message."""
    try:
        data = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict) and "error" in data:
        return str(data["error"])
    return response.text


def model(uri: str, broker: str | None = None) -> Model:
    """Construct a :class:`Model` handle for ``uri``.

    See :class:`Model` for argument details.
    """
    return Model(uri, broker=broker)

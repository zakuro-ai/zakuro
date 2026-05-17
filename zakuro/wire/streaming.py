"""Worker-side reassembly for v0.2 streaming chunks (#175 Phase 2).

The wire format defines :class:`zakuro.wire.ChunkFrame` for multi-chunk
dispatches. This module accumulates those chunks per ``stream_id`` and
exposes the reassembled :class:`zakuro.wire.EnvelopeV2` once the final
chunk arrives.

Design notes
------------

* Per-stream state lives in a :class:`_StreamBuffer` keyed by
  ``stream_id``. The buffer enforces:

    - **In-order arrival.** Chunks must arrive with monotonically
      increasing ``seq`` starting at 0. Out-of-order chunks reject the
      whole stream with :class:`ChunkOrderError` — the QUIC layer
      already guarantees per-stream ordering, so out-of-order arrival
      here is a protocol bug, not a transient network event.
    - **Memory bound.** A stream that exceeds ``max_stream_bytes``
      (default 4 GiB) is dropped with :class:`ChunkBufferOverflow`.
      Protects against an attacker submitting a stream that never ends.
    - **Timeout.** A stream that goes silent for ``stream_timeout``
      seconds is reaped. The reassembler holds a single timer; orphan
      streams free their buffers on garbage collection.

* The reassembler is **single-threaded** by design — pyo3-aioquic
  pushes chunks one at a time from the connection's task. Callers that
  want concurrent reassembly across connections should instantiate one
  ``ChunkReassembler`` per connection.

* When the final chunk arrives, :meth:`feed` returns the reassembled
  envelope bytes. The caller decodes them via
  :func:`zakuro.wire.decode_envelope_v2` and forwards to the executor.

The companion design for delta-encoded payloads (#174 Phase 2) lives in
``zakuro/wire/cache.py`` — separate concern with its own algorithm
choice (bsdiff vs xdelta vs zstd-dict) that this module does not need
to know about.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from zakuro.wire import ChunkFrame, WireError

# Defaults follow the headline numbers in the RFC 0001 amendment.
DEFAULT_MAX_STREAM_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
DEFAULT_STREAM_TIMEOUT_SECONDS = 60.0


class StreamingError(WireError):
    """Base for all reassembly failures."""


class ChunkOrderError(StreamingError):
    """A chunk arrived with the wrong ``seq`` for its stream."""


class ChunkBufferOverflowError(StreamingError):
    """Stream exceeded the per-stream byte cap."""


class StreamTimeoutError(StreamingError):
    """Stream went silent past ``stream_timeout``."""


class DuplicateLastChunkError(StreamingError):
    """Two chunks of the same stream arrived with ``last=True``."""


# Aliases (without the N818 Error suffix) kept so external callers can
# write `except ChunkBufferOverflow` etc. Not removed for backwards
# compat with first-pass test imports.
ChunkBufferOverflow = ChunkBufferOverflowError
StreamTimeout = StreamTimeoutError
DuplicateLastChunk = DuplicateLastChunkError


@dataclass
class _StreamBuffer:
    stream_id: int
    chunks: list[bytes] = field(default_factory=list)
    expected_seq: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    bytes_accumulated: int = 0
    closed: bool = False


class ChunkReassembler:
    """Per-connection reassembly buffer for :class:`ChunkFrame` streams.

    Parameters
    ----------
    max_stream_bytes
        Per-stream byte ceiling. Exceeded streams raise
        :class:`ChunkBufferOverflow` and are dropped.
    stream_timeout
        Per-stream idle timeout in seconds. Streams whose
        ``last_activity`` is older than ``now - stream_timeout`` are
        reaped on the next :meth:`gc` call.
    """

    def __init__(
        self,
        *,
        max_stream_bytes: int = DEFAULT_MAX_STREAM_BYTES,
        stream_timeout: float = DEFAULT_STREAM_TIMEOUT_SECONDS,
    ) -> None:
        if max_stream_bytes <= 0:
            raise ValueError("max_stream_bytes must be positive")
        if stream_timeout <= 0:
            raise ValueError("stream_timeout must be positive")
        self._max_stream_bytes = max_stream_bytes
        self._stream_timeout = stream_timeout
        self._streams: dict[int, _StreamBuffer] = {}

    @property
    def active_streams(self) -> int:
        return len(self._streams)

    def feed(self, chunk: ChunkFrame) -> bytes | None:
        """Append *chunk* to its stream. Return reassembled bytes on the last chunk.

        Returns ``None`` while more chunks are expected. When the chunk
        carries ``last=True`` and arrives in order, returns the
        concatenated payload bytes (caller decodes via
        :func:`decode_envelope_v2`) and drops the stream's buffer.

        Raises :class:`ChunkOrderError` on out-of-order arrival,
        :class:`ChunkBufferOverflow` on per-stream byte-cap violation,
        :class:`DuplicateLastChunk` if a stream sees ``last=True`` more
        than once.
        """
        # Reap any stale streams *before* checking this chunk's stream
        # state — protects against a stream_id reuse where the prior
        # stream timed out without ever sending last=True.
        self.gc()

        stream = self._streams.get(chunk.stream_id)
        if stream is None:
            stream = _StreamBuffer(stream_id=chunk.stream_id)
            self._streams[chunk.stream_id] = stream

        if stream.closed:
            raise DuplicateLastChunk(
                f"stream {chunk.stream_id} already terminated; got seq={chunk.seq}"
            )

        if chunk.seq != stream.expected_seq:
            # Drop the bad stream so an attacker can't keep a half-built
            # buffer alive by retrying with wrong seqs.
            del self._streams[chunk.stream_id]
            raise ChunkOrderError(
                f"stream {chunk.stream_id} expected seq={stream.expected_seq}, got seq={chunk.seq}"
            )

        new_total = stream.bytes_accumulated + len(chunk.bytes_)
        if new_total > self._max_stream_bytes:
            del self._streams[chunk.stream_id]
            raise ChunkBufferOverflow(
                f"stream {chunk.stream_id} exceeded {self._max_stream_bytes} bytes"
            )

        stream.chunks.append(chunk.bytes_)
        stream.bytes_accumulated = new_total
        stream.expected_seq += 1
        stream.last_activity = time.monotonic()

        if chunk.last:
            stream.closed = True
            assembled = b"".join(stream.chunks)
            del self._streams[chunk.stream_id]
            return assembled

        return None

    def gc(self, *, now: float | None = None) -> int:
        """Reap streams idle past ``stream_timeout``. Returns count reaped."""
        cutoff = (now if now is not None else time.monotonic()) - self._stream_timeout
        stale = [sid for sid, s in self._streams.items() if s.last_activity < cutoff]
        for sid in stale:
            del self._streams[sid]
        return len(stale)

    def drop_stream(self, stream_id: int) -> bool:
        """Forget *stream_id* unconditionally. Returns True if a stream was present."""
        return self._streams.pop(stream_id, None) is not None

    def reset(self) -> None:
        """Drop every in-progress stream. Test-only — production reassemblers
        keep state across requests."""
        self._streams.clear()

"""Telling the caller's own exceptions apart from failures to reach a worker.

A remote function's exception comes back pickled and is raised again in the
caller, as the same type. Without a mark it is indistinguishable from a
transport error of that type, so retry logic retried the user's own errors.
Every place that raises an exception unpickled from a worker marks it first.

Two cases look like the caller's own exception and are not, or are and do not
look like it:

- the worker could not load the call at all (a module it lacks, a Python or
  cloudpickle mismatch). The worker flags that exception
  (:func:`mark_load_failure`), and :func:`mark_remote` leaves it unmarked, so
  another worker may take the call;
- the call ran, but its reply cannot be rebuilt here (an exception with a
  custom constructor, a result of a type this side cannot import).
  :func:`load_reply` raises a marked :class:`RemoteReplyError` for it, so the
  call is not run a second time.
"""

from __future__ import annotations

import contextlib
from typing import Any

import cloudpickle

_MARK = "__zakuro_remote__"
_LOAD_FAILURE = "__zakuro_load_failure__"


class RemoteReplyError(RuntimeError):
    """The remote call ran, but its reply could not be rebuilt in this process."""


def mark_remote(exc: BaseException) -> BaseException:
    """Mark ``exc`` as raised by the remote code itself, and return it.

    An exception the worker flagged as failing to load the call stays
    unmarked: that worker never ran the call, so another one may."""
    if getattr(exc, _LOAD_FAILURE, False) is True:
        return exc
    # An exception type that refuses attributes stays unmarked: it is then
    # treated as before (retryable), never wrongly as the user's own.
    with contextlib.suppress(AttributeError, TypeError):
        setattr(exc, _MARK, True)
    return exc


def is_remote(exc: BaseException) -> bool:
    """Whether ``exc`` was raised by the remote code, not by reaching it."""
    return getattr(exc, _MARK, False) is True


def mark_load_failure(exc: BaseException) -> BaseException:
    """Mark ``exc`` (on the worker) as the worker failing to load the call."""
    with contextlib.suppress(AttributeError, TypeError):
        setattr(exc, _LOAD_FAILURE, True)
    return exc


def is_load_failure(exc: BaseException) -> bool:
    """Whether the worker flagged ``exc`` as failing to load the call."""
    return getattr(exc, _LOAD_FAILURE, False) is True


def load_reply(data: bytes) -> Any:
    """Unpickle a worker's reply: the call's result or the exception it raised.

    A reply that cannot be rebuilt here still came from a call that ran, so
    the error raised for it is marked as the caller's own: never retried."""
    try:
        return cloudpickle.loads(data)
    except Exception as exc:
        raise mark_remote(
            RemoteReplyError(
                f"the remote call ran, but its reply could not be rebuilt here: {exc!r}"
            )
        ) from exc

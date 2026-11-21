"""Telling the caller's own exceptions apart from failures to reach a worker.

A remote function's exception comes back pickled and is raised again in the
caller, as the same type. Without a mark it is indistinguishable from a
transport error of that type, so retry logic retried the user's own errors.
Every place that raises an exception unpickled from a worker marks it first.
"""

from __future__ import annotations

import contextlib

_MARK = "__zakuro_remote__"


def mark_remote(exc: BaseException) -> BaseException:
    """Mark ``exc`` as raised by the remote code itself, and return it."""
    # An exception type that refuses attributes stays unmarked: it is then
    # treated as before (retryable), never wrongly as the user's own.
    with contextlib.suppress(AttributeError, TypeError):
        setattr(exc, _MARK, True)
    return exc


def is_remote(exc: BaseException) -> bool:
    """Whether ``exc`` was raised by the remote code, not by reaching it."""
    return getattr(exc, _MARK, False) is True

"""The ``/execute`` body, built from a function the SDK has already pickled.

The body stays ``cloudpickle.dumps({"func", "args", "kwargs"})``, loadable by
any worker with a plain ``cloudpickle.loads`` (ZC-0009: additive only). The
function inside it is not pickled a second time: it travels as the exact bytes
the SDK produced, wrapped so that unpickling it calls ``pickle.loads`` on those
bytes. That keeps the bytes identical across calls of an unchanged function,
which is what lets a worker cache the loaded function by their hash.
"""

from __future__ import annotations

import pickle
from collections.abc import Callable
from typing import Any

import cloudpickle


class PrepickledFunction:
    """A function carried as its pickled bytes."""

    __slots__ = ("func_bytes",)

    def __init__(self, func_bytes: bytes) -> None:
        self.func_bytes = func_bytes

    def __reduce__(self) -> tuple[Callable[[bytes], Any], tuple[bytes]]:
        return (pickle.loads, (self.func_bytes,))


def build_execute_payload(
    func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> bytes:
    """The ``/execute`` body for ``func_bytes`` called with ``args`` and ``kwargs``."""
    payload: bytes = cloudpickle.dumps(
        {
            "func": PrepickledFunction(func_bytes),
            "args": args,
            "kwargs": kwargs,
        }
    )
    return payload

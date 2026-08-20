"""The stable public API surface.

``zakuro.public`` re-exports the subset of the API that ships a stability
guarantee. Anything here is:

- **Import-safe** without the ``[worker]`` extra (you can use it as a
  client with just the core install).
- **Covered by tests** against a known behaviour contract.
- **Bound by a deprecation policy** — at least two minor releases of
  warning before a name or signature changes in a breaking way.

Everything else under ``zakuro.*`` is internal. It may still be useful
and is still supported, but future refactors can move, rename, or remove
it without a formal deprecation cycle. Pin to ``zakuro.public.*`` when
you want stability.

Versioning: the surface here is tracked alongside SemVer. Breaking
changes to anything in this module require a major-version bump; minor
versions may add new symbols but never remove existing ones.
"""

from __future__ import annotations

from zakuro import __build__, __version__
from zakuro.adaptive import AdaptiveCompute, DecisionReplay, replay_decisions
from zakuro.compute import Compute
from zakuro.config import Config
from zakuro.fn import Fn, cls, fn
from zakuro.standalone import detect_backend, is_standalone
from zakuro.worker.runner import Worker

__all__ = [
    # decorators
    "fn",
    "cls",
    # core types
    "Compute",
    "AdaptiveCompute",
    "DecisionReplay",
    "replay_decisions",
    "Fn",
    "Worker",
    "Config",
    # helpers
    "detect_backend",
    "is_standalone",
    # version
    "__version__",
    "__build__",
]

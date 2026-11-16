"""Deprecation machinery backing the policy in ``docs/stability.md``.

A stable symbol scheduled for removal calls :func:`warn_deprecated` so callers
get a ``DeprecationWarning`` for at least two minor releases before the name is
removed (breaking changes require a major bump). This module is internal
(``zakuro._deprecation``); it is the implementation behind the documented
guarantee, not part of the public surface.
"""

from __future__ import annotations

import warnings


def warn_deprecated(
    name: str,
    *,
    replacement: str | None = None,
    since: str | None = None,
    removal: str | None = None,
    stacklevel: int = 2,
) -> None:
    """Emit a ``DeprecationWarning`` for a deprecated public symbol.

    Args:
        name: the deprecated symbol (e.g. ``"zakuro.old_fn"``).
        replacement: the symbol to use instead, if any.
        since: the version in which ``name`` was deprecated.
        removal: the version in which ``name`` will be removed.
        stacklevel: passed through to :func:`warnings.warn`; the default points
            the warning at the caller of the deprecated symbol.
    """
    msg = f"{name} is deprecated"
    if since:
        msg += f" since v{since}"
    if removal:
        msg += f" and will be removed in v{removal}"
    if replacement:
        msg += f"; use {replacement} instead"
    msg += "."
    warnings.warn(msg, DeprecationWarning, stacklevel=stacklevel + 1)

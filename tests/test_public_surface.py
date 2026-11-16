"""Pins the stable public API surface and the deprecation machinery so the
guarantees in docs/stability.md are actually enforced (not just asserted)."""

from __future__ import annotations

import subprocess
import sys
import warnings

# The stable surface documented in docs/stability.md. Changing this set is a
# deliberate, reviewable act — a breaking change requires a major version bump.
EXPECTED_PUBLIC = {
    "fn",
    "cls",
    "Fn",
    "Compute",
    "AdaptiveCompute",
    # Shipped in the public 0.2.24 surface (zakuro-ai/zakuro); reconciled here.
    "Dataset",
    "dataset",
    "DecisionReplay",
    "replay_decisions",
    "Worker",
    "Config",
    "detect_backend",
    "is_standalone",
    "__version__",
    "__build__",
}


def test_public_all_matches_snapshot() -> None:
    import zakuro.public as public

    assert set(public.__all__) == EXPECTED_PUBLIC


def test_public_symbols_all_resolve() -> None:
    import zakuro.public as public

    for name in public.__all__:
        assert hasattr(public, name), f"zakuro.public.{name} is advertised but missing"


def test_public_is_import_safe_without_worker_extra() -> None:
    """Importing zakuro.public must not pull the [worker] deps (fastapi,
    uvicorn, aioquic, psutil) — NF4. Checked in a clean subprocess so other
    tests' imports don't pollute sys.modules."""
    code = (
        "import sys, zakuro.public\n"
        "worker_deps = ('fastapi', 'uvicorn', 'aioquic', 'psutil')\n"
        "leaked = [m for m in worker_deps if m in sys.modules]\n"
        "assert not leaked, f'public import leaked worker deps: {leaked}'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_warn_deprecated_emits_deprecation_warning() -> None:
    from zakuro._deprecation import warn_deprecated

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_deprecated("old_name", replacement="new_name", since="0.3", removal="0.5")

    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)
    msg = str(caught[0].message)
    assert "old_name" in msg
    assert "new_name" in msg
    assert "0.5" in msg

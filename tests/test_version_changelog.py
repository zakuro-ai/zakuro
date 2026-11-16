"""Guard against drift between ``zakuro.__version__`` and CHANGELOG.md (ZAK-0018)."""

from __future__ import annotations

import re
from pathlib import Path

import zakuro

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_HEADING = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.MULTILINE)


def _newest_released() -> tuple[int, int, int]:
    versions = [tuple(int(p) for p in m.groups()) for m in _HEADING.finditer(CHANGELOG.read_text())]
    assert versions, "CHANGELOG.md has no released version heading"
    return max(versions)  # type: ignore[return-value]


def test_version_is_not_below_newest_changelog_heading():
    current = tuple(int(p) for p in zakuro.__version__.split(".")[:3])
    newest = _newest_released()
    assert current >= newest, (
        f"zakuro.__version__ {zakuro.__version__} is below the newest CHANGELOG "
        f"heading {'.'.join(map(str, newest))}; bump zakuro/__init__.py or fix the CHANGELOG"
    )

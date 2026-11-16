"""Guards the workflow branch lists against the defect in #25: `build.yml` did
not list `master` — the repo's default branch — so PRs into `master` never ran
the Build gate at all. These tests fail if a workflow drifts off the default
branch again."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# The repo's default branch. Everything gated on a branch list must include it.
DEFAULT_BRANCH = "master"

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def _triggers(path: Path) -> dict:
    """Return the workflow's `on:` block.

    YAML 1.1 resolves the bare key `on` to the boolean True, so accept both
    spellings rather than depending on the loader's behaviour.
    """
    doc = yaml.safe_load(path.read_text())
    on = doc.get("on", doc.get(True))
    return on if isinstance(on, dict) else {}


def test_workflow_dir_is_not_empty() -> None:
    # Keeps the parametrised tests below from passing vacuously.
    assert _workflow_files()


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_branch_filters_include_default_branch(path: Path, event: str) -> None:
    trigger = _triggers(path).get(event)
    if not isinstance(trigger, dict) or "branches" not in trigger:
        pytest.skip(f"{path.name} has no {event}.branches filter")

    assert DEFAULT_BRANCH in trigger["branches"], (
        f"{path.name}: on.{event}.branches is {trigger['branches']} and omits "
        f"the default branch {DEFAULT_BRANCH!r}, so the workflow never runs "
        f"for it"
    )

"""The README quickstart must only use addressing the SDK accepts (issue #118)."""

import re
from pathlib import Path

import pytest

from zakuro.compute import _is_node_uri, reject_direct_addressing

README = Path(__file__).resolve().parent.parent / "README.md"
_BLOCK = re.compile(r"```python\n(.*?)```", re.S)
_URI = re.compile(r"""uri\s*=\s*["']([^"']+)["']""")


def _readme_uris() -> list[str]:
    uris: list[str] = []
    for block in _BLOCK.findall(README.read_text(encoding="utf-8")):
        compile(block, "README.md", "exec")  # snippet is valid Python
        uris += _URI.findall(block)
    return uris


def test_readme_has_uri_examples() -> None:
    assert _readme_uris()


@pytest.mark.parametrize("uri", _readme_uris())
def test_readme_uri_accepted_without_allow_direct(
    uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZAKURO_ALLOW_DIRECT", raising=False)
    reject_direct_addressing(uri)  # must not raise


def test_readme_shows_node_addressing() -> None:
    assert any(_is_node_uri(u) for u in _readme_uris())


def test_readme_version_claims_match_package() -> None:
    from zakuro import __version__

    major_minor = ".".join(__version__.split(".")[:2])
    text = README.read_text(encoding="utf-8")
    for claim in re.findall(r"\b(\d+\.\d+)\.x\b", text):
        assert claim == major_minor

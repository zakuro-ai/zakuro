"""Stdlib-only interactive prompt helpers.

Every helper accepts an explicit ``input_stream`` so tests can feed scripted
answers via ``io.StringIO`` without monkey-patching ``sys.stdin``.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Sequence
from typing import IO


def is_interactive(stream: IO[str] | None = None) -> bool:
    """Return True if ``stream`` (default: stdin) is a real terminal."""
    s = stream if stream is not None else sys.stdin
    isatty = getattr(s, "isatty", None)
    return bool(isatty and isatty())


def ask(
    prompt: str,
    *,
    default: str | None = None,
    input_stream: IO[str] | None = None,
) -> str:
    """Ask for a free-form string. Returns ``default`` when input is empty.

    Raises ``ValueError`` if no default is supplied and the user enters nothing.
    """
    stream = input_stream if input_stream is not None else sys.stdin
    suffix = f" [{default}]" if default is not None else ""
    sys.stdout.write(f"{prompt}{suffix}: ")
    sys.stdout.flush()
    line = stream.readline().rstrip("\n")
    if not line:
        if default is None:
            raise ValueError(f"{prompt!r} is required")
        return default
    return line


def ask_choice(
    prompt: str,
    choices: Sequence[str],
    *,
    default: str | None = None,
    input_stream: IO[str] | None = None,
) -> str:
    """Ask the user to pick one of ``choices`` by number. Re-prompts on invalid input."""
    stream = input_stream if input_stream is not None else sys.stdin
    while True:
        sys.stdout.write(f"{prompt}\n")
        for i, choice in enumerate(choices, 1):
            marker = "*" if choice == default else " "
            sys.stdout.write(f"  {marker} {i}) {choice}\n")
        suffix = f" [{default}]" if default else ""
        sys.stdout.write(f"Select 1-{len(choices)}{suffix}: ")
        sys.stdout.flush()
        line = stream.readline().rstrip("\n").strip()
        if not line:
            if default is not None:
                return default
            raise EOFError("no input available for ask_choice and no default supplied")
        try:
            idx = int(line)
        except ValueError:
            sys.stdout.write(f"  (not a number: {line!r})\n")
            continue
        if 1 <= idx <= len(choices):
            return choices[idx - 1]
        sys.stdout.write(f"  (out of range: {idx})\n")


def confirm(
    prompt: str,
    *,
    default: bool = True,
    input_stream: IO[str] | None = None,
) -> bool:
    """Yes/no prompt. Empty input returns ``default``."""
    stream = input_stream if input_stream is not None else sys.stdin
    suffix = " (Y/n)" if default else " (y/N)"
    sys.stdout.write(f"{prompt}{suffix}: ")
    sys.stdout.flush()
    line = stream.readline().rstrip("\n").strip().lower()
    if not line:
        return default
    return line in ("y", "yes")


def secret(prompt: str) -> str:
    """Ask for a secret with echo suppression (delegates to ``getpass``)."""
    return getpass.getpass(f"{prompt}: ")

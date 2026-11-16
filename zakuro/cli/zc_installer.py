"""Wraps the upstream zc installer: `curl -fsSL https://get.zakuro-ai.com/zc | sh`."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

INSTALL_COMMAND = "curl -fsSL https://get.zakuro-ai.com/zc | sh"
MANUAL_INSTALL_URL = "https://github.com/zakuro-ai/zc#installation"

Status = Literal["installed", "already_installed", "curl_missing", "failed"]


@dataclass
class InstallResult:
    status: Status
    ran_subprocess: bool
    returncode: int | None = None
    message: str = ""


def install_zc(*, force: bool = False) -> InstallResult:
    """Install the ``zc`` CLI via the upstream installer.

    Returns an :class:`InstallResult` instead of raising — the wizard decides
    how to render the outcome.
    """
    if shutil.which("zc") and not force:
        return InstallResult(
            status="already_installed",
            ran_subprocess=False,
            message="`zc` is already on PATH.",
        )

    if shutil.which("curl") is None:
        return InstallResult(
            status="curl_missing",
            ran_subprocess=False,
            message=(f"`curl` not found. Install zc manually from {MANUAL_INSTALL_URL}."),
        )

    completed = subprocess.run(INSTALL_COMMAND, shell=True, check=False)
    if completed.returncode == 0:
        return InstallResult(
            status="installed",
            ran_subprocess=True,
            returncode=0,
            message="`zc` installed.",
        )
    return InstallResult(
        status="failed",
        ran_subprocess=True,
        returncode=completed.returncode,
        message=(
            f"Installer exited with code {completed.returncode}. "
            f"Install zc manually from {MANUAL_INSTALL_URL}."
        ),
    )

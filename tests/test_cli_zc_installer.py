"""Tests for zakuro.cli.zc_installer."""

from unittest.mock import MagicMock, patch

from zakuro.cli import zc_installer


class TestInstallZc:
    def test_skips_when_already_installed(self) -> None:
        with patch(
            "shutil.which", side_effect=lambda name: "/usr/local/bin/zc" if name == "zc" else None
        ):
            result = zc_installer.install_zc()
        assert result.status == "already_installed"
        assert result.ran_subprocess is False

    def test_force_runs_even_if_installed(self) -> None:
        with (
            patch(
                "shutil.which",
                side_effect=lambda name: "/usr/local/bin/zc" if name == "zc" else "/usr/bin/curl",
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = zc_installer.install_zc(force=True)
        assert result.status == "installed"
        assert result.ran_subprocess is True

    def test_fails_without_curl(self) -> None:
        with patch("shutil.which", return_value=None):
            result = zc_installer.install_zc()
        assert result.status == "curl_missing"
        assert result.ran_subprocess is False
        assert "github.com/zakuro-ai/zc" in result.message

    def test_runs_expected_command(self) -> None:
        with (
            patch(
                "shutil.which",
                side_effect=lambda name: "/usr/bin/curl" if name == "curl" else None,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            zc_installer.install_zc()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd == "curl -fsSL https://get.zakuro-ai.com/zc | sh"
        assert kwargs.get("shell") is True
        assert kwargs.get("check") is False

    def test_propagates_failure(self) -> None:
        with (
            patch(
                "shutil.which",
                side_effect=lambda name: "/usr/bin/curl" if name == "curl" else None,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=42)
            result = zc_installer.install_zc()
        assert result.status == "failed"
        assert result.returncode == 42

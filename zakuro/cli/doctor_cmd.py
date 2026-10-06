"""`zakuro doctor` — verify the install."""

from __future__ import annotations

import argparse
import shutil
import subprocess


def run(args: argparse.Namespace) -> int:
    del args  # currently no flags
    failures = 0

    # 1. Import check
    try:
        import zakuro

        version = getattr(zakuro, "__version__", "unknown")
        print(f"  [OK]  import zakuro            -> {version}")
    except Exception as exc:  # pragma: no cover — extreme failure mode
        print(f"  [FAIL] import zakuro            -> {exc!r}")
        return 1

    # 2. Backend detection (reuse existing logic, never raises)
    try:
        from zakuro.standalone import detect_backend

        backend = detect_backend()
        backend_str = backend if backend else "standalone (in-process)"
        print(f"  [OK]  detect_backend()         -> {backend_str}")
    except Exception as exc:
        print(f"  [FAIL] detect_backend()         -> {exc!r}")
        failures += 1

    # 3. zc version (only if zc is on PATH)
    zc_path = shutil.which("zc")
    if zc_path is None:
        print("  [..]  zc --version             -> skipped (zc not installed)")
    else:
        try:
            completed = subprocess.run(
                [zc_path, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            line = (
                (completed.stdout or completed.stderr).strip().splitlines()[0]
                if (completed.stdout or completed.stderr)
                else "(no output)"
            )
            if completed.returncode == 0:
                print(f"  [OK]  zc --version             -> {line}")
            else:
                print(f"  [FAIL] zc --version             -> exit {completed.returncode}: {line}")
                failures += 1
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"  [FAIL] zc --version             -> {exc!r}")
            failures += 1

    return 0 if failures == 0 else 1

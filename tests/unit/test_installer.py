"""Installer argument and supported-host contracts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_source_installer_check_validates_supported_host(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    script = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
    script = script.replace(
        'readonly SCS_INSTALLER_VERSION="@SCS_VERSION@"',
        'readonly SCS_INSTALLER_VERSION="0.1.0"',
    )
    installer = tmp_path / "install.sh"
    installer.write_text(script, encoding="utf-8")
    installer.chmod(0o700)

    completed = subprocess.run(
        [str(installer), "--check"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ,
    )

    assert completed.returncode == 0
    assert "prerequisites are available" in completed.stdout


def test_source_installer_requires_an_exact_release_version() -> None:
    root = Path(__file__).parents[2]

    completed = subprocess.run(
        [str(root / "scripts" / "install.sh"), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "requires --version" in completed.stderr

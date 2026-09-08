"""Installer argument and supported-host contracts."""

from __future__ import annotations

import os
import subprocess
import sys
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


def test_installer_stops_daemon_before_replacing_tool() -> None:
    root = Path(__file__).parents[2]
    script = (root / "scripts" / "install.sh").read_text(encoding="utf-8")

    stop_position = script.index("daemon stop --cancel-active")
    install_position = script.index('"$uv_command" tool install')

    assert stop_position < install_position
    assert "daemon stop --cancel-active >/dev/null 2>&1 || true" not in script


def test_installer_treats_zombie_daemon_as_exited(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    script = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
    function_start = script.index("process_is_running()")
    function_end = script.index("\n}\n", function_start) + len("\n}\n")
    process_probe = tmp_path / "process-probe.sh"
    process_probe.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"{script[function_start:function_end]}\n"
        'process_is_running "$1"\n'
        'if process_is_running "$2"; then exit 1; fi\n',
        encoding="utf-8",
    )
    process_probe.chmod(0o700)

    zombie_owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, time; "
                "child = os.fork(); "
                "child == 0 and os._exit(0); "
                "print(child, flush=True); "
                "time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert zombie_owner.stdout is not None

    try:
        zombie_pid = zombie_owner.stdout.readline().strip()
        completed = subprocess.run(
            [str(process_probe), str(zombie_owner.pid), zombie_pid],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        zombie_owner.terminate()
        zombie_owner.wait(timeout=5)

    assert completed.returncode == 0

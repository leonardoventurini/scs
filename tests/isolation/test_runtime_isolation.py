"""Executable isolation checks for the standalone SCS product."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_scs_imports_from_an_isolated_working_directory(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import scs; import scs.main; print(scs.__version__)",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip()

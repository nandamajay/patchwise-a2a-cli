from __future__ import annotations

import subprocess
from pathlib import Path


def run_shell_command(command: str, cwd: Path, env: dict[str, str]) -> dict:
    proc = subprocess.run(
        ["bash", "-lc", command],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=env,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }

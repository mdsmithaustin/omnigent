from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not os.environ.get("OMNIGENT_PRIME_PATH"),
    reason="Set OMNIGENT_PRIME_PATH to qualified Prime Agent 0.9.6",
)
def test_prime_native_terminal_and_http_messages() -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / ".agents" / "skills" / "verify-prime-native" / "scripts" / "verify.py"
    assert helper.is_file(), "Prime Native verification skill must be installed in this checkout"
    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--repo",
            str(repo),
            "--prime-path",
            os.environ["OMNIGENT_PRIME_PATH"],
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

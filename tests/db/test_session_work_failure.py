from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from omnigent.db import utils

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy_session_work_schema.py"
CANARY = "ap-error-secret-canary"


def test_python_unexpected_failure_retains_safe_evidence(monkeypatch):
    def fail_engine(uri):
        raise TypeError(CANARY)

    monkeypatch.setattr(utils, "_create_engine", fail_engine)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema("sqlite://", role="split-ap", target="mm1a2b3c4d5e")

    error = caught.value
    assert str(error) == "Database schema deployment failed."
    assert [item.kind for item in error.diagnostics] == ["type-error"]
    assert error.diagnostics[0].boundary == "deployment"
    assert error.diagnostics[0].frames[-1].code == "db.utils.deploy_session_work_schema"
    assert CANARY not in json.dumps([asdict(item) for item in error.diagnostics])
    assert error.__context__ is None
    assert error.__cause__ is None


def test_cli_unexpected_failure_retains_safe_evidence():
    code = f"""
import runpy, sys
from omnigent.db import utils
def fail_engine(uri):
    raise TypeError({CANARY!r})
utils._create_engine = fail_engine
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(SCRIPT),
            "--role",
            "split-ap",
            "--target",
            "mm1a2b3c4d5e",
            "--uri-env",
            "SCHEMA_TEST_URI",
        ],
        cwd=ROOT,
        env={**os.environ, "SCHEMA_TEST_URI": "sqlite://"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"] == "Database schema deployment failed."
    assert payload["diagnostics"][0]["kind"] == "type-error"
    assert payload["diagnostics"][0]["boundary"] == "deployment"
    assert CANARY not in result.stderr

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REAL_RUN = subprocess.run


@pytest.fixture
def helper(monkeypatch, tmp_path):
    source = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/verify-prime-native/scripts/verify.py"
    )
    spec = importlib.util.spec_from_file_location("prime_metadata_verify", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Doctor entered a forbidden runtime boundary")

    monkeypatch.setitem(
        sys.modules,
        "omnigent.testing.process_reaper",
        SimpleNamespace(reap_leaked_omnigent_processes=forbidden),
    )
    for name in ("drive", "owned_prime_processes"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.psutil, "process_iter", forbidden)
    monkeypatch.setattr(module.psutil.Process, "environ", forbidden)
    monkeypatch.setattr(module.psutil, "wait_procs", forbidden)
    monkeypatch.setattr(module.shutil, "copytree", forbidden)
    monkeypatch.setattr(module.httpx, "Client", forbidden)
    original_mkdtemp = tempfile.mkdtemp
    allocations = []

    def allocate(*, prefix, dir):
        path = Path(original_mkdtemp(prefix=prefix, dir=tmp_path))
        allocations.append(path)
        return str(path)

    monkeypatch.setattr(module.tempfile, "mkdtemp", allocate)
    monkeypatch.setattr(module.shutil, "which", lambda path: "/fixture/prime-agent")
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **k: "fixture-sha\n")
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        output = {
            "--version": "0.9.6\n",
            "--help": "model List available models --server\n",
            "HEAD": "fixture-sha\n",
        }[argv[-1]]
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(module.subprocess, "run", command)
    monkeypatch.setattr(sys, "argv", [str(source), "--doctor", "--repo", str(tmp_path)])
    monkeypatch.setattr(module, "allocations", allocations, raising=False)
    monkeypatch.setattr(module, "calls", calls, raising=False)
    return module


def test_doctor_never_enters_runtime_finalizer(helper):
    assert helper.main() == 0


@pytest.mark.parametrize("outcome", ["success", "nonzero", "wrong-marker", "timeout"])
def test_doctor_records_outcomes_without_runtime_boundaries(helper, monkeypatch, outcome):
    original_run = helper.subprocess.run

    def run(argv, **kwargs):
        if argv[-1] == "--version":
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, 30)
            if outcome == "nonzero":
                return subprocess.CompletedProcess(argv, 7, "", "")
            if outcome == "wrong-marker":
                return subprocess.CompletedProcess(argv, 0, "WRONG\n", "")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(helper.subprocess, "run", run)
    status = helper.main()
    assert status == (0 if outcome == "success" else 1)
    evidence = helper.allocations[0]
    receipt = json.loads((evidence / "doctor.json").read_text())
    first = receipt["checks"][0]
    assert first["name"] == "prime-version"
    assert (
        first["outcome"]
        == {
            "success": "passed",
            "nonzero": "nonzero-exit",
            "wrong-marker": "missing-marker",
            "timeout": "timeout",
        }[outcome]
    )
    assert json.loads((evidence / "manifest.json").read_text())["exit"] == status
    assert not (evidence / "cleanup.json").exists()
    assert all("prime-native-runtime-" not in path.name for path in helper.allocations)


def test_doctor_module_entry_and_private_profile(helper, monkeypatch, tmp_path):
    class ForbiddenEnvironment:
        def get(self, *_args):
            raise AssertionError("Doctor read a host environment value")

        def __iter__(self):
            raise AssertionError("Doctor copied the host environment")

    monkeypatch.setattr(
        helper, "os", SimpleNamespace(environ=ForbiddenEnvironment(), defpath=os.defpath)
    )
    resolutions = []
    monkeypatch.setattr(
        helper.shutil, "which", lambda name: resolutions.append(name) or "/fixture/prime-agent"
    )
    assert helper.main() == 0
    assert resolutions == ["prime-agent"]
    native = next(call for call in helper.calls if "prime-native" in call[0])
    assert native[0] == [sys.executable, "-m", "omnigent", "prime-native", "--help"]
    assert native[1]["cwd"] == tmp_path
    env = native[1]["env"]
    for key in (
        "HOME",
        "TMPDIR",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_DATA_DIR",
        "PRIME_AGENT_CODING_AGENT_DIR",
    ):
        assert Path(env[key]).is_relative_to(helper.allocations[1])
    assert not helper.allocations[1].exists()


@pytest.mark.parametrize(
    "name,argv,marker",
    [
        ("prime-version", ["/fixture/prime-agent", "--version"], "0.9.6\n"),
        ("prime-help", ["/fixture/prime-agent", "--help"], "model\n"),
        (
            "prime-model-help",
            ["/fixture/prime-agent", "model", "list", "--help"],
            "List available models\n",
        ),
        (
            "native-help",
            [sys.executable, "-m", "omnigent", "prime-native", "--help"],
            "--server\n",
        ),
    ],
)
@pytest.mark.parametrize("outcome", ["success", "nonzero", "wrong-marker", "timeout"])
def test_metadata_output_retained(helper, monkeypatch, name, argv, marker, outcome):
    original_run = helper.subprocess.run
    stdout = marker if outcome in ("success", "nonzero") else "unexpected public output\n"
    stderr = "public command diagnostic\n"

    def run(command, **kwargs):
        if command != argv:
            return original_run(command, **kwargs)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 30, stdout.encode(), stderr.encode())
        return subprocess.CompletedProcess(
            command, 7 if outcome == "nonzero" else 0, stdout, stderr
        )

    monkeypatch.setattr(helper.subprocess, "run", run)
    assert helper.main() == (0 if outcome == "success" else 1)
    evidence = helper.allocations[0]
    assert (evidence / f"{name}.txt").read_text() == stdout + stderr


@pytest.mark.parametrize("outcome", ["nonzero", "timeout"])
def test_metadata_output_is_bounded(helper, monkeypatch, outcome):
    def run(argv, **kwargs):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 30, b"x" * 70000, b"y" * 70000)
        return subprocess.CompletedProcess(argv, 7, "x" * 70000, "y" * 70000)

    monkeypatch.setattr(helper.subprocess, "run", run)
    assert helper.main() == 1
    assert (helper.allocations[0] / "prime-version.txt").read_text() == "x" * 32768 + "y" * 32768


def test_missing_executable_fails_with_receipt(helper, monkeypatch):
    monkeypatch.setattr(helper.shutil, "which", lambda name: None)
    assert helper.main() == 1
    evidence = helper.allocations[0]
    assert json.loads((evidence / "manifest.json").read_text())["exit"] == 1
    assert "unavailable" in (evidence / "failure.txt").read_text()
    assert not helper.calls


def test_receipt_write_failure_cannot_report_success(helper, monkeypatch):
    original_write = Path.write_text

    def write(path, *args, **kwargs):
        if path.name == "doctor.json":
            raise OSError("fixture receipt write failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    assert helper.main() == 1
    evidence = helper.allocations[0]
    assert json.loads((evidence / "manifest.json").read_text())["exit"] == 1
    assert "receipt write failure" in (evidence / "failure.txt").read_text()


def test_real_metadata_child_timeout_fails_safely(helper, monkeypatch, tmp_path):
    child = tmp_path / "slow-prime"
    child.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    child.chmod(0o700)
    monkeypatch.setattr(helper.shutil, "which", lambda name: str(child))

    def bounded_run(argv, **kwargs):
        kwargs["timeout"] = 0.05
        return REAL_RUN(argv, **kwargs)

    monkeypatch.setattr(helper.subprocess, "run", bounded_run)
    assert helper.main() == 1
    evidence = helper.allocations[0]
    assert json.loads((evidence / "doctor.json").read_text())["checks"][0]["outcome"] == "timeout"

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest


def _adapter_probe():
    scripts = Path(__file__).parents[1] / ".agents/skills/verify-prime-native/scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "prime_native_adapter_probe_for_test", scripts / "adapter_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(probe, *artifacts: tuple[str, int, str]):
    return probe.SourceSnapshot(
        tuple(probe.SourceArtifact(path, size, digest) for path, size, digest in artifacts)
    )


def _kernel_pair(probe):
    requested = probe.RequestedKernel(
        entry=Path("/kernel/bin/python"),
        venv_root=Path("/kernel"),
        rlm_module=Path("/kernel/rlm.py"),
        rlm_sha256="sha",
    )
    actual = probe.ActualKernelIdentity(
        nonce="nonce",
        pid=42,
        executable=Path("/kernel/bin/python"),
        prefix=Path("/kernel"),
        rlm_module=Path("/kernel/rlm.py"),
        rlm_sha256="sha",
    )
    return requested, actual


def _kernel_witness() -> dict[str, object]:
    return {
        "nonce": "nonce",
        "pid": 42,
        "executable": "/kernel/bin/python",
        "prefix": "/kernel",
        "rlm_module": "/kernel/rlm.py",
        "rlm_sha256": "sha",
    }


def _kernel_record(pid: int = 42, entry: str = "/kernel/bin/python") -> dict[str, object]:
    return {"pid": pid, "argv": [entry, "-m", "rlm.repl"]}


def _selected_root_identity(probe, *, worker_pid: int = 12, worker_started: float = 2.0):
    processes = (
        probe.ProcessIdentity(
            10,
            1.0,
            ("prime-agent", "--extension", "/runtime/root/omnigent_pi_native_extension.js"),
        ),
        probe.ProcessIdentity(
            11,
            1.5,
            (
                "prime-agent",
                "--mode",
                "daemon",
                "--daemon-socket",
                "/runtime/root/tmp/daemon.sock",
            ),
        ),
        probe.ProcessIdentity(
            worker_pid,
            worker_started,
            (
                "prime-agent",
                "--mode",
                "daemon",
                "--daemon-socket",
                "/runtime/root/tmp/worker-root.sock",
            ),
        ),
        probe.ProcessIdentity(13, 3.0, ("/kernel/bin/python", "-m", "rlm.repl")),
    )
    return probe.SelectedRootIdentity(
        session_id="session-1",
        config_path="/runtime/root/controls/config.json",
        bridge_root="/runtime/root",
        agent_dir="/runtime/root/agent",
        binding_pid=10,
        binding_started=1.0,
        binding_incarnation="incarnation-1",
        external_session_id="prime-session-1",
        processes=processes,
        roles=probe.qualify_selected_root_roles(
            processes, binding_pid=worker_pid, kernel_entry="/kernel/bin/python"
        ),
    )


def test_selected_root_identity_ignores_other_root_helper_churn():
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    after = _selected_root_identity(probe)
    raw_before = [{"pid": 90, "started": 9.0, "argv": ["prime-agent", "shutdown"]}]
    raw_after = [{"pid": 91, "started": 10.0, "argv": ["prime-agent", "shutdown"]}]

    probe.assert_same_selected_root_identity(before, after)

    assert raw_before != raw_after
    assert probe.selected_root_identity_record(before)["processes"][2] == {
        "pid": 12,
        "started": 2.0,
        "argv": [
            "prime-agent",
            "--mode",
            "daemon",
            "--daemon-socket",
            "/runtime/root/tmp/worker-root.sock",
        ],
    }


def test_selected_root_rejects_missing_supervisor_role():
    probe = _adapter_probe()
    root = _selected_root_identity(probe)

    with pytest.raises(RuntimeError, match="supervisor"):
        probe.qualify_selected_root_roles(
            tuple(process for process in root.processes if process.pid != 11),
            binding_pid=12,
            kernel_entry="/kernel/bin/python",
        )


def test_selected_root_ignores_transient_extra_process_when_roles_are_stable():
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    extra = probe.ProcessIdentity(90, 9.0, ("prime-helper",))
    after = dataclasses.replace(before, processes=(*before.processes, extra))

    probe.assert_same_selected_root_identity(before, after)


def test_selected_root_identity_rejects_worker_replacement_with_stable_binding():
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    after = _selected_root_identity(probe, worker_pid=14, worker_started=4.0)

    with pytest.raises(RuntimeError, match="selected Prime root process identity"):
        probe.assert_same_selected_root_identity(before, after)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("binding_pid", 14, "binding"),
        ("binding_started", 4.0, "binding"),
        ("binding_incarnation", "incarnation-2", "binding"),
        ("external_session_id", "prime-session-2", "external session"),
    ],
)
def test_selected_root_identity_rejects_binding_or_native_session_change(field, value, message):
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    after = dataclasses.replace(before, **{field: value})

    with pytest.raises(RuntimeError, match=message):
        probe.assert_same_selected_root_identity(before, after)


class _OwnedHostProcess:
    def __init__(self, *, pid: int, started: float, data_dir: Path):
        self.pid = pid
        self._started = started
        self._data_dir = data_dir

    def create_time(self) -> float:
        return self._started

    def environ(self) -> dict[str, str]:
        return {"OMNIGENT_DATA_DIR": str(self._data_dir)}


def test_owned_host_qualification_requires_exact_target_pid_start_and_data_dir(tmp_path: Path):
    probe = _adapter_probe()
    data_dir = tmp_path / "data"
    record = {
        "pid": 42,
        "target": "http://127.0.0.1:8123",
        "mode": "server",
        "server_url": "http://127.0.0.1:8123",
        "started_at": 100,
    }

    qualified = probe.qualify_owned_host_record(
        record,
        "http://127.0.0.1:8123",
        data_dir,
        _OwnedHostProcess(pid=42, started=99.5, data_dir=data_dir),
    )

    assert qualified == {"pid": 42, "started": 99.5, "target": "http://127.0.0.1:8123"}


@pytest.mark.parametrize(
    ("record_update", "process", "message"),
    [
        ({"target": "http://other:8123"}, _OwnedHostProcess, "target"),
        ({"started_at": 10}, _OwnedHostProcess, "creation time"),
    ],
)
def test_owned_host_qualification_rejects_unrelated_or_reused_process(
    tmp_path: Path, record_update, process, message
):
    probe = _adapter_probe()
    data_dir = tmp_path / "data"
    record = {
        "pid": 42,
        "target": "http://127.0.0.1:8123",
        "mode": "server",
        "server_url": "http://127.0.0.1:8123",
        "started_at": 100,
    }
    record.update(record_update)
    started = 99.5 if message == "target" else 20.0

    with pytest.raises(RuntimeError, match=message):
        probe.qualify_owned_host_record(
            record,
            "http://127.0.0.1:8123",
            data_dir,
            process(pid=42, started=started, data_dir=data_dir),
        )


def test_cleanup_verdict_rejects_own_survivor_or_forced_native_fallback():
    probe = _adapter_probe()

    assert not probe.cleanup_is_complete(
        final_owned_census=[{"pid": 42, "started": 1.0, "ownership": "exact_data_dir"}],
        forced_native_fallback=False,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )
    assert not probe.cleanup_is_complete(
        final_owned_census=[],
        forced_native_fallback=True,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )


def test_final_cleanup_retains_a_live_initial_identity_when_fresh_scan_misses_it(
    monkeypatch: pytest.MonkeyPatch,
):
    probe = _adapter_probe()

    class Process:
        pid = 42

        def create_time(self):
            return 1.5

        def is_running(self):
            return True

        def status(self):
            return "sleeping"

        def cmdline(self):
            return ["python", "-m", "rlm.repl"]

    monkeypatch.setattr(probe.psutil, "Process", lambda pid: Process())
    initial = [{"pid": 42, "started": 1.5, "ownership": "exact_data_dir"}]

    retained = probe.live_initial_process_records(initial)

    assert retained == [
        {
            "pid": 42,
            "started": 1.5,
            "argv": ["python", "-m", "rlm.repl"],
            "ownership": "initial_exact_data_dir",
        }
    ]
    assert not probe.cleanup_is_complete(
        final_owned_census=retained,
        forced_native_fallback=False,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )


def test_final_cleanup_fails_when_initial_identity_becomes_unreadable(
    monkeypatch: pytest.MonkeyPatch,
):
    probe = _adapter_probe()

    class Process:
        def create_time(self):
            raise probe.psutil.AccessDenied(pid=42)

    monkeypatch.setattr(probe.psutil, "Process", lambda pid: Process())

    retained = probe.live_initial_process_records(
        [{"pid": 42, "started": 1.5, "ownership": "exact_data_dir"}]
    )

    assert len(retained) == 1
    assert "AccessDenied" in retained[0]["process_error"]
    assert not probe.cleanup_is_complete(
        final_owned_census=retained,
        forced_native_fallback=False,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )


def test_final_cleanup_does_not_claim_a_reused_pid(monkeypatch: pytest.MonkeyPatch):
    probe = _adapter_probe()

    class Process:
        def create_time(self):
            return 2.0

    monkeypatch.setattr(probe.psutil, "Process", lambda pid: Process())

    assert (
        probe.live_initial_process_records(
            [{"pid": 42, "started": 1.5, "ownership": "exact_data_dir"}]
        )
        == []
    )


def test_cleanup_records_failed_session_delete():
    probe = _adapter_probe()
    run = object.__new__(probe.AdapterRun)
    run.url = "http://127.0.0.1:8123"
    run.server = type("Server", (), {"poll": lambda self: None})()
    run.owned_session_ids = ["session-1"]

    class Client:
        def delete(self, path: str) -> httpx.Response:
            assert path == "/v1/sessions/session-1"
            return httpx.Response(
                503,
                text="unavailable",
                request=httpx.Request("DELETE", run.url + path),
            )

    run.client = lambda **kwargs: contextlib.nullcontext(Client())
    steps: list[object] = []
    errors: list[object] = []

    run.delete_owned_sessions(steps, errors)

    assert steps == [{"delete_session": "session-1", "status": 503, "body": "unavailable"}]
    assert len(errors) == 1
    assert "503" in str(errors[0])


def test_stop_owned_host_uses_exact_private_env_and_never_stops_other_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    probe = _adapter_probe()
    run = object.__new__(probe.AdapterRun)
    run.url = "http://127.0.0.1:8123"
    run.runtime = tmp_path / "runtime"
    run.workspace = tmp_path / "workspace"
    data_dir = run.runtime / "data"
    run.env = lambda: {"OMNIGENT_DATA_DIR": str(data_dir)}
    own_record = {
        "pid": 42,
        "target": run.url,
        "mode": "server",
        "server_url": run.url,
        "started_at": 100,
    }
    run.own_host_registry = lambda: [
        {"path": str(data_dir / "daemons" / "own.json"), "record": own_record},
        {
            "path": str(data_dir / "daemons" / "other.json"),
            "record": {**own_record, "pid": 43, "target": "http://other:8123"},
        },
    ]
    monkeypatch.setattr(
        probe.psutil,
        "Process",
        lambda pid: _OwnedHostProcess(pid=pid, started=99.5, data_dir=data_dir),
    )
    calls: list[dict[str, object]] = []

    class Result:
        returncode = 0
        stdout = "stopped\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return Result()

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    steps = []
    errors = []

    run.stop_owned_host(steps, errors)

    assert errors == []
    assert calls == [
        {
            "argv": [
                str(probe.REPO / ".venv/bin/omnigent"),
                "host",
                "stop",
                "--server",
                run.url,
                "--daemon-only",
            ],
            "cwd": run.workspace,
            "env": {"OMNIGENT_DATA_DIR": str(data_dir)},
            "capture_output": True,
            "text": True,
            "timeout": 30,
        }
    ]
    assert steps[0]["host_stop"]["qualified"] == {
        "pid": 42,
        "started": 99.5,
        "target": run.url,
    }


def test_source_snapshot_rejects_byte_drift():
    probe = _adapter_probe()
    before = _snapshot(probe, ("omnigent/runner/app.py", 3, "before"))
    after = _snapshot(probe, ("omnigent/runner/app.py", 3, "after"))

    with pytest.raises(probe.SourceIntegrityError, match="Source integrity changed"):
        probe.assert_same_sources(before, after)

    comparison = probe.compare_source_snapshots(before, after)
    assert comparison["changed"] == [
        {
            "path": "omnigent/runner/app.py",
            "before": {"path": "omnigent/runner/app.py", "bytes": 3, "sha256": "before"},
            "after": {"path": "omnigent/runner/app.py", "bytes": 3, "sha256": "after"},
        }
    ]


@pytest.mark.parametrize(
    ("before", "after", "key", "expected"),
    [
        (
            (("omnigent/host/connect.py", 1, "a"),),
            (("omnigent/host/connect.py", 1, "a"), ("omnigent/new.py", 1, "b")),
            "added",
            ["omnigent/new.py"],
        ),
        (
            (("omnigent/host/connect.py", 1, "a"), ("omnigent/old.py", 1, "b")),
            (("omnigent/host/connect.py", 1, "a"),),
            "removed",
            ["omnigent/old.py"],
        ),
    ],
)
def test_source_snapshot_rejects_membership_changes(before, after, key, expected):
    probe = _adapter_probe()

    comparison = probe.compare_source_snapshots(
        _snapshot(probe, *before), _snapshot(probe, *after)
    )

    assert comparison[key] == expected
    with pytest.raises(probe.SourceIntegrityError, match="Source integrity changed"):
        probe.assert_same_sources(_snapshot(probe, *before), _snapshot(probe, *after))


def test_checkout_origin_rejects_a_module_outside_the_managed_checkout(tmp_path: Path):
    probe = _adapter_probe()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(probe.SourceIntegrityError, match="outside the exact worktree"):
        probe.assert_checkout_origin("omnigent.host.connect", outside, checkout)


def test_deployed_extension_identity_rejects_different_bytes(tmp_path: Path):
    probe = _adapter_probe()
    source = tmp_path / "packaged.js"
    deployed = tmp_path / "deployed.js"
    source.write_text("export const value = 'source';\n", encoding="utf-8")
    deployed.write_text("export const value = 'runtime';\n", encoding="utf-8")

    with pytest.raises(probe.SourceIntegrityError, match="Deployed extension bytes differ"):
        probe.deployed_extension_identity(source, deployed)


def test_fatal_failure_overrides_ten_verified_scenarios(tmp_path: Path):
    probe = _adapter_probe()
    evidence = probe.Evidence(tmp_path)
    for name in probe.REQUIRED:
        evidence.verified(name)

    evidence.record_fatal(RuntimeError("source changed after scenarios"))

    assert evidence.final_verdict == "NOT VERIFIED"
    assert evidence.fatal == [{"type": "RuntimeError", "error": "source changed after scenarios"}]


def test_kernel_identity_qualifies_one_owned_requested_kernel():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    probe.qualify_kernel_identity(
        requested,
        actual,
        "nonce",
        [_kernel_record()],
    )


def test_kernel_identity_rejects_another_owned_kernel_with_wrong_argv():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="owned process argv differs"):
        probe.qualify_kernel_identity(
            requested,
            actual,
            "nonce",
            [_kernel_record(), _kernel_record(43, "/other/bin/python")],
        )


def test_kernel_preflight_keeps_a_real_venv_symlink_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    probe = _adapter_probe()
    checkout = tmp_path / "checkout"
    package = checkout / "rlm"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text(
        "def spawn():\n    pass\n\ndef collect():\n    pass\n",
        encoding="utf-8",
    )
    venv = tmp_path / "venv"
    probe.subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", str(venv)])
    entry = venv / "bin" / "python"
    assert entry.is_symlink()
    monkeypatch.setattr(probe, "REPO", checkout)

    requested, payload = probe.requested_kernel_identity(entry)

    assert requested.entry == entry
    assert requested.venv_root == venv.resolve()
    assert requested.rlm_module == source.resolve()
    assert requested.rlm_sha256 == probe.sha256(source)
    assert payload["executable"] == str(entry)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("executable", "/other/bin/python", "executable"),
        ("prefix", "/other", "prefix"),
        ("module_sha256", "wrong-sha", "hash"),
    ],
)
def test_kernel_preflight_rejects_wrong_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
):
    probe = _adapter_probe()
    venv = tmp_path / "venv"
    entry = venv / "bin" / "python"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    module = venv / "rlm.py"
    module.write_text("rlm", encoding="utf-8")
    payload = {
        "executable": str(entry),
        "prefix": str(venv),
        "module": str(module),
        "module_sha256": probe.sha256(module),
    }
    payload[field] = value
    monkeypatch.setattr(
        probe.subprocess,
        "check_output",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(RuntimeError, match=message):
        probe.requested_kernel_identity(entry)


def test_kernel_identity_rejects_a_missing_witness_file(tmp_path: Path):
    probe = _adapter_probe()

    with pytest.raises(RuntimeError, match="sidecar is missing"):
        probe.read_actual_kernel_identity(tmp_path / "kernel-identity.json")


@pytest.mark.parametrize("contents", ["{", "[]"])
def test_kernel_identity_rejects_malformed_or_non_object_json(tmp_path: Path, contents: str):
    probe = _adapter_probe()
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="sidecar is malformed"):
        probe.read_actual_kernel_identity(sidecar)


@pytest.mark.parametrize("field", list(_kernel_witness()))
def test_kernel_identity_rejects_missing_witness_fields(tmp_path: Path, field: str):
    probe = _adapter_probe()
    payload = _kernel_witness()
    del payload[field]
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=field):
        probe.read_actual_kernel_identity(sidecar)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nonce", 1),
        ("pid", True),
        ("executable", 1),
        ("prefix", 1),
        ("rlm_module", 1),
        ("rlm_sha256", 1),
    ],
)
def test_kernel_identity_rejects_wrongly_typed_witness_fields(
    tmp_path: Path, field: str, value: object
):
    probe = _adapter_probe()
    payload = _kernel_witness()
    payload[field] = value
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=field):
        probe.read_actual_kernel_identity(sidecar)


def test_kernel_identity_rejects_a_nonpositive_pid(tmp_path: Path):
    probe = _adapter_probe()
    payload = _kernel_witness()
    payload["pid"] = 0
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="pid"):
        probe.read_actual_kernel_identity(sidecar)


def test_kernel_identity_rejects_stale_nonce():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="nonce"):
        probe.qualify_kernel_identity(
            requested,
            dataclasses.replace(actual, nonce="stale"),
            "current",
            [_kernel_record()],
        )


def test_kernel_identity_rejects_duplicate_witness_pid():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="exactly one"):
        probe.qualify_kernel_identity(
            requested,
            actual,
            "nonce",
            [_kernel_record(), _kernel_record()],
        )


def test_kernel_identity_rejects_wrong_witnessed_argv():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="argv"):
        probe.qualify_kernel_identity(
            requested,
            actual,
            "nonce",
            [{"pid": 42, "argv": ["/kernel/bin/python", "-m", "wrong"]}],
        )


def test_kernel_identity_rejects_an_unowned_witness_pid():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="exactly one"):
        probe.qualify_kernel_identity(requested, actual, "nonce", [_kernel_record(99)])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("executable", Path("/wrong/bin/python"), "executable"),
        ("prefix", Path("/wrong"), "prefix"),
        ("rlm_module", Path("/wrong/rlm.py"), "module"),
        ("rlm_sha256", "wrong-sha", "hash"),
    ],
)
def test_kernel_identity_rejects_mismatched_requested_fields(field, value, message):
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match=message):
        probe.qualify_kernel_identity(
            requested,
            dataclasses.replace(actual, **{field: value}),
            "nonce",
            [_kernel_record()],
        )


def test_kernel_identity_rejects_matching_bytes_from_another_venv():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="prefix"):
        probe.qualify_kernel_identity(
            requested,
            dataclasses.replace(actual, prefix=Path("/other-kernel")),
            "nonce",
            [_kernel_record()],
        )


def test_kernel_identity_accepts_resolved_directory_aliases_and_lexical_entry(
    tmp_path: Path,
):
    probe = _adapter_probe()
    venv = tmp_path / "venv"
    entry = venv / "bin" / "python"
    module = venv / "lib" / "rlm" / "__init__.py"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(sys.executable)
    module.parent.mkdir(parents=True)
    module.write_text("rlm", encoding="utf-8")
    prefix_alias = tmp_path / "prefix-alias"
    prefix_alias.symlink_to(venv, target_is_directory=True)
    module_alias = tmp_path / "module-alias"
    module_alias.symlink_to(module.parent, target_is_directory=True)
    requested = probe.RequestedKernel(entry, venv.resolve(), module.resolve(), "sha")
    actual = probe.ActualKernelIdentity(
        "nonce",
        42,
        entry,
        prefix_alias,
        module_alias / "__init__.py",
        "sha",
    )

    probe.qualify_kernel_identity(requested, actual, "nonce", [_kernel_record(42, str(entry))])


def test_kernel_identity_failure_makes_memory_not_verified_and_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    probe = _adapter_probe()
    before = _snapshot(probe, ("omnigent/runner/app.py", 1, "before"))
    cleanup_called: list[bool] = []
    requested, actual = _kernel_pair(probe)

    class FailingRun:
        def __init__(self, _args, evidence, requested_kernel):
            self.evidence = evidence
            self.requested_kernel = requested_kernel

        def start(self):
            return

        def cleanup(self):
            cleanup_called.append(True)
            return True, {"owned_survivors": []}

    class ParsedArgs:
        serve_mcp = False
        prime_path = tmp_path / "prime-agent"
        kernel_python = tmp_path / "kernel-python"
        evidence_dir = tmp_path
        production_ready = None

    class ParsedParser:
        def parse_args(self):
            return ParsedArgs()

    monkeypatch.setattr(probe, "parser", lambda: ParsedParser())
    monkeypatch.setattr(probe, "source_snapshot", lambda: before)
    monkeypatch.setattr(probe, "source_identity", lambda _args, _snapshot: ({}, requested))
    monkeypatch.setattr(probe, "AdapterRun", FailingRun)

    def failing_drive(run):
        for name in probe.REQUIRED:
            if name not in {"living_python_memory", "owned_cleanup"}:
                run.evidence.verified(name)
        assert not probe.exercise(
            "living_python_memory",
            run.evidence,
            lambda: probe.qualify_kernel_identity(
                run.requested_kernel,
                actual,
                "nonce",
                [_kernel_record(), _kernel_record(43, "/other/bin/python")],
            ),
        )

    monkeypatch.setattr(probe, "drive", failing_drive)

    assert probe.main() == 1
    stdout = capsys.readouterr().out
    outcomes = json.loads((tmp_path / "outcomes.json").read_text(encoding="utf-8"))
    integrity = json.loads((tmp_path / "source-integrity.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert cleanup_called == [True]
    assert len(outcomes["scenarios"]) == 10
    assert set(outcomes["scenarios"]) == set(probe.REQUIRED)
    assert outcomes["scenarios"]["living_python_memory"] == {
        "actions": [],
        "observations": [],
        "reason": "RuntimeError: Kernel identity owned process argv differs",
        "verdict": "NOT VERIFIED",
    }
    assert outcomes["scenarios"]["owned_cleanup"]["verdict"] == "VERIFIED"
    assert all(
        scenario["verdict"] == "VERIFIED"
        for name, scenario in outcomes["scenarios"].items()
        if name != "living_python_memory"
    )
    assert outcomes["fatal"] == []
    assert outcomes["final_verdict"] == "NOT VERIFIED"
    assert integrity["comparison"] == {"added": [], "removed": [], "changed": []}
    assert manifest["exit"] == 1
    assert manifest["final_verdict"] == "NOT VERIFIED"
    assert "NOT VERIFIED final" in stdout


def test_cleanup_and_integrity_failures_are_both_retained(tmp_path: Path, monkeypatch):
    probe = _adapter_probe()
    before = _snapshot(probe, ("omnigent/runner/native/interrupt.py", 1, "before"))
    after = _snapshot(probe, ("omnigent/runner/native/interrupt.py", 1, "after"))

    class FailingRun:
        def __init__(self, _args, _evidence, _requested):
            return

        def start(self):
            raise RuntimeError("scenario setup failed")

        def cleanup(self):
            raise RuntimeError("cleanup failed")

    class ParsedArgs:
        serve_mcp = False
        prime_path = tmp_path / "prime-agent"
        kernel_python = tmp_path / "kernel-python"
        evidence_dir = tmp_path
        production_ready = None

    class ParsedParser:
        def parse_args(self):
            return ParsedArgs()

    snapshots = iter((before, after))
    monkeypatch.setattr(probe, "parser", lambda: ParsedParser())
    monkeypatch.setattr(probe, "source_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(probe, "source_identity", lambda _args, _snapshot: ({}, object()))
    monkeypatch.setattr(probe, "AdapterRun", FailingRun)

    assert probe.main() == 1
    outcomes = (tmp_path / "outcomes.json").read_text(encoding="utf-8")
    integrity = (tmp_path / "source-integrity.json").read_text(encoding="utf-8")

    assert "scenario setup failed" in outcomes
    assert "cleanup failed" in outcomes
    assert "Source integrity changed" in outcomes
    assert '"after"' in integrity

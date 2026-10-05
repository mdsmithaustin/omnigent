from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest


@pytest.fixture
def probe():
    scripts = Path(__file__).parents[1] / ".agents/skills/verify-prime-native/scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("mcp_probe_for_test", scripts / "mcp_probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def request(probe, tmp_path, *, cooperative=True):
    source = tmp_path / "synthetic-source"
    source.write_text("SYNTHETIC SOURCE SENTINEL")
    return probe.Request(
        tmp_path / "no-prime",
        Path(sys.executable),
        source,
        tmp_path,
        cooperative_cleanup=cooperative,
    )


@pytest.fixture
def run(probe, tmp_path):
    if sys.platform != "darwin":
        pytest.skip("Requires admitted Darwin vnode events")
    req = request(probe, tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    provider_request = probe.owner.Request(
        req.prime_path,
        req.kernel_entry,
        req.auth_source,
        req.evidence_parent,
        "selector",
        cooperative_cleanup=req.cooperative_cleanup,
    )
    runtime_owner = probe.owner._RuntimeOwner.allocate(provider_request, evidence)
    subject = probe._McpRun(req, provider_request, runtime_owner)
    yield subject
    settlement = subject.fixture.close()
    with contextlib.suppress(Exception):
        subject.cleanup(settlement)
    with contextlib.suppress(Exception):
        runtime_owner.__exit__(None, None, None)


def test_real_fixture_generations_settle_before_scratch_removal(probe, run):
    producer = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.cli_diagnostics import setup_cli_logging; "
            "import logging; setup_cli_logging(['synthetic-mcp']); logging.shutdown()",
        ],
        env={**os.environ, "OMNIGENT_DATA_DIR": str(run.runtime / "data")},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert producer.returncode == 0, producer.stderr
    assert (run.runtime / "data/logs/cli/latest-cli.log").is_symlink()
    first = run.fixture.start(1)
    assert first.process.poll() is None
    assert run.fixture.refused() is False
    run.fixture.stop(first)
    assert run.fixture.refused() is True
    second = run.fixture.start(2)
    assert second.identity != first.identity
    assert second.startup_nonce != first.startup_nonce
    assert probe.owner.identity_alive(first.identity) is False
    settlement = run.fixture.close()
    assert settlement.status == "settled_fixture"
    assert settlement.allocation_id == run.runtime_owner.allocation_id
    assert settlement.errors == ()
    receipt = json.loads((run.evidence / "fixture-cleanup.json").read_text())
    assert receipt["passed"] is True
    assert receipt["endpoint_refused"] is True
    assert receipt["outputs_closed"] == [True, True]
    assert receipt["capture_mode"] == "child_direct_to_file"
    assert len(receipt["generations"]) == 2
    assert all(item["exit"] is not None for item in receipt["generations"])
    assert probe.owner.identity_alive(second.identity) is False
    cleanup = run.cleanup(settlement)
    assert cleanup.status == "VERIFIED"
    assert run.runtime_owner.removed is not None
    assert run.runtime.exists() is False
    alias = json.loads((run.evidence / "runtime-cli-alias-remove.json").read_text())
    assert alias["action"] == "unlink_alias"
    assert alias["allocation_id"] == settlement.allocation_id
    assert not (run.evidence / "owned-logs/cli/latest-cli.log").exists()
    errors = []
    run.finalize_owned_credentials(errors)
    assert errors == []
    assert run.scenario_request.auth_source.read_text() == "SYNTHETIC SOURCE SENTINEL"


@pytest.mark.parametrize(
    "failure", ["stop_receipt", "cleanup_receipt", "output_close", "endpoint"]
)
def test_real_fixture_close_failures_never_settle(probe, run, monkeypatch, failure):
    handle = run.fixture.start(1)
    original_write = probe.owner.write_json
    original_output = handle.stdout
    listener = None
    if failure.endswith("receipt"):
        target = "stopped.json" if failure == "stop_receipt" else "fixture-cleanup.json"

        def write(path, *args, **kwargs):
            if path.name == target:
                raise OSError("synthetic receipt failure")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(probe.owner, "write_json", write)
    elif failure == "output_close":

        class FailingOutput:
            @property
            def closed(self):
                return original_output.closed

            def close(self):
                original_output.close()
                raise OSError("synthetic output close failure")

        handle.stdout = FailingOutput()
        run.fixture.outputs[0] = handle.stdout
    else:
        run.fixture.stop(handle)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", run.declaration.port))
        listener.listen()
    try:
        settlement = run.fixture.close()
        assert settlement.status == "failed_fixture"
        assert settlement.allocation_id == run.runtime_owner.allocation_id
        assert settlement.errors
        assert probe.owner.identity_alive(handle.identity) is False
        assert original_output.closed is True
        cleanup = run.cleanup(settlement)
        assert cleanup.status == "FAILED"
        assert run.runtime.is_dir()
        assert run.runtime_owner.removed is None
        monkeypatch.undo()
        handle.stdout = original_output
        run.fixture.outputs[0] = original_output
        if listener:
            listener.close()
        retry = run.fixture.close()
        assert retry.status == "failed_fixture"
        assert run.cleanup(retry).status == "FAILED"
        assert run.scenario_request.auth_source.read_text() == "SYNTHETIC SOURCE SENTINEL"
    finally:
        if listener:
            listener.close()


@pytest.mark.parametrize("failure", ["missing", "no_fixture", "wrong_allocation", "unknown"])
def test_mcp_requires_explicit_fixture_settlement(probe, run, monkeypatch, failure):
    if failure == "missing":
        with pytest.raises(TypeError):
            run.cleanup()
        assert run.runtime.is_dir()
        return
    if failure == "no_fixture":
        with pytest.raises(RuntimeError, match="mcp_fixture_settlement_required"):
            run.cleanup(
                probe.owner._ExternalSettlement(run.runtime_owner.allocation_id, "no_fixture", ())
            )
        assert run.runtime.is_dir()
        return
    if failure == "wrong_allocation":
        settled = run.fixture.close()
        result = run.cleanup(
            probe.owner._ExternalSettlement("different", settled.status, settled.evidence)
        )
        assert result.status == "FAILED"
        assert run.runtime.is_dir()
        return

    def unknown():
        raise ValueError("unknown fixture error")

    def start():
        raise RuntimeError("synthetic scenario stop")

    monkeypatch.setattr(run.fixture, "close", unknown)
    monkeypatch.setattr(run, "start", start)
    draft = run.qualify_mcp()
    assert draft.result.qualified is False
    assert draft.result.cleanup.status == "FAILED"
    assert "unknown fixture error" in " ".join(draft.payload["finalization_errors"])
    assert run.runtime.is_dir()
    monkeypatch.undo()


def test_draft_cannot_publish_inside_owner_lifetime(probe, run, monkeypatch):
    def start():
        raise RuntimeError("synthetic scenario stop")

    monkeypatch.setattr(run, "start", start)
    draft = run.qualify_mcp()
    assert draft.result.cleanup.status == "VERIFIED"
    assert draft.result.qualified is False
    assert draft.payload["failure"] == "synthetic scenario stop"
    assert draft.result.receipt.exists() is False
    assert (run.evidence / "completion.json").exists() is False
    assert run.runtime_owner.removed is not None
    assert run.runtime_owner.closed is False


def synthetic_success(probe, run):
    cleanup = run.cleanup(run.fixture.close())
    assert cleanup.status == "VERIFIED"
    observations = tuple(
        cleanup
        if name == "owned_cleanup"
        else probe.owner.Observation(name, "VERIFIED", (), "synthetic")
        for name in probe.REQUIRED
    )
    result = probe.owner.CaseResult(
        run.evidence / "result.json", probe.REQUIRED, observations, cleanup
    )
    return probe.owner._CaseDraft(
        result,
        {
            "observations": [asdict(item) for item in observations],
            "cleanup": asdict(cleanup),
            "failure": None,
        },
        {"synthetic": True},
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
@pytest.mark.parametrize(
    "failure",
    [None, "constructor", "partial_subclass", "enter", "exit", "owner_receipt", "pin_close"],
)
def test_constructor_and_owner_failures_cannot_publish_success(
    probe, tmp_path, monkeypatch, failure
):
    req = request(probe, tmp_path)
    allocations = []
    allocate = probe.owner._RuntimeOwner.allocate

    def capture(req, evidence):
        allocated = allocate(req, evidence)
        allocations.append(allocated)
        return allocated

    monkeypatch.setattr(probe.owner._RuntimeOwner, "allocate", capture)
    monkeypatch.setattr(probe._McpRun, "qualify_mcp", lambda run: synthetic_success(probe, run))
    if failure in ("constructor", "partial_subclass"):
        original = probe._McpRun.__init__

        def constructor(run, req, provider_req, runtime_owner):
            if failure == "partial_subclass":
                original(run, req, provider_req, runtime_owner)
            (runtime_owner.root / "partial").write_text("retain this diagnostic")
            raise RuntimeError("synthetic constructor failure")

        monkeypatch.setattr(probe._McpRun, "__init__", constructor)
    elif failure == "enter":
        enter = probe.owner._RuntimeOwner.__enter__

        def fail_enter(owner):
            owner.root.rename(tmp_path / "moved")
            return enter(owner)

        monkeypatch.setattr(probe.owner._RuntimeOwner, "__enter__", fail_enter)
    elif failure == "exit":
        exit_owner = probe.owner._RuntimeOwner.__exit__

        def fail_exit(owner, *args):
            exit_owner(owner, *args)
            raise OSError("synthetic owner exit failure")

        monkeypatch.setattr(probe.owner._RuntimeOwner, "__exit__", fail_exit)
    elif failure == "owner_receipt":
        write = probe.owner.write_json

        def fail_write(path, *args, **kwargs):
            if path.name == "runtime-owner.json":
                raise OSError("synthetic owner receipt failure")
            return write(path, *args, **kwargs)

        monkeypatch.setattr(probe.owner, "write_json", fail_write)
    elif failure == "pin_close":
        close = probe.owner.os.close

        def fail_close(fd):
            close(fd)
            if allocations and fd == allocations[0].root_fd and allocations[0].closed:
                raise OSError("synthetic pin close failure")

        monkeypatch.setattr(probe.owner.os, "close", fail_close)
    publish = probe.owner._publish_case

    def publish_after_close(draft):
        assert allocations[0].closed is True
        assert draft.result.receipt.exists() is False
        return publish(draft)

    monkeypatch.setattr(probe.owner, "_publish_case", publish_after_close)
    passed, receipt = probe.qualify(req)
    assert passed is (failure is None)
    value = json.loads(receipt.read_text())
    assert "passed" not in value
    assert value["authority"] == "completion.json"
    completion = json.loads((receipt.parent / "completion.json").read_text())
    assert completion["passed"] is (failure is None)
    assert completion["result_sha256"] == probe.sha256(receipt)
    assert completion["manifest_sha256"] == probe.sha256(receipt.parent / "manifest.json")
    assert req.auth_source.read_text() == "SYNTHETIC SOURCE SENTINEL"
    if failure in ("constructor", "partial_subclass"):
        assert (allocations[0].root / "partial").read_text() == "retain this diagnostic"


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
@pytest.mark.parametrize(
    "failure", ["result", "manifest", "completion", "commit", "hash", "missing", "false"]
)
def test_mcp_publication_and_reader_inverse_controls(probe, tmp_path, monkeypatch, failure):
    req = request(probe, tmp_path)
    monkeypatch.setattr(probe._McpRun, "qualify_mcp", lambda run: synthetic_success(probe, run))
    write = probe.owner.write_json
    if failure in ("result", "manifest", "completion"):
        target = {
            "result": "result.json",
            "manifest": "manifest.json",
            "completion": ".completion-pending.json",
        }[failure]

        def fail_write(path, *args, **kwargs):
            if path.name == target:
                raise OSError("synthetic publication failure")
            return write(path, *args, **kwargs)

        monkeypatch.setattr(probe.owner, "write_json", fail_write)
    elif failure == "commit":

        def fail_commit(*args):
            raise OSError("synthetic commit failure")

        monkeypatch.setattr(probe.owner.os, "rename", fail_commit)
    else:
        publish = probe.owner._publish_case

        def corrupt(draft):
            result = publish(draft)
            marker = result.receipt.parent / "completion.json"
            marker.chmod(0o600)
            if failure == "missing":
                marker.unlink()
            else:
                value = json.loads(marker.read_text())
                value["result_sha256" if failure == "hash" else "passed"] = (
                    "incorrect" if failure == "hash" else False
                )
                marker.write_text(json.dumps(value))
            return result

        monkeypatch.setattr(probe.owner, "_publish_case", corrupt)
    with pytest.raises((OSError, RuntimeError), match=r"synthetic|not_committed"):
        probe.qualify(req)
    (evidence,) = tmp_path.glob("provider-mcp-*")
    if failure in ("result", "manifest", "completion", "commit"):
        assert (evidence / "completion.json").exists() is False
    result = evidence / "result.json"
    if result.exists():
        assert "passed" not in json.loads(result.read_text())
    assert req.auth_source.read_text() == "SYNTHETIC SOURCE SENTINEL"


def test_real_cli_requires_cooperative_acknowledgement(probe, tmp_path):
    req = request(probe, tmp_path)
    command = [
        sys.executable,
        probe.__file__,
        "--prime-path",
        str(req.prime_path),
        "--kernel-python",
        str(req.kernel_entry),
        "--auth-source",
        str(req.auth_source),
        "--evidence-parent",
        str(tmp_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "--cooperative-cleanup" in result.stderr
    assert list(tmp_path.glob("provider-mcp-*")) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_real_cli_preflight_commits_only_failed_case(probe, tmp_path):
    req = request(probe, tmp_path)
    req.prime_path.write_text("SYNTHETIC INVALID PRIME")
    result = subprocess.run(
        [
            sys.executable,
            probe.__file__,
            "--prime-path",
            str(req.prime_path),
            "--kernel-python",
            str(req.kernel_entry),
            "--auth-source",
            str(req.auth_source),
            "--evidence-parent",
            str(tmp_path),
            "--cooperative-cleanup",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    printed = json.loads(result.stdout.splitlines()[-1])
    assert printed["result"] == "FAILED"
    receipt = Path(printed["receipt"])
    value = json.loads(receipt.read_text())
    assert value["failure"] == "prime_artifact_not_admitted"
    assert value["cleanup"]["status"] == "VERIFIED"
    completion = json.loads((receipt.parent / "completion.json").read_text())
    assert completion["passed"] is False
    assert completion["result_sha256"] == probe.sha256(receipt)
    assert completion["manifest_sha256"] == probe.sha256(receipt.parent / "manifest.json")
    assert json.loads((receipt.parent / "runtime-owner.json").read_text())["closed"] is True
    assert req.auth_source.read_text() == "SYNTHETIC SOURCE SENTINEL"


def test_mcp_retired_finalizer_does_not_rediscover_session(probe, run, monkeypatch):
    def start():
        raise RuntimeError("synthetic scenario stop")

    cleanup = run.cleanup

    def cleanup_then_retire(settlement):
        result = cleanup(settlement)
        assert result.status == "VERIFIED"
        run.session_id = "retired-session"
        return result

    def forbidden(*args):
        raise AssertionError("retired credential parent was rediscovered")

    monkeypatch.setattr(run, "start", start)
    monkeypatch.setattr(run, "cleanup", cleanup_then_retire)
    monkeypatch.setattr(run, "recover_owned_session", forbidden)
    monkeypatch.setattr(run, "own_session", forbidden)
    draft = run.qualify_mcp()
    assert draft.result.cleanup.status == "VERIFIED"
    assert draft.payload["finalization_errors"] == []
    assert run.runtime.exists() is False
    assert run.scenario_request.auth_source.read_text() == "SYNTHETIC SOURCE SENTINEL"


def test_programmatic_request_cannot_infer_cooperative_cleanup(probe, tmp_path):
    passed, receipt = probe.qualify(request(probe, tmp_path, cooperative=False))
    assert passed is False
    assert (
        "runtime_cooperative_environment_not_admitted"
        in json.loads(receipt.read_text())["failure"]
    )
    completion = json.loads((receipt.parent / "completion.json").read_text())
    assert completion["passed"] is False
    assert completion["result_sha256"] == probe.sha256(receipt)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
@pytest.mark.parametrize("corruption", ["missing", "hash"])
def test_failed_case_needs_committed_completion_too(probe, tmp_path, monkeypatch, corruption):
    req = request(probe, tmp_path, cooperative=False)
    publish = probe.owner._publish_case

    def corrupt(draft):
        assert draft.result.qualified is False
        result = publish(draft)
        path = result.receipt.parent / "completion.json"
        path.chmod(0o600)
        if corruption == "missing":
            path.unlink()
        else:
            value = json.loads(path.read_text())
            value["manifest_sha256"] = "wrong"
            path.write_text(json.dumps(value))
        return result

    monkeypatch.setattr(probe.owner, "_publish_case", corrupt)
    with pytest.raises(RuntimeError, match="mcp_case_completion_not_committed"):
        probe.qualify(req)
    assert req.auth_source.read_text() == "SYNTHETIC SOURCE SENTINEL"


@pytest.mark.parametrize("failure", ["launch_receipt", "ownership", "pipe"])
def test_real_fixture_partial_start_is_failed_settlement(probe, run, monkeypatch, failure):
    write = probe.owner.write_json
    if failure == "launch_receipt":

        def fail_write(path, *args, **kwargs):
            if path.name == "launch.json":
                raise OSError("synthetic launch receipt failure")
            return write(path, *args, **kwargs)

        monkeypatch.setattr(probe.owner, "write_json", fail_write)
        with pytest.raises(OSError, match="synthetic launch receipt failure"):
            run.fixture.start(1)
    elif failure == "ownership":
        own = run.own

        def fail_own(process):
            own(process)
            raise RuntimeError("synthetic ownership return failure")

        monkeypatch.setattr(run, "own", fail_own)
        with pytest.raises(RuntimeError, match="synthetic ownership return failure"):
            run.fixture.start(1)
        handle = run.fixture.handles[0]
        process = probe.psutil.Process(handle.process.pid)
        handle.identity = probe.ProcessIdentity(
            process.pid, process.create_time(), tuple(process.cmdline())
        )
    else:
        handle = run.fixture.start(1)
        handle.process.stdout = (run.evidence / "unexpected-pipe").open("w")
    handle = run.fixture.handles[0]
    try:
        settlement = run.fixture.close()
        assert settlement.status == "failed_fixture"
        assert settlement.errors
        assert probe.owner.identity_alive(handle.identity) is False
        assert handle.stdout.closed is True
        assert run.fixture.refused() is True
        assert run.cleanup(settlement).status == "FAILED"
        assert run.runtime.is_dir()
    finally:
        if handle.process.stdout is not None:
            handle.process.stdout.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_mcp_first_failure_survives_owner_exit_failure(probe, tmp_path, monkeypatch):
    def fail_start(run):
        raise RuntimeError("first synthetic scenario failure")

    exit_owner = probe.owner._RuntimeOwner.__exit__

    def fail_exit(owner, *args):
        exit_owner(owner, *args)
        raise OSError("later synthetic owner failure")

    monkeypatch.setattr(probe._McpRun, "start", fail_start)
    monkeypatch.setattr(probe.owner._RuntimeOwner, "__exit__", fail_exit)
    passed, receipt = probe.qualify(request(probe, tmp_path))
    assert passed is False
    value = json.loads(receipt.read_text())
    assert value["failure"] == "first synthetic scenario failure"
    assert any("later synthetic owner failure" in item for item in value["finalization_errors"])
    assert value["cleanup"]["status"] == "FAILED"
    assert json.loads((receipt.parent / "completion.json").read_text())["passed"] is False


def test_live_generation_cannot_be_restarted_before_stop(probe, run):
    first = run.fixture.start(1)
    with pytest.raises(RuntimeError, match="generation_order_or_endpoint_not_free"):
        run.fixture.start(2)
    assert len(run.fixture.handles) == 1
    assert first.process.poll() is None
    settlement = run.fixture.close()
    assert settlement.status == "failed_fixture"
    assert probe.owner.identity_alive(first.identity) is False
    assert run.fixture.refused() is True
    assert run.cleanup(settlement).status == "FAILED"
    assert run.runtime.is_dir()


@pytest.mark.parametrize(
    "stale_pin",
    [
        "0" * 64,
        "a4ac20a5c467e8b867d281759b2c193dd0b6d659cc6cd87fc3a8d2f62fba36ed",
        "45b233b559bb9b5206d8db51b4d638dff15fb32872ac2dc64f3a6814ae57c10c",
        "c4d86b911fd5b793ec24453c666b43c14a6736164478051f3b005085cf2847c3",
    ],
    ids=["unknown", "previous-provider", "pre-retirement-provider", "pre-final-error-provider"],
)
def test_mcp_rejects_stale_owner_before_allocation(probe, tmp_path, monkeypatch, stale_pin):
    req = request(probe, tmp_path)
    allocations = []
    monkeypatch.setattr(probe, "OWNER_SHA256", stale_pin)
    monkeypatch.setattr(
        probe.owner._RuntimeOwner, "allocate", lambda *args: allocations.append(args)
    )
    passed, receipt = probe.qualify(req)
    assert passed is False
    assert allocations == []
    assert "mcp_provider_owner_not_admitted" in receipt.read_text()
    assert json.loads((receipt.parent / "completion.json").read_text())["passed"] is False


def test_mcp_current_owner_pin_reaches_provider_boundary(probe, run, monkeypatch):
    def stop_at_provider(subject):
        raise RuntimeError("synthetic_provider_boundary")

    monkeypatch.setattr(probe.owner._OwnedRun, "preflight", stop_at_provider)
    with pytest.raises(RuntimeError, match="synthetic_provider_boundary"):
        run.preflight()


@pytest.mark.parametrize("poll_pass", [1, 2])
@pytest.mark.parametrize(
    "error,expected_class",
    [
        ("connection refused SYNTHETIC_SECRET", "transport"),
        ([], "malformed"),
        ({}, "malformed"),
        (False, "malformed"),
        (0, "malformed"),
    ],
    ids=["transport", "empty-list", "empty-dict", "false", "zero"],
)
def test_mcp_completed_reply_retains_native_failure(probe, run, poll_pass, error, expected_class):
    import time

    header = {"id": "session", "type": "session"}
    entries = [
        header,
        {
            "id": "user",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "PROMPT"},
        },
        {
            "id": "final",
            "type": "message",
            "parentId": "user",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": error,
                "content": [{"type": "text", "text": ""}],
            },
        },
    ]
    run.session_id, run.external_id = "conversation", "session"
    run.journal_path = run.runtime / "synthetic-journal"
    pages = iter([entries[:2], entries] if poll_pass == 2 else [entries])
    run.journal = lambda: next(pages)
    run.items = lambda **kwargs: []
    run.snapshot = lambda **kwargs: {"status": "idle"}
    operation = probe.owner._ReplyOperation(
        "PROMPT",
        "REPLY",
        None,
        time.monotonic() + 5,
        probe.owner._reply_rows([header]),
        (),
        ("session",),
        (),
        run.journal_path,
        "conversation",
        "session",
        ("session",),
        None,
    )
    with pytest.raises(RuntimeError, match=r"^native_assistant_not_successful$"):
        run.completed_reply(operation, "outage")
    text = (run.evidence / "outage-native-failure.json").read_text()
    assert "SYNTHETIC_SECRET" not in text
    observation = json.loads(text)
    assert observation["error_class"] == expected_class
    assert observation["error_present"] is True
    assert observation["operation"] == "outage"
    assert observation["native_reply_id"] == "final"
    assert observation["native_final_position"] == 2
    assert observation["native_user_id"] == "user"
    assert observation["native_user_position"] == 1
    assert observation["session_id"] == "conversation"
    assert observation["native_session_id"] == "session"
    assert observation["baseline_count"] == 1
    assert observation["public_baseline_count"] == 0
    assert observation["reason"] == "native_assistant_not_successful"
    predicates = json.loads((run.evidence / "outage-native-predicates.json").read_text())
    assert predicates["observation_state"] == "previous_poll"
    run.session_id = None

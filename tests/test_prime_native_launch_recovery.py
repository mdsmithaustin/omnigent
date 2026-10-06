from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib.util
import json
import os
import runpy
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import pexpect
import pytest

from omnigent.harnesses.prime_native import bridge, main, process
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.inner.terminal_lifecycle import TerminalLifecycleTrace
from omnigent.runner.native.orchestration import NativeLaunchContext


@pytest.fixture
def launch_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PrimeRuntimePaths:
    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(process, "_ACTIVE_RUNTIMES", set())
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    monkeypatch.setattr(main, "runtime_paths", lambda _: paths)
    monkeypatch.setattr(main, "resolve_prime_executable", lambda: "/qualified/prime-agent")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    return paths


@pytest.mark.parametrize("cancelled", [False, True])
async def test_launch_preserves_dispatch_failure_when_cleanup_is_uncertain(
    launch_paths: PrimeRuntimePaths, cancelled: bool
) -> None:
    failure = (
        asyncio.CancelledError("terminal dispatch cancelled")
        if cancelled
        else RuntimeError("terminal dispatch response lost")
    )
    registry = AsyncMock()
    registry.launch_required_terminal.side_effect = failure
    registry.terminal_registry.close_launch.side_effect = RuntimeError("exact closure unknown")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"workspace": str(launch_paths.root.parent)})
        ),
        base_url="http://omnigent.test",
    ) as client:
        context = NativeLaunchContext(
            session_id="conv_prime_failed_dispatch",
            resource_registry=registry,
            publish_event=lambda *_: None,
            server_client=client,
        )
        with pytest.raises(BaseException) as caught:
            await main.launch_prime_terminal(context)
    assert caught.value is failure
    assert failure.__notes__ == [
        "Prime terminal cleanup retained ownership: exact closure unknown"
    ]


async def test_failed_dispatch_fences_a_delayed_wrapper(
    launch_paths: PrimeRuntimePaths, tmp_path: Path
) -> None:
    dispatched: list[object] = []

    async def lose_response(**kwargs: object) -> None:
        dispatched.append(kwargs["spec"])
        raise RuntimeError("terminal dispatch response lost")

    registry = AsyncMock()
    registry.launch_required_terminal.side_effect = lose_response
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"workspace": str(tmp_path)})
        ),
        base_url="http://omnigent.test",
    ) as client:
        context = NativeLaunchContext(
            session_id="conv_prime_delayed_dispatch",
            resource_registry=registry,
            publish_event=lambda *_: None,
            server_client=client,
        )
        with pytest.raises(RuntimeError):
            await main.launch_prime_terminal(context)
    assert len(dispatched) == 1
    spec = cast(TerminalEnvSpec, dispatched[0])
    marker = tmp_path / "late-child-started"
    child_start = spec.args.index("/qualified/prime-agent")
    command = [
        sys.executable,
        *spec.args[:child_start],
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')",
        str(marker),
    ]
    owner_before = (launch_paths.root / "owner.pid").read_text()
    result = subprocess.run(
        command,
        env={**os.environ, **spec.env, "OMNIGENT_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert {
        "child_started": marker.exists(),
        "wrapper_rejected": result.returncode != 0,
        "owner": (launch_paths.root / "owner.pid").read_text()
        if (launch_paths.root / "owner.pid").exists()
        else None,
    } == {"child_started": False, "wrapper_rejected": True, "owner": owner_before}
    reservation = json.loads((launch_paths.root / "launch.pending.json").read_text())
    assert reservation["state"] == "revoked"


@pytest.mark.parametrize("ordering", ["delayed", "claimed"])
def test_launch_fault_driver_preserves_the_original_wrapper_command(
    tmp_path: Path, ordering: str
) -> None:
    helper = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/verify-prime-native/scripts/launch_probe.py"
    )
    backend = tmp_path / "tmux"
    backend_arguments = tmp_path / "backend-arguments.json"
    backend.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        "if 'display-message' not in sys.argv:\n"
        f" pathlib.Path({str(backend_arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print('tmux 3.6a')\n"
    )
    backend.chmod(0o700)
    shim = runpy.run_path(str(helper))["write_tmux_proxy"](tmp_path, str(backend), ordering)
    env = {"PATH": os.environ["PATH"]}
    version = subprocess.run(
        [str(shim), "-V"], env=env, capture_output=True, text=True, timeout=10, check=False
    )
    assert version.returncode == 0
    assert version.stdout == "tmux 3.6a\n"
    assert json.loads(backend_arguments.read_text()) == ["-V"]
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "terminal.json").write_text("{}")
    original = shlex.join(
        [
            sys.executable,
            "-m",
            "omnigent.harnesses.prime_native.process",
            str(runtime),
            "a" * 32,
            "/published/prime-agent",
            "--offline",
        ]
    )
    (tmp_path / "gate-ready.json").write_text(json.dumps({"command": ["/bin/sh", "-c", original]}))
    result = subprocess.run(
        [
            str(shim),
            "-S",
            str(tmp_path / "tmux.sock"),
            "new-session",
            "-d",
            original,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 73
    from omnigent.process_logging import redact_log_text

    injected = (
        f"PRIME_LAUNCH_ACK_LOST attempt_sha256={hashlib.sha256(('a' * 32).encode()).hexdigest()}"
    )
    assert result.stderr == injected + "\n"
    assert redact_log_text(result.stderr) == result.stderr
    logs = tmp_path / "runner"
    logs.mkdir()
    (logs / "runner.log").write_text(
        redact_log_text(
            f"WARN | Native Prime Native terminal start failed; error_id=err_ab12: "
            f"tmux launch failed (rc=73): {result.stderr}"
        )
    )
    observed = runpy.run_path(str(helper))["launch_error_observation"](tmp_path, injected)
    assert observed["original_error"]["error_id"] == "err_ab12"
    assert observed["original_error_is_primary"] is True
    record = json.loads((tmp_path / "backend-fault.json").read_text())
    assert record["original_command"] == original
    assert record["tmux_returncode"] == 0
    passed = json.loads(backend_arguments.read_text())
    assert passed[:-1] == ["-S", str(tmp_path / "tmux.sock"), "new-session", "-d"]
    if ordering == "claimed":
        assert passed[-1] == original
    else:
        assert shlex.split(passed[-1]) == [
            sys.executable,
            str(helper),
            "--gate",
            str(tmp_path),
            "/bin/sh",
            "-c",
            original,
        ]
    (tmp_path / "fault-disabled").touch()
    repeated = subprocess.run(
        [str(shim), "-S", str(tmp_path / "next.sock"), "new-session", "-d", original],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert repeated.returncode == 0
    assert repeated.stderr == ""
    assert json.loads((tmp_path / "backend-fault.json").read_text()) == record


def test_launch_fault_driver_declines_issue_prompt_and_preserves_web_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = Path(__file__).parents[1] / ".agents/skills/verify-prime-native/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "launch_adapter_probe", scripts / "adapter_probe.py"
    )
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, probe)
    spec.loader.exec_module(probe)
    answer = tmp_path / "answer"
    command = (
        "from pathlib import Path; "
        "reply = input('file a GitHub issue with this report? [Y/n] '); "
        f"Path({str(answer)!r}).write_text(reply)"
    )
    with pexpect.spawn(sys.executable, ["-c", command], encoding="utf-8", timeout=5) as terminal:
        with pytest.raises(pexpect.EOF, match="declined issue report"):
            probe.expect_conversation_id(terminal)
    assert answer.read_text() == "n"
    with pexpect.spawn(
        sys.executable,
        ["-c", "print('Web UI: http://127.0.0.1:8000/c/conv_expected')"],
        encoding="utf-8",
        timeout=5,
    ) as terminal:
        assert probe.expect_conversation_id(terminal) == "conv_expected"


def wrapper_command(paths: PrimeRuntimePaths, token: str, *command: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "omnigent.harnesses.prime_native.process",
        str(paths.root),
        token,
        *command,
    ]


def test_revoked_wrapper_cannot_mutate_a_new_launch(
    launch_paths: PrimeRuntimePaths, tmp_path: Path
) -> None:
    first = process.reserve_prime_launch(launch_paths)
    process.abandon_prime_launch(launch_paths, first, dispatched=True)
    second = process.reserve_prime_launch(launch_paths)
    process.owner_claim.write_owner_claim(launch_paths.root)
    owner = (launch_paths.root / "owner.pid").read_bytes()
    history = launch_paths.session_dir / "saved.jsonl"
    history.write_text("saved transcript")
    marker = tmp_path / "unexpected-child"
    result = subprocess.run(
        wrapper_command(
            launch_paths,
            first.token,
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ),
        env={**os.environ, "OMNIGENT_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "authority was revoked or replaced" in result.stderr
    assert not marker.exists()
    assert process._read_launch_reservation(launch_paths) == second
    assert (launch_paths.root / "owner.pid").read_bytes() == owner
    assert history.read_text() == "saved transcript"
    with pytest.raises(RuntimeError, match="reservation changed"):
        process.abandon_prime_launch(launch_paths, first, dispatched=True)
    assert process._read_launch_reservation(launch_paths) == second


def test_claim_persistence_failure_prevents_child_creation(
    launch_paths: PrimeRuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = process.reserve_prime_launch(launch_paths)
    monkeypatch.setattr(
        sys, "argv", wrapper_command(launch_paths, reservation.token, "unexpected")[2:]
    )
    monkeypatch.setattr(
        process, "_write_launch_reservation", Mock(side_effect=OSError("disk full"))
    )
    prepare = Mock(side_effect=AssertionError("paths prepared without a persisted claim"))
    monkeypatch.setattr(PrimeRuntimePaths, "prepare", prepare)
    with pytest.raises(OSError, match="disk full"):
        process.main()
    assert process._read_launch_reservation(launch_paths) == reservation


def test_wrapper_claim_before_revocation_retains_child_until_qualified_cleanup(
    launch_paths: PrimeRuntimePaths, tmp_path: Path
) -> None:
    reservation = process.reserve_prime_launch(launch_paths)
    marker = tmp_path / "child-pid"
    child = (
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    wrapper = subprocess.Popen(
        wrapper_command(launch_paths, reservation.token, sys.executable, "-c", child),
        env={**os.environ, "OMNIGENT_DATA_DIR": str(tmp_path)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            if marker.exists() and (launch_paths.root / "terminal.json").exists():
                break
            time.sleep(0.01)
        assert marker.exists()
        claimed = process._read_launch_reservation(launch_paths)
        assert claimed is not None and claimed.state == "claimed"
        assert claimed.wrapper is not None and claimed.wrapper.pid == wrapper.pid
        with pytest.raises(RuntimeError, match="supervisor is unreachable"):
            process.abandon_prime_launch(launch_paths, reservation, dispatched=True)
        revoked = process._read_launch_reservation(launch_paths)
        assert revoked is not None and revoked.state == "revoked"
        assert revoked.wrapper == claimed.wrapper
        assert process.psutil.Process(int(marker.read_text())).is_running()
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
        wrapper.wait(timeout=10)
    assert not (launch_paths.root / "owner.pid").exists()


def test_child_record_crash_gap_remains_unknown(
    launch_paths: PrimeRuntimePaths, tmp_path: Path
) -> None:
    reservation = process.reserve_prime_launch(launch_paths)
    marker = tmp_path / "child-pid"
    child = (
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    source = (
        "import sys; from omnigent.harnesses.pi_native import bridge; "
        "from omnigent.harnesses.prime_native import process; "
        "bridge._atomic_json=lambda *a,**k: "
        "(_ for _ in ()).throw(OSError('record unavailable')); "
        "sys.argv=sys.argv[1:]; process.main()"
    )
    log = tmp_path / "wrapper.log"
    with log.open("w") as output:
        wrapper = subprocess.run(
            [
                sys.executable,
                "-c",
                source,
                str(launch_paths.root),
                str(launch_paths.root),
                reservation.token,
                sys.executable,
                "-c",
                child,
            ],
            env={**os.environ, "OMNIGENT_DATA_DIR": str(tmp_path)},
            stdout=output,
            stderr=output,
            text=True,
            timeout=10,
        )
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.01)
        assert wrapper.returncode != 0
        assert "record unavailable" in log.read_text()
        assert marker.exists()
        with pytest.raises(RuntimeError, match="claimed dispatch has no child record"):
            process.abandon_prime_launch(launch_paths, reservation, dispatched=True)
        with pytest.raises(RuntimeError, match="claimed dispatch has no child record"):
            process.stop_prime_runtime(launch_paths)
        assert process._read_launch_reservation(launch_paths).wrapper is not None
        assert (launch_paths.root / "owner.pid").exists()
    finally:
        if marker.exists():
            child_process = process.psutil.Process(int(marker.read_text()))
            child_process.terminate()
            try:
                child_process.wait(timeout=5)
            except process.psutil.TimeoutExpired:
                child_process.kill()


async def test_repeated_cancellation_waits_for_token_revocation(
    launch_paths: PrimeRuntimePaths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    real_abandon = main.abandon_prime_launch

    def delayed_abandon(*args: object, **kwargs: object) -> None:
        entered.set()
        assert release.wait(5)
        real_abandon(*args, **kwargs)

    monkeypatch.setattr(main, "abandon_prime_launch", delayed_abandon)
    failure = asyncio.CancelledError("original cancellation")
    registry = AsyncMock()
    registry.terminal_registry = None
    registry.launch_required_terminal.side_effect = failure
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"workspace": str(tmp_path)})
        ),
        base_url="http://omnigent.test",
    ) as client:
        context = NativeLaunchContext(
            session_id="cancel-recovery",
            resource_registry=registry,
            publish_event=lambda *_: None,
            server_client=client,
        )
        task = asyncio.create_task(main.launch_prime_terminal(context))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    assert caught.value is failure
    assert process._read_launch_reservation(launch_paths).state == "revoked"


async def test_failed_terminal_close_retains_the_exact_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import registry as terminals

    registry = terminals.TerminalRegistry()
    original = RuntimeError("acknowledgment lost")
    instance = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        command="python",
        args=["runtime", "old-token"],
        launch=AsyncMock(side_effect=original),
        close=AsyncMock(side_effect=RuntimeError("close failed")),
    )
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *a, **k: SimpleNamespace(instance=instance, cwd=tmp_path),
    )
    spec = TerminalEnvSpec(command=instance.command, args=instance.args)
    with pytest.raises(RuntimeError) as caught:
        await registry.launch("conv", "prime-native", "main", spec)
    assert caught.value is original
    with pytest.raises(RuntimeError, match="close failed"):
        await registry.close_launch("conv", "prime-native", "main", spec=spec)
    assert registry._failed_launches["conv"][0].instance is instance
    await registry.close_launch(
        "conv",
        "prime-native",
        "main",
        spec=TerminalEnvSpec(command="python", args=["runtime", "new-token"]),
    )
    assert registry._failed_launches["conv"][0].instance is instance
    instance.close.side_effect = None
    await registry.close_launch("conv", "prime-native", "main", spec=spec)
    assert registry._failed_launches == {}


async def test_terminal_close_failure_preserves_private_socket_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner.terminal import TerminalInstance

    private_dir = tmp_path / "terminal"
    private_dir.mkdir()
    socket = private_dir / "tmux.sock"
    socket.touch()
    instance = TerminalInstance(
        name="prime-native", session_key="main", socket_path=socket, private_dir=private_dir
    )
    monkeypatch.setattr(
        TerminalInstance, "_tmux", AsyncMock(side_effect=RuntimeError("inspection failed"))
    )
    with pytest.raises(RuntimeError, match="inspection failed"):
        await instance.close(require_confirmation=True)
    assert socket.exists()
    assert private_dir.exists()


def test_launch_driver_rejects_masked_primary_error(tmp_path: Path) -> None:
    helper = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/verify-prime-native/scripts/launch_probe.py"
    )
    driver = runpy.run_path(str(helper))
    logs = tmp_path / "runner"
    logs.mkdir()
    log = logs / "runner.log"
    original = "tmux launch failed (rc=73): PRIME_LAUNCH_ACK_LOST"
    log.write_text(
        "WARN | Native Prime Native terminal start failed; error_id=err_ab12: cleanup failed\n"
        f"RuntimeError: {original}\n"
    )
    assert [error["message"] for error in driver["primary_launch_failures"](tmp_path)] == [
        "cleanup failed"
    ]
    log.write_text(
        f"WARN | Native Prime Native terminal start failed; error_id=err_ab12: {original}\n"
    )
    assert [error["message"] for error in driver["primary_launch_failures"](tmp_path)] == [
        original
    ]


def test_launch_driver_requires_dead_server_identity_for_closure(tmp_path: Path) -> None:
    helper = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/verify-prime-native/scripts/launch_probe.py"
    )
    driver = runpy.run_path(str(helper))
    backend = tmp_path / "tmux"
    backend.write_text(
        f"#!{sys.executable}\n"
        "import sys; sys.stderr.write('inspection unavailable'); sys.exit(2)\n"
    )
    backend.chmod(0o700)
    fault = {
        "socket": str(tmp_path / "absent.sock"),
        "server_identity": {
            "pid": os.getpid(),
            "created_at": process.psutil.Process().create_time(),
        },
    }
    assert driver["terminal_observation"](str(backend), fault)["closed"] is False
    fault["server_identity"] = {"pid": 99_999_999, "created_at": 1.0}
    assert driver["terminal_observation"](str(backend), fault)["closed"] is False
    backend.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stderr.write('no server running on {fault['socket']}'); sys.exit(1)\n"
    )
    assert driver["terminal_observation"](str(backend), fault)["closed"] is True


@pytest.mark.parametrize("ordering", ["delayed", "claimed"])
def test_launch_driver_requires_backend_and_order_proof(tmp_path: Path, ordering: str) -> None:
    helper = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/verify-prime-native/scripts/launch_probe.py"
    )
    driver = runpy.run_path(str(helper))
    token = "a" * 32
    attempt_digest = hashlib.sha256(token.encode()).hexdigest()
    command = shlex.join(
        [
            sys.executable,
            "-m",
            "omnigent.harnesses.prime_native.process",
            str(tmp_path),
            token,
            "/published/prime-agent",
        ]
    )
    witness = {
        "pid": os.getpid(),
        "created_at": 1.0,
        "ready_at": 1.0,
        "command": ["/bin/sh", "-c", command],
        "launch_token": token,
    }
    fault = {
        "original_command": command,
        "runtime": str(tmp_path),
        "server_identity": {"pid": os.getpid(), "created_at": 1.0},
        "tmux_returncode": 0,
        "injected_returncode": 73,
        "error": f"PRIME_LAUNCH_ACK_LOST attempt_sha256={attempt_digest}",
        "launch_token": token,
        "ordering": ordering,
        "order_witness": witness,
        "fault_at": 2.0,
        "runtime_at_fault": {
            "launch.pending.json": json.dumps({"token": token}),
            "terminal.json": json.dumps(witness),
        },
    }
    assert driver["qualified_fault"](fault, ordering) is True
    assert (
        driver["qualified_fault"](
            {**fault, "error": "PRIME_LAUNCH_ACK_LOST attempt_sha256=" + "0" * 64}, ordering
        )
        is False
    )
    assert (
        driver["qualified_fault"](
            {**fault, "error": f"PRIME_LAUNCH_ACK_LOST token={token}"}, ordering
        )
        is False
    )
    assert driver["qualified_fault"]({**fault, "tmux_returncode": 1}, ordering) is False
    assert driver["qualified_fault"]({**fault, "server_identity": None}, ordering) is False
    assert driver["qualified_fault"]({**fault, "order_witness": {}}, ordering) is False
    assert driver["qualified_fault"]({**fault, "ordering": "wrong"}, ordering) is False
    assert driver["qualified_fault"]({**fault, "runtime_at_fault": {}}, ordering) is False


async def test_cancelled_terminal_launch_retains_instance_after_dispatch_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import registry as terminals

    registry = terminals.TerminalRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()
    launched = []

    async def launch(**kwargs: object) -> None:
        entered.set()
        await release.wait()
        launched.append("created")

    instance = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        command="python",
        args=["runtime", "token"],
        launch=launch,
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *a, **k: SimpleNamespace(instance=instance, cwd=tmp_path),
    )
    spec = TerminalEnvSpec(command=instance.command, args=instance.args)
    task = asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))
    await entered.wait()
    task.cancel("cancelled while tmux creates")
    await asyncio.sleep(0)
    task.cancel("second cancel")
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError, match="cancelled while tmux creates"):
        await task
    assert launched == ["created"]
    assert registry._failed_launches["conv"][0].instance is instance
    await registry.close_launch("conv", "prime-native", "main", spec=spec)
    assert registry._failed_launches == {}


async def test_failed_terminal_close_timeout_retains_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import registry as terminals

    registry = terminals.TerminalRegistry()

    async def close(**kwargs: object) -> None:
        await asyncio.Event().wait()

    instance = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        command="python",
        args=["runtime", "token"],
        launch=AsyncMock(side_effect=RuntimeError("lost reply")),
        close=close,
    )
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *a, **k: SimpleNamespace(instance=instance, cwd=tmp_path),
    )
    monkeypatch.setattr(terminals, "_CLOSE_TIMEOUT_S", 0.01)
    spec = TerminalEnvSpec(command=instance.command, args=instance.args)
    with pytest.raises(RuntimeError, match="lost reply"):
        await registry.launch("conv", "prime-native", "main", spec)
    with pytest.raises(TimeoutError):
        await registry.close_launch("conv", "prime-native", "main", spec=spec)
    assert registry._failed_launches["conv"][0].instance is instance


def test_unreadable_wrapper_identity_prevents_creation(
    launch_paths: PrimeRuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = process.reserve_prime_launch(launch_paths)
    monkeypatch.setattr(
        sys, "argv", ["process", str(launch_paths.root), reservation.token, "unexpected-child"]
    )
    monkeypatch.setattr(
        process, "_process_identity", Mock(side_effect=RuntimeError("identity unreadable"))
    )
    with pytest.raises(RuntimeError, match="identity unreadable"):
        process.main()
    assert process._read_launch_reservation(launch_paths) == reservation
    assert not (launch_paths.root / "terminal.json").exists()


async def test_registered_launch_with_failed_close_keeps_qualified_cleanup_ownership() -> None:
    from omnigent.terminals import registry as terminals

    registry = terminals.TerminalRegistry()
    instance = SimpleNamespace(
        command="python",
        args=["runtime", "token"],
        close=AsyncMock(side_effect=RuntimeError("server closure unconfirmed")),
    )
    registry._by_conversation["conv"] = {("prime-native", "main"): instance}
    spec = TerminalEnvSpec(command=instance.command, args=instance.args)
    with pytest.raises(RuntimeError, match="server closure unconfirmed"):
        await registry.close_launch("conv", "prime-native", "main", spec=spec)
    await registry.cleanup_conversation("conv")
    assert registry._failed_launches["conv"][0].instance is instance
    instance.close.side_effect = None
    await registry.close_launch("conv", "prime-native", "main", spec=spec)
    assert registry._failed_launches == {}


async def test_cancel_during_liveness_retains_exact_terminal_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import registry as terminals

    registry = terminals.TerminalRegistry()
    probing = asyncio.Event()
    closed = []

    async def is_alive() -> bool:
        probing.set()
        await asyncio.Event().wait()
        return True

    async def close(**kwargs: object) -> None:
        closed.append("original-token")

    instance = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        command="python",
        args=["runtime", "original-token"],
        running=True,
        launch=AsyncMock(),
        is_alive=is_alive,
        close=close,
    )
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *a, **k: SimpleNamespace(instance=instance, cwd=tmp_path),
    )
    spec = TerminalEnvSpec(command=instance.command, args=instance.args)
    task = asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))
    await asyncio.wait_for(probing.wait(), 1)
    task.cancel("cancel during liveness")
    with pytest.raises(asyncio.CancelledError, match="cancel during liveness"):
        await task
    await registry.close_launch("conv", "prime-native", "main", spec=spec)
    assert closed == ["original-token"]
    assert registry._failed_launches == {}


@pytest.mark.parametrize("created_server", [False, True])
async def test_absent_failed_terminal_allows_fresh_generic_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created_server: bool
) -> None:
    from omnigent.inner import terminal
    from omnigent.terminals import registry as terminals

    registry = terminals.TerminalRegistry()
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first = terminal.TerminalInstance(
        name="bash", session_key="main", socket_path=first_dir / "tmux.sock", private_dir=first_dir
    )
    fresh = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    instances = iter([first, fresh])
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *a, **k: SimpleNamespace(instance=next(instances), cwd=tmp_path),
    )

    async def subprocess_result(*cmd: str, **kwargs: object) -> SimpleNamespace:
        if "new-session" in cmd:
            if created_server:
                first.socket_path.touch()
            stderr = b"creation reply lost" if created_server else b"failed to start server"
        else:
            assert cmd[-1] == "kill-server"
            stderr = f"no server running on {first.socket_path}".encode()
        return SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", stderr)))

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", subprocess_result)
    spec = TerminalEnvSpec(command="bash")
    with pytest.raises(RuntimeError, match="tmux launch failed"):
        await registry.launch("conv", "bash", "main", spec)
    first.socket_path.unlink(missing_ok=True)
    assert await registry.launch("conv", "bash", "main", spec) is fresh
    assert registry.get("conv", "bash", "main") is fresh
    assert not first_dir.exists()


@pytest.mark.parametrize("masked", [False, True])
def test_launch_driver_attributes_original_error_separately_from_ensure_failure(
    tmp_path: Path, masked: bool
) -> None:
    helper = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/verify-prime-native/scripts/launch_probe.py"
    )
    driver = runpy.run_path(str(helper))
    logs = tmp_path / "runner"
    logs.mkdir()
    injected = "PRIME_LAUNCH_ACK_LOST token=" + "a" * 32
    original = f"tmux launch failed (rc=73): {injected}"
    primary = "cleanup failed" if masked else original
    (logs / "runner.log").write_text(
        f"WARN | Native Prime Native terminal start failed; error_id=err_ab12: {primary}\n"
        f"Traceback (most recent call last):\nRuntimeError: {original}\n"
        "WARN | Native Prime Native terminal start failed; "
        "error_id=err_cd34: supervisor unreachable\n"
    )
    observation = driver["launch_error_observation"](tmp_path, injected)
    assert observation["original_error"]["error_id"] == "err_ab12"
    assert observation["original_error_is_primary"] is (not masked)
    assert observation["later_errors"][0]["error_id"] == "err_cd34"


@pytest.mark.parametrize("server_alive", [False, True])
async def test_qualified_close_checks_captured_server_after_socket_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_alive: bool
) -> None:
    from omnigent.inner import terminal

    directory = tmp_path / "terminal"
    directory.mkdir()
    instance = terminal.TerminalInstance(
        name="bash", session_key="main", socket_path=directory / "tmux.sock", private_dir=directory
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:

        async def subprocess_result(*cmd: str, **kwargs: object) -> SimpleNamespace:
            launched = "new-session" in cmd
            return SimpleNamespace(
                returncode=0 if launched else 1,
                communicate=AsyncMock(
                    return_value=(
                        f"{child.pid}\n".encode() if launched else b"",
                        b""
                        if launched
                        else f"no server running on {instance.socket_path}".encode(),
                    )
                ),
            )

        monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", subprocess_result)
        await instance.launch(cwd=tmp_path)
        if not server_alive:
            child.terminate()
            child.wait(timeout=5)
            await instance.close(require_confirmation=True)
            assert not directory.exists()
        else:
            with pytest.raises(RuntimeError):
                await instance.close(require_confirmation=True)
            assert directory.exists()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


async def test_qualified_close_rejects_unrelated_target_gone_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import terminal

    instance = terminal.TerminalInstance(
        name="bash", session_key="main", socket_path=tmp_path / "tmux.sock", private_dir=tmp_path
    )
    monkeypatch.setattr(
        terminal.asyncio,
        "create_subprocess_exec",
        AsyncMock(
            return_value=SimpleNamespace(
                returncode=1, communicate=AsyncMock(return_value=(b"", b"can't find session main"))
            )
        ),
    )
    with pytest.raises(RuntimeError, match="can't find session"):
        await instance.close(require_confirmation=True)
    assert tmp_path.exists()


@pytest.mark.parametrize("dispatched", [False, True])
async def test_qualified_close_distinguishes_no_dispatch_from_unknown_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dispatched: bool
) -> None:
    from omnigent.inner import terminal

    directory = tmp_path / "terminal"
    directory.mkdir()
    instance = terminal.TerminalInstance(
        name="bash", session_key="main", socket_path=directory / "tmux.sock", private_dir=directory
    )

    async def subprocess_result(*cmd: str, **kwargs: object) -> SimpleNamespace:
        if "new-session" in cmd:
            stderr = b"creation response lost"
        else:
            stderr = f"no server running on {instance.socket_path}".encode()
        return SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", stderr)))

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", subprocess_result)
    if dispatched:
        with pytest.raises(RuntimeError, match="creation response lost"):
            await instance.launch(cwd=tmp_path)
    else:
        with monkeypatch.context() as preexec:
            preexec.setattr(instance, "_tmux_base_cmd", Mock(side_effect=FileNotFoundError))
            with pytest.raises(FileNotFoundError):
                await instance.launch(cwd=tmp_path)
    if dispatched:
        with pytest.raises(RuntimeError, match="no server running"):
            await instance.close(require_confirmation=True)
        assert directory.exists()
    else:
        await instance.close(require_confirmation=True)
        assert not directory.exists()


async def test_post_spawn_oserror_retains_terminal_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import terminal

    directory = tmp_path / "terminal"
    directory.mkdir()
    marker = tmp_path / "started"
    instance = terminal.TerminalInstance(
        name="prime-native",
        session_key="main",
        socket_path=directory / "tmux.sock",
        private_dir=directory,
    )
    monkeypatch.setattr(
        instance,
        "_tmux_base_cmd",
        lambda: [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('started'); time.sleep(60)",
        ],
    )
    create_subprocess_exec = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []
    failure = OSError(errno.EMFILE, "subprocess transport setup failed after spawn")

    async def fail_after_spawn(*cmd: str, stdout: int, stderr: int, env: dict[str, str]) -> None:
        children.append(await create_subprocess_exec(*cmd, stdout=stdout, stderr=stderr, env=env))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        raise failure

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", fail_after_spawn)
    try:
        with pytest.raises(OSError) as caught:
            await instance.launch(cwd=tmp_path)
        assert caught.value is failure
        assert marker.read_text() == "started"
        assert children[0].returncode is None
        monkeypatch.setattr(
            instance,
            "_tmux",
            AsyncMock(
                side_effect=terminal._TmuxTargetGoneError(
                    ["kill-server"],
                    returncode=1,
                    stderr=f"no server running on {instance.socket_path}".encode(),
                )
            ),
        )
        with pytest.raises(RuntimeError, match="no server running"):
            await instance.close(require_confirmation=True)
        assert directory.exists()
        assert process.psutil.Process(children[0].pid).status() != process.psutil.STATUS_ZOMBIE
    finally:
        for child in children:
            if child.returncode is None:
                child.terminate()
            await asyncio.wait_for(child.wait(), 5)


@pytest.mark.parametrize("qualified_close_attempted", [False, True])
async def test_generic_retry_retains_prime_terminal_with_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qualified_close_attempted: bool
) -> None:
    from omnigent.inner import terminal
    from omnigent.terminals import registry as terminals

    directory = tmp_path / "terminal"
    directory.mkdir()
    original = terminal.TerminalInstance(
        name="prime-native",
        session_key="main",
        socket_path=directory / "tmux.sock",
        private_dir=directory,
        command=sys.executable,
        args=["-m", "omnigent.harnesses.prime_native.process", "runtime", "original-token"],
    )
    registry = terminals.TerminalRegistry()
    fresh = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    instances = iter([original, fresh])
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *a, **k: SimpleNamespace(instance=next(instances), cwd=tmp_path),
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    async def subprocess_result(*cmd: str, **kwargs: object) -> SimpleNamespace:
        launched = "new-session" in cmd
        return SimpleNamespace(
            returncode=1,
            communicate=AsyncMock(
                return_value=(
                    f"{child.pid}\n".encode() if launched else b"",
                    b"creation reply lost"
                    if launched
                    else f"no server running on {original.socket_path}".encode(),
                )
            ),
        )

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", subprocess_result)
    spec = TerminalEnvSpec(command=original.command, args=original.args)
    try:
        with pytest.raises(RuntimeError, match="creation reply lost"):
            await registry.launch("conv", "prime-native", "main", spec)
        if qualified_close_attempted:
            with pytest.raises(RuntimeError, match="no server running"):
                await registry.close_launch("conv", "prime-native", "main", spec=spec)
        with pytest.raises(RuntimeError, match="no server running"):
            await registry.launch("conv", "prime-native", "main", TerminalEnvSpec(command="bash"))
        assert registry._failed_launches["conv"][0].instance is original
        assert directory.exists()
        assert child.poll() is None
        child.terminate()
        child.wait(timeout=5)
        assert (
            await registry.launch("conv", "prime-native", "main", TerminalEnvSpec(command="bash"))
            is fresh
        )
        assert registry.get("conv", "prime-native", "main") is fresh
        assert not directory.exists()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_launch_driver_attributes_each_injection_to_its_own_error_block(tmp_path: Path) -> None:
    helper = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/verify-prime-native/scripts/launch_probe.py"
    )
    driver = runpy.run_path(str(helper))
    logs = tmp_path / "runner"
    logs.mkdir()
    first = "PRIME_LAUNCH_ACK_LOST token=" + "a" * 32
    second = "PRIME_LAUNCH_ACK_LOST token=" + "b" * 32
    (logs / "runner.log").write_text(
        "WARN | Native Prime Native terminal start failed; error_id=err_ab12: "
        f"tmux launch failed (rc=73): {first}\nTraceback\nRuntimeError: {first}\n"
        f"ERROR Native ensure failed\nTraceback\nRuntimeError: {second}\n"
        "WARN | Native Prime Native terminal start failed; error_id=err_cd34: "
        f"tmux launch failed (rc=73): {second}\nTraceback\nRuntimeError: {second}\n"
    )
    for injected, error_id in [(first, "err_ab12"), (second, "err_cd34")]:
        observed = driver["launch_error_observation"](tmp_path, injected)
        assert observed["original_error"]["error_id"] == error_id
        assert observed["original_error_is_primary"] is True
    missing = driver["launch_error_observation"](tmp_path, "unobserved token")
    assert missing["original_error_is_primary"] is False
    assert missing["original_error"] is None


@pytest.mark.parametrize(
    "actor",
    [
        "new_launch_liveness_false",
        "race_loser",
        "existing_running_liveness_false_replacement",
        "existing_not_running_replacement",
        "registered_explicit_close",
        "registered_conversation_cleanup",
        "registered_shutdown",
        "retained_generic_retry",
        "retained_conversation_cleanup",
        "retained_shutdown",
        "qualified_close_control",
    ],
)
async def test_prime_close_actors_retain_live_owner_until_qualified_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actor: str
) -> None:
    from omnigent.inner import terminal
    from omnigent.terminals import registry as terminals

    directory = tmp_path / "terminal"
    directory.mkdir()
    original = terminal.TerminalInstance(
        name="prime-native",
        session_key="main",
        socket_path=directory / "tmux.sock",
        private_dir=directory,
        command=sys.executable,
        args=["-m", "omnigent.harnesses.prime_native.process", "runtime", "original-token"],
    )
    registry = terminals.TerminalRegistry()
    winner = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        command=sys.executable,
        args=["successor-token"],
        close=AsyncMock(),
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    winner_lock = None
    fresh = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    created = []

    def create(*args: object, **kwargs: object) -> SimpleNamespace:
        if args[2].args == ["successor-token"]:
            return SimpleNamespace(instance=winner, cwd=tmp_path)
        instance = original if not created else fresh
        created.append(instance)
        if actor == "race_loser" and instance is original:
            assert registry.transfer("source", "conv", "prime-native", "main")
        return SimpleNamespace(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(terminals, "create_terminal_instance", create)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    probe_absent = actor == "new_launch_liveness_false"

    async def subprocess_result(*cmd: str, **kwargs: object) -> SimpleNamespace:
        if "new-session" in cmd:
            output = (f"{child.pid}\n".encode(), b"")
            returncode = 0
        elif "list-panes" in cmd and not probe_absent:
            output = (b"0 0\n", b"")
            returncode = 0
        else:
            output = (b"", f"no server running on {original.socket_path}".encode())
            returncode = 1
        return SimpleNamespace(returncode=returncode, communicate=AsyncMock(return_value=output))

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", subprocess_result)
    spec = TerminalEnvSpec(command=original.command, args=original.args)
    error = None
    try:
        if actor == "race_loser":
            assert (
                await registry.launch(
                    "source",
                    "prime-native",
                    "main",
                    TerminalEnvSpec(command=winner.command, args=winner.args),
                )
                is winner
            )
            winner_lock = registry.get_instance_lock("source", "prime-native", "main")
        try:
            launched = await registry.launch("conv", "prime-native", "main", spec)
            if actor not in {"new_launch_liveness_false", "race_loser"}:
                assert launched is original
                if actor.startswith("existing_"):
                    probe_absent = True
                    if actor == "existing_not_running_replacement":
                        original.running = False
                    await registry.launch(
                        "conv", "prime-native", "main", TerminalEnvSpec(command="bash")
                    )
                elif actor == "registered_explicit_close":
                    await registry.close("conv", "prime-native", "main", expected=original)
                elif actor == "registered_conversation_cleanup":
                    await registry.cleanup_conversation("conv")
                elif actor == "registered_shutdown":
                    await registry.shutdown()
                else:
                    with pytest.raises(RuntimeError, match="no server running"):
                        await registry.close_launch("conv", "prime-native", "main", spec=spec)
                    if actor == "retained_generic_retry":
                        await registry.launch(
                            "conv", "prime-native", "main", TerminalEnvSpec(command="bash")
                        )
                    elif actor == "retained_conversation_cleanup":
                        await registry.cleanup_conversation("conv")
                    elif actor == "retained_shutdown":
                        await registry.shutdown()
                    else:
                        await registry.close_launch("conv", "prime-native", "main", spec=spec)
        except RuntimeError as exc:
            error = exc

        assert child.poll() is None
        assert original._tmux_server_identity == (
            child.pid,
            process.psutil.Process(child.pid).create_time(),
        )
        assert directory.exists()
        assert (
            any(entry.instance is original for entry in registry._failed_launches.get("conv", []))
            or registry.get("conv", "prime-native", "main") is original
        )
        assert created == [original]
        if actor == "new_launch_liveness_false":
            assert isinstance(error, terminals.TerminalExitedDuringLaunch)
            assert error.instance is original
        if actor == "race_loser":
            assert registry.get("conv", "prime-native", "main") is winner
            assert registry.get_instance_lock("conv", "prime-native", "main") is winner_lock
        with pytest.raises(RuntimeError, match="no server running"):
            await registry.launch("conv", "prime-native", "main", TerminalEnvSpec(command="bash"))
        assert created == [original]
        assert directory.exists()
        child.terminate()
        child.wait(timeout=5)
        await registry.close_launch("conv", "prime-native", "main", spec=spec)
        assert not directory.exists()
        assert registry._failed_launches == {}
        if actor == "race_loser":
            assert registry.get("conv", "prime-native", "main") is winner
            assert registry.get_instance_lock("conv", "prime-native", "main") is winner_lock
            await registry.close("conv", "prime-native", "main", expected=winner)
        assert (
            await registry.launch("conv", "prime-native", "main", TerminalEnvSpec(command="bash"))
            is fresh
        )
        assert registry.get("conv", "prime-native", "main") is fresh
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_prime_close_retains_original_without_removing_concurrent_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    from omnigent.inner import terminal
    from omnigent.terminals import registry as terminals

    directory = tmp_path / "terminal"
    directory.mkdir()
    original = terminal.TerminalInstance(
        name="prime-native",
        session_key="main",
        socket_path=directory / "tmux.sock",
        private_dir=directory,
    )
    registry = terminals.TerminalRegistry()
    successor = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    instances = iter((original, successor))
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *args, **kwargs: SimpleNamespace(instance=next(instances), cwd=tmp_path),
    )
    monkeypatch.setattr(original, "launch", AsyncMock())
    monkeypatch.setattr(original, "is_alive", AsyncMock(return_value=True))
    spec = TerminalEnvSpec(command="bash")
    assert await registry.launch("conv", "prime-native", "main", spec) is original
    assert await registry.launch("source", "prime-native", "main", spec) is successor
    successor_lock = registry.get_instance_lock("source", "prime-native", "main")
    closing = asyncio.Event()
    release = asyncio.Event()
    failure = OSError("close transport failed")

    async def kill_server(*args: str) -> None:
        closing.set()
        await release.wait()
        raise failure

    monkeypatch.setattr(original, "_tmux", kill_server)
    original.running = True
    task = asyncio.create_task(registry.close("conv", "prime-native", "main", expected=original))
    await asyncio.wait_for(closing.wait(), 1)
    assert registry.transfer("source", "conv", "prime-native", "main")
    if cancelled:
        task.cancel("original close cancelled")
        with pytest.raises(asyncio.CancelledError, match="original close cancelled"):
            await task
    else:
        release.set()
        with pytest.raises(OSError) as caught:
            await task
        assert caught.value is failure
    assert directory.exists()
    assert registry.get("conv", "prime-native", "main") is successor
    assert registry.get_instance_lock("conv", "prime-native", "main") is successor_lock
    assert registry._failed_launches["conv"][0].instance is original


async def test_prime_replacement_rechecks_owner_after_concurrent_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import terminal
    from omnigent.terminals import registry as terminals

    directory = tmp_path / "terminal"
    directory.mkdir()
    original = terminal.TerminalInstance(
        name="prime-native",
        session_key="main",
        socket_path=directory / "tmux.sock",
        private_dir=directory,
    )
    original.running = True
    registry = terminals.TerminalRegistry()
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *args, **kwargs: SimpleNamespace(instance=original, cwd=tmp_path),
    )
    monkeypatch.setattr(original, "launch", AsyncMock())
    monkeypatch.setattr(original, "is_alive", AsyncMock(return_value=True))
    assert (
        await registry.launch("conv", "prime-native", "main", TerminalEnvSpec(command="bash"))
        is original
    )
    probing = asyncio.Event()
    release = asyncio.Event()

    async def is_alive() -> bool:
        probing.set()
        await release.wait()
        return False

    monkeypatch.setattr(original, "is_alive", is_alive)
    monkeypatch.setattr(original, "_tmux", AsyncMock(side_effect=OSError("close unconfirmed")))
    fresh = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    dispatched = []

    def create(*args: object, **kwargs: object) -> SimpleNamespace:
        dispatched.append("new launch")
        return SimpleNamespace(instance=fresh, cwd=tmp_path)

    monkeypatch.setattr(terminals, "create_terminal_instance", create)
    task = asyncio.create_task(
        registry.launch("conv", "prime-native", "main", TerminalEnvSpec(command="bash"))
    )
    await asyncio.wait_for(probing.wait(), 1)
    with pytest.raises(OSError, match="close unconfirmed"):
        await registry.close("conv", "prime-native", "main", expected=original)
    release.set()
    with pytest.raises(OSError, match="close unconfirmed"):
        await task
    assert registry._failed_launches["conv"][0].instance is original
    assert directory.exists()
    assert dispatched == []


async def test_prime_race_loser_preserves_nonrunning_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import registry as terminals

    registry = terminals.TerminalRegistry()
    winner = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    loser = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
        close=AsyncMock(side_effect=OSError("loser close unconfirmed")),
    )

    instances = iter((winner, loser))
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *args, **kwargs: SimpleNamespace(instance=next(instances), cwd=tmp_path),
    )
    spec = TerminalEnvSpec(command="bash")
    assert await registry.launch("source", "prime-native", "main", spec) is winner
    winner_lock = registry.get_instance_lock("source", "prime-native", "main")
    winner.running = False

    async def transfer_during_launch() -> bool:
        assert registry.transfer("source", "conv", "prime-native", "main")
        return True

    loser.is_alive = transfer_during_launch
    with pytest.raises(OSError, match="loser close unconfirmed"):
        await registry.launch("conv", "prime-native", "main", TerminalEnvSpec(command="bash"))
    assert registry.get("conv", "prime-native", "main") is winner
    assert registry.get_instance_lock("conv", "prime-native", "main") is winner_lock
    assert registry._failed_launches["conv"][0].instance is loser


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel_dispatch", "cancel_probe"])
async def test_same_key_launch_waits_for_owner_before_preparing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from omnigent.terminals import registry as terminals

    spawning = asyncio.Event()
    finish_spawn = asyncio.Event()
    probing = asyncio.Event()
    finish_probe = asyncio.Event()
    started_waiter = asyncio.Event()
    dispatched = []
    prepared = []
    close_failure = OSError("first close unconfirmed")

    async def launch(*, cwd: Path) -> None:
        dispatched.append("first")
        spawning.set()
        await finish_spawn.wait()

    async def is_alive() -> bool:
        probing.set()
        await finish_probe.wait()
        return outcome == "success"

    first = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        name="prime-native",
        session_key="main",
        command="prime-agent",
        args=[],
        running=True,
        launch=launch,
        is_alive=is_alive,
        close=AsyncMock(side_effect=close_failure),
        last_exit_status=lambda: None,
    )

    async def second_launch(*, cwd: Path) -> None:
        dispatched.append("second")

    second = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=second_launch,
        is_alive=AsyncMock(return_value=True),
    )

    def create(*args: object, **kwargs: object) -> SimpleNamespace:
        instance = first if not prepared else second
        prepared.append(instance)
        return SimpleNamespace(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(terminals, "create_terminal_instance", create)
    registry = terminals.TerminalRegistry()
    spec = TerminalEnvSpec(command="prime-agent")
    owner = asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))

    async def queued_launch() -> object:
        started_waiter.set()
        return await registry.launch("conv", "prime-native", "main", spec)

    waiter = None
    try:
        await asyncio.wait_for(spawning.wait(), 1)
        if outcome != "cancel_dispatch":
            finish_spawn.set()
            await asyncio.wait_for(probing.wait(), 1)
        waiter = asyncio.create_task(queued_launch())
        await asyncio.wait_for(started_waiter.wait(), 1)
        await asyncio.sleep(0.03)
        assert dispatched == ["first"], "second same-key dispatch before owner settled"
        assert prepared == [first], "second same-key preparation before owner settled"
        if outcome.startswith("cancel_"):
            owner.cancel("owner cancelled")
            await asyncio.sleep(0)
            if outcome == "cancel_dispatch":
                assert not owner.done()
                assert dispatched == ["first"]
            finish_spawn.set()
            with pytest.raises(asyncio.CancelledError, match="owner cancelled"):
                await owner
        else:
            finish_probe.set()
            if outcome == "failure":
                with pytest.raises(terminals.TerminalExitedDuringLaunch) as caught:
                    await owner
                assert caught.value.instance is first
                assert caught.value.__cause__ is close_failure
            else:
                assert await owner is first
        if outcome == "success":
            assert await asyncio.wait_for(waiter, 1) is first
            assert registry.get("conv", "prime-native", "main") is first
        else:
            with pytest.raises(OSError) as caught:
                await asyncio.wait_for(waiter, 1)
            assert caught.value is close_failure
            assert registry._failed_launches["conv"][0].instance is first
            assert registry.get("conv", "prime-native", "main") is None
            first.close.side_effect = None
            assert await registry.launch("conv", "prime-native", "main", spec) is second
            assert registry.get("conv", "prime-native", "main") is second
            assert registry._failed_launches == {}
        assert dispatched == (["first"] if outcome == "success" else ["first", "second"])
        assert registry._launch_locks == {}
    finally:
        finish_spawn.set()
        finish_probe.set()
        for task in (owner, waiter):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (owner, waiter) if task), return_exceptions=True)


async def test_waiting_launch_cancellation_and_cleanup_preserve_other_waiters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import registry as terminals

    probing = asyncio.Event()
    finish_probe = asyncio.Event()
    prepared = []

    async def is_alive() -> bool:
        probing.set()
        await finish_probe.wait()
        return True

    instance = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=is_alive,
        close=AsyncMock(),
    )

    def create(*args: object, **kwargs: object) -> SimpleNamespace:
        prepared.append("terminal")
        return SimpleNamespace(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(terminals, "create_terminal_instance", create)
    registry = terminals.TerminalRegistry()
    spec = TerminalEnvSpec(command="prime-agent")
    tasks = [asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))]
    try:
        await asyncio.wait_for(probing.wait(), 1)
        tasks.extend(
            asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))
            for _ in range(2)
        )
        await asyncio.sleep(0.03)
        tasks[1].cancel("waiting launch cancelled")
        with pytest.raises(asyncio.CancelledError, match="waiting launch cancelled"):
            await tasks[1]
        await registry.cleanup_conversation("conv")
        await registry.shutdown()
        tasks.append(asyncio.create_task(registry.launch("conv", "prime-native", "main", spec)))
        await asyncio.sleep(0.03)
        assert prepared == ["terminal"], (
            "cleanup retired a lock still owned by active or waiting calls"
        )
        assert not tasks[0].done()
        assert not tasks[2].done()
        assert not tasks[3].done()
        finish_probe.set()
        assert await asyncio.wait_for(tasks[0], 1) is instance
        assert await asyncio.wait_for(tasks[2], 1) is instance
        assert await asyncio.wait_for(tasks[3], 1) is instance
        assert registry._launch_locks == {}
        for _ in range(3):
            assert await registry.close("conv", "prime-native", "main", expected=instance)
            assert await registry.launch("conv", "prime-native", "main", spec) is instance
            assert registry._launch_locks == {}
        assert prepared == ["terminal"] * 4
    finally:
        finish_probe.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "other_key",
    [
        ("other", "prime-native", "main"),
        ("conv", "bash", "main"),
        ("conv", "prime-native", "other"),
    ],
)
async def test_different_terminal_keys_launch_while_owner_is_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_key: tuple[str, str, str]
) -> None:
    from omnigent.terminals import registry as terminals

    probing = asyncio.Event()
    release = asyncio.Event()

    async def is_alive() -> bool:
        probing.set()
        await release.wait()
        return True

    first = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=is_alive,
    )
    second = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    instances = iter((first, second))
    monkeypatch.setattr(
        terminals,
        "create_terminal_instance",
        lambda *args, **kwargs: SimpleNamespace(instance=next(instances), cwd=tmp_path),
    )
    registry = terminals.TerminalRegistry()
    spec = TerminalEnvSpec(command="bash")
    owner = asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))
    try:
        await asyncio.wait_for(probing.wait(), 1)
        assert await asyncio.wait_for(registry.launch(*other_key, spec), 1) is second
        assert registry.get(*other_key) is second
        assert not owner.done()
        release.set()
        assert await owner is first
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


async def test_same_key_launch_coordinates_across_event_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import registry as terminals

    probing = asyncio.Event()
    release = asyncio.Event()
    waiter_started = threading.Event()
    prepared = []
    first_probe = True

    async def is_alive() -> bool:
        nonlocal first_probe
        if first_probe:
            first_probe = False
            probing.set()
            await release.wait()
        return True

    instance = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=is_alive,
    )

    def create(*args: object, **kwargs: object) -> SimpleNamespace:
        prepared.append("terminal")
        return SimpleNamespace(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(terminals, "create_terminal_instance", create)
    registry = terminals.TerminalRegistry()
    spec = TerminalEnvSpec(command="bash")

    async def launch_elsewhere() -> object:
        waiter_started.set()
        return await registry.launch("conv", "bash", "main", spec)

    owner = asyncio.create_task(registry.launch("conv", "bash", "main", spec))
    waiter = None
    try:
        await asyncio.wait_for(probing.wait(), 1)
        waiter = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(launch_elsewhere())))
        assert await asyncio.to_thread(waiter_started.wait, 1)
        await asyncio.sleep(0.03)
        assert prepared == ["terminal"], "second loop prepared a duplicate terminal"
        release.set()
        assert await owner is instance
        assert await asyncio.wait_for(waiter, 1) is instance
        assert registry.get("conv", "bash", "main") is instance
        assert registry._launch_locks == {}
    finally:
        release.set()
        await asyncio.gather(*(task for task in (owner, waiter) if task), return_exceptions=True)


@pytest.mark.parametrize("retire", ["close", "cleanup", "shutdown"])
async def test_launch_waiters_keep_admission_when_registered_owner_retires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retire: str
) -> None:
    from omnigent.terminals import registry as terminals

    probing = asyncio.Event()
    release = asyncio.Event()
    original = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
        close=AsyncMock(),
    )
    fresh = SimpleNamespace(
        lifecycle_trace=TerminalLifecycleTrace(),
        running=True,
        launch=AsyncMock(),
        is_alive=AsyncMock(return_value=True),
    )
    prepared = []

    def create(*args: object, **kwargs: object) -> SimpleNamespace:
        instance = original if not prepared else fresh
        prepared.append(instance)
        return SimpleNamespace(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(terminals, "create_terminal_instance", create)
    registry = terminals.TerminalRegistry()
    spec = TerminalEnvSpec(command="prime-agent")
    assert await registry.launch("conv", "prime-native", "main", spec) is original

    async def is_alive() -> bool:
        probing.set()
        await release.wait()
        return True

    original.is_alive = is_alive
    first = asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))
    queued = None
    newcomer = None
    try:
        await asyncio.wait_for(probing.wait(), 1)
        queued = asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))
        await asyncio.sleep(0.03)
        if retire == "close":
            assert await registry.close("conv", "prime-native", "main", expected=original)
        elif retire == "cleanup":
            await registry.cleanup_conversation("conv")
        else:
            await registry.shutdown()
        assert registry.get("conv", "prime-native", "main") is None
        newcomer = asyncio.create_task(registry.launch("conv", "prime-native", "main", spec))
        await asyncio.sleep(0.03)
        assert prepared == [original], (
            "retiring a terminal split admission between old waiters and a newcomer"
        )
        release.set()
        assert await asyncio.wait_for(first, 1) is fresh
        assert await asyncio.wait_for(queued, 1) is fresh
        assert await asyncio.wait_for(newcomer, 1) is fresh
        assert prepared == [original, fresh]
        assert registry.get("conv", "prime-native", "main") is fresh
        assert registry._launch_locks == {}
    finally:
        release.set()
        tasks = [task for task in (first, queued, newcomer) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

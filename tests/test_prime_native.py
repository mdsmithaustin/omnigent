from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from omnigent.harnesses.prime_native import bridge, process
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.harnesses.prime_native.process import build_prime_launch, stop_prime_runtime

_PRIME_DAEMON_SERVER = r"""
import contextlib
import json
import os
import socket
import sys
import time

socket_path, command_path, raw_behavior = sys.argv[1:]
behavior = json.loads(raw_behavior)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(socket_path)
os.chmod(socket_path, 0o600)
server.listen(1)
connection, _ = server.accept()
hello = {
    "type": "daemon_hello",
    "socketPath": socket_path,
    "protocol": {"name": "prime-agent.daemon", "version": 7},
    "schemaId": "protocol-7-schema-30-f908f493c9e1",
    "schemaRevision": 30,
    "appVersion": "0.9.6",
    "supervisorPid": os.getpid(),
    "supervisorSocketPath": socket_path,
    "clientId": "fixture-client",
    "serverCapabilities": [],
}
hello.update(behavior.get("hello", {}))
payload = (json.dumps(hello, separators=(",", ":")) + "\n").encode()
split = max(1, len(payload) // 2)
connection.sendall(payload[:split])
time.sleep(0.02)
connection.sendall(payload[split:])
received = b""
while b"\n" not in received:
    chunk = connection.recv(4096)
    if not chunk:
        break
    received += chunk
if received:
    command = json.loads(received.split(b"\n", 1)[0])
    with open(command_path, "w", encoding="utf-8") as output:
        json.dump(command, output)
    response_mode = behavior.get("response", "success")
    if response_mode != "eof":
        response = {
            "id": "wrong-id" if response_mode == "wrong_id" else command["id"],
            "type": "response",
            "command": "shutdown",
            "success": response_mode != "failed",
        }
        if response_mode == "failed":
            response["error"] = "busy"
        encoded = (
            b"{not-json}\n"
            if response_mode == "malformed"
            else (json.dumps(response, separators=(",", ":")) + "\n").encode()
        )
        split = max(1, len(encoded) // 2)
        connection.sendall(encoded[:split])
        time.sleep(0.02)
        connection.sendall(encoded[split:])
    if response_mode == "survive":
        time.sleep(30)
connection.close()
server.close()
with contextlib.suppress(FileNotFoundError):
    os.unlink(socket_path)
"""


@dataclass
class _PrimeDaemon:
    process: subprocess.Popen[str]
    socket_path: Path
    command_path: Path


@contextmanager
def _prime_daemon(
    paths: PrimeRuntimePaths,
    tmp_path: Path,
    *,
    hello: dict[str, object] | None = None,
    response: str = "success",
    socket_name: str = "daemon.sock",
) -> Iterator[_PrimeDaemon]:
    socket_dir = paths.temp_dir / f"prime-agent-{os.getuid()}"
    socket_dir.mkdir(mode=0o700, exist_ok=True)
    socket_path = socket_dir / socket_name
    command_path = tmp_path / f"command-{time.monotonic_ns()}.json"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _PRIME_DAEMON_SERVER,
            str(socket_path),
            str(command_path),
            json.dumps({"hello": hello or {}, "response": response}),
        ],
        env={**os.environ, **paths.env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(200):
            if socket_path.exists() or child.poll() is not None:
                break
            time.sleep(0.01)
        assert socket_path.exists(), child.stderr.read() if child.stderr is not None else ""
        yield _PrimeDaemon(child, socket_path, command_path)
    finally:
        if child.poll() is None:
            child.terminate()
        child.communicate(timeout=5)


@pytest.fixture(autouse=True)
def private_prime_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with tempfile.TemporaryDirectory(prefix="ogp-test-", dir="/tmp") as compact_root:
        monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
        monkeypatch.setattr(bridge, "_COMPACT_ROOT", Path(compact_root) / "compact")
        monkeypatch.setattr(process, "_ACTIVE_RUNTIMES", set())
        yield


def test_prime_modules_import_without_posix_user_id() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; del os.getuid; import omnigent.harnesses.prime_native.process",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_data_root_expands_user_home(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.harnesses.prime_native.bridge import bridge_roots; "
            "print(bridge_roots()[0])",
        ],
        env={**os.environ, "HOME": str(tmp_path), "OMNIGENT_DATA_DIR": "~/prime-data"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == str(tmp_path / "prime-data" / "prime-native")


def test_launch_expands_source_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "fixture"}')
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    build_prime_launch(
        paths,
        executable="/opt/prime-agent",
        extension=paths.root / "extension.js",
        config=paths.root / "config.json",
        environ={"PRIME_AGENT_CODING_AGENT_DIR": "~/source"},
    )
    assert json.loads((paths.agent_dir / "auth.json").read_text()) == {"token": "fixture"}


def test_launch_rejects_windows_before_creating_an_unstoppable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    monkeypatch.setattr(process, "IS_WINDOWS", True)

    with pytest.raises(RuntimeError, match="unavailable on Windows"):
        build_prime_launch(
            paths,
            executable="/opt/prime-agent",
            extension=paths.root / "extension.js",
            config=paths.root / "config.json",
        )

    assert not paths.root.exists()


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("linked_path", ["ancestor", "root", "agent", "tmp", "sessions", "inbox"])
def test_launch_rejects_directory_links_before_copying_credentials(
    tmp_path: Path, compact: bool, linked_path: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"secret": "fixture"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    paths = PrimeRuntimePaths(bridge.bridge_roots()[int(compact)] / "runtime")
    link = {
        "ancestor": paths.root.parent,
        "root": paths.root,
    }.get(linked_path, paths.root / linked_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        build_prime_launch(
            paths,
            executable="/opt/prime-agent",
            extension=paths.root / "omnigent_pi_native_extension.js",
            config=paths.root / "config.json",
            environ={"PRIME_AGENT_CODING_AGENT_DIR": str(source)},
        )

    assert list(outside.iterdir()) == []
    assert not (paths.agent_dir / "auth.json").exists()


@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize(
    "filename",
    [
        "agent/auth.json",
        "agent/settings.json",
        "agent/models.json",
        "executable",
        "terminal.json",
        "owner.pid",
        "config.json",
        "omnigent_pi_native_extension.js",
    ],
)
def test_launch_rejects_destination_file_links(
    tmp_path: Path, filename: str, dangling: bool
) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    paths.prepare()
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"secret": "fixture"}')
    outside = tmp_path / "outside.json"
    if not dangling:
        outside.write_text("unchanged")
    (paths.root / filename).symlink_to(outside)

    with pytest.raises(RuntimeError, match="symlink"):
        build_prime_launch(
            paths,
            executable="/opt/prime-agent",
            extension=paths.root / "omnigent_pi_native_extension.js",
            config=paths.root / "config.json",
            environ={"PRIME_AGENT_CODING_AGENT_DIR": str(source)},
        )

    assert outside.exists() is not dangling
    if not dangling:
        assert outside.read_text() == "unchanged"
    assert (paths.root / filename).is_symlink()


@pytest.mark.parametrize(
    "linked_path",
    ["ancestor", "root", "executable", "kernel-process-names.json", "terminal.json", "owner.pid"],
)
def test_stop_rejects_links_before_executing_recorded_command(
    tmp_path: Path, linked_path: str
) -> None:
    marker = tmp_path / "executed"
    executable = tmp_path / "marker-script"
    executable.write_text(
        "#!/bin/sh\ntouch "
        + shlex.quote(str(marker))
        + '\nprintf \'{"stopped": [], "failed": []}\\n\'\n'
    )
    executable.chmod(0o700)
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked_path in {"ancestor", "root"}:
        target = outside / "runtime" if linked_path == "ancestor" else outside
        target.mkdir(exist_ok=True)
        (target / "executable").write_text(str(executable))
        link = paths.root.parent if linked_path == "ancestor" else paths.root
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
    else:
        paths.prepare()
        (paths.root / "executable").write_text(str(executable))
        record = outside / linked_path
        record.write_text(str(executable) if linked_path == "executable" else "{}")
        link = paths.root / linked_path
        link.unlink(missing_ok=True)
        link.symlink_to(record)

    with pytest.raises(RuntimeError, match="symlink"):
        stop_prime_runtime(paths)
    assert not marker.exists()
    assert link.is_symlink()


def test_missing_runtime_stop_does_not_create_runtime_state(tmp_path: Path) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    stop_prime_runtime(paths)
    assert not paths.root.exists()
    stop_prime_runtime(paths)
    assert not paths.root.exists()


def test_stop_does_not_terminate_a_process_from_a_linked_terminal_record(tmp_path: Path) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    paths.prepare()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        outside = tmp_path / "terminal.json"
        outside.write_text(
            json.dumps(
                {"pid": child.pid, "created_at": process.psutil.Process(child.pid).create_time()}
            )
        )
        (paths.root / "terminal.json").symlink_to(outside)
        with pytest.raises(RuntimeError, match="symlink"):
            stop_prime_runtime(paths)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_arbitrary_runtime_root_is_rejected(tmp_path: Path) -> None:
    paths = PrimeRuntimePaths(tmp_path / "untrusted")
    with pytest.raises(RuntimeError, match="outside its bridge roots"):
        paths.prepare()
    with pytest.raises(RuntimeError, match="outside its bridge roots"):
        stop_prime_runtime(paths)
    assert not paths.root.exists()


def test_mismatched_trusted_parent_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge._trusted_parent_for_bridge_dir",
        lambda _: tmp_path / "unrelated",
    )
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    with pytest.raises(RuntimeError, match="not under trusted parent"):
        paths.prepare()
    assert not paths.root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_tool_relay_accepts_only_private_prime_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compact: bool
) -> None:
    from omnigent.harnesses.claude_native.bridge import start_tool_relay

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact")
    root = bridge.bridge_roots()[int(compact)]
    root.mkdir(mode=0o755)
    bridge_dir = root / "conversation"

    async def execute(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"text": "relay result"}

    relay = start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[{"name": "sys_os_read", "parameters": {"type": "object", "properties": {}}}],
        tool_executor=execute,
        loop=asyncio.get_running_loop(),
    )
    try:
        config = json.loads((bridge_dir / "tool_relay.json").read_text())
        assert config["tools"][0]["name"] == "sys_os_read"
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(bridge_dir.stat().st_mode) == 0o700
    finally:
        relay.close()

    linked = root / "linked"
    linked.symlink_to(bridge_dir, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        start_tool_relay(
            bridge_dir=linked,
            tools=[],
            tool_executor=execute,
            loop=asyncio.get_running_loop(),
        )


def test_launch_uses_private_config_temp_and_native_resume(tmp_path: Path) -> None:
    source = tmp_path / "user-agent"
    source.mkdir()
    (source / "auth.json").write_text('{"provider": "secret"}')
    (source / "settings.json").write_text('{"defaultModel": "fixture"}')
    (source / "models.json").write_text('{"providers": {}}')
    (source / "daemon").mkdir()
    (source / "daemon" / "owner").write_text("unrelated")
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    launch = build_prime_launch(
        paths,
        executable="/opt/prime-agent",
        extension=paths.root / "extension.js",
        config=paths.root / "config.json",
        extra_args=("--offline",),
        external_session_id="saved-prime-id",
        model="local/fixture",
        environ={"PRIME_AGENT_CODING_AGENT_DIR": str(source), "OMNIGENT_DATA_DIR": str(tmp_path)},
    )
    assert launch.argv == (
        "--extension",
        str(paths.root / "extension.js"),
        "--session-dir",
        str(paths.session_dir),
        "--offline",
        "--resume",
        "saved-prime-id",
        "--model",
        "local/fixture",
    )
    assert launch.env == {
        "PRIME_AGENT_CODING_AGENT_DIR": str(paths.agent_dir),
        "TMPDIR": str(paths.temp_dir),
        "OMNIGENT_EXTENSION_NATIVE_CONFIG": str(paths.root / "config.json"),
        "OMNIGENT_DATA_DIR": str(tmp_path),
    }
    assert json.loads((paths.agent_dir / "auth.json").read_text()) == {"provider": "secret"}
    assert not (paths.agent_dir / "daemon").exists()
    assert stat.S_IMODE(paths.agent_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((paths.agent_dir / "auth.json").stat().st_mode) == 0o600
    (paths.agent_dir / "auth.json").write_text("updated")
    assert json.loads((source / "auth.json").read_text()) == {"provider": "secret"}


@pytest.mark.parametrize(
    "arg",
    ["--session", "--resume=other", "--daemon-socket", "--extension", "--mode", "--no-extensions"],
)
def test_reserved_args_rejected_before_config_is_created(tmp_path: Path, arg: str) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    with pytest.raises(ValueError, match="owns its session"):
        build_prime_launch(
            paths,
            executable="prime-agent",
            extension=Path("e"),
            config=Path("c"),
            extra_args=(arg,),
        )
    assert not paths.root.exists()


def test_paths_isolate_sessions_and_data_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", Path("/tmp/ogp-path-test"))
    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path / "first")
    first = bridge.runtime_paths("conv_a")
    second = bridge.runtime_paths("conv_b")
    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path / "second")
    other_data_root = bridge.runtime_paths("conv_a")
    assert len({first.root, second.root, other_data_root.root}) == 3
    assert first.agent_dir != second.agent_dir
    assert first.temp_dir != second.temp_dir
    assert len(str(first.temp_dir / f"prime-agent-{os.getuid()}" / "daemon.sock").encode()) < 100


def _shutdown_paths(tmp_path: Path) -> PrimeRuntimePaths:
    paths = PrimeRuntimePaths(bridge.bridge_roots()[1] / "runtime")
    paths.prepare()
    (paths.root / "executable").write_text("/opt/prime-agent")
    (paths.session_dir / "saved.jsonl").write_text("saved transcript")
    return paths


def _write_lifecycle_records(paths: PrimeRuntimePaths) -> tuple[str, str]:
    process.owner_claim.write_owner_claim(paths.root)
    terminal = json.dumps({"pid": 99_999_999, "created_at": 1.0})
    (paths.root / "terminal.json").write_text(terminal)
    return (paths.root / "owner.pid").read_text(), terminal


def test_stale_launch_reservation_recovers_after_owner_process_exits(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from omnigent.harnesses.prime_native import bridge, process; "
            "bridge._DATA_ROOT=Path(sys.argv[1]); "
            "bridge._COMPACT_ROOT=Path(sys.argv[2]); "
            "process.reserve_prime_launch(bridge.PrimeRuntimePaths(Path(sys.argv[3])))",
            str(bridge._DATA_ROOT),
            str(bridge._COMPACT_ROOT),
            str(paths.root),
        ],
        check=True,
        timeout=10,
    )
    reservation = paths.root / "launch.pending.json"
    assert reservation.exists()

    assert process.stop_orphaned_runtimes() == 1
    assert not reservation.exists()
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"
    stop_prime_runtime(paths)


def test_reused_owner_pid_does_not_block_stale_launch_recovery(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    _write_lifecycle_records(paths)
    reservation = paths.root / "launch.pending.json"
    reservation.write_text(
        json.dumps(
            {"token": "old-owner", "pid": os.getpid(), "created_at": 1.0, "state": "pending"}
        )
    )

    stop_prime_runtime(paths)

    assert not reservation.exists()
    assert not (paths.root / "owner.pid").exists()
    assert not (paths.root / "terminal.json").exists()


def test_maintenance_retains_live_terminal_with_stale_launch_reservation(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    reservation = paths.root / "launch.pending.json"
    reservation.write_text(
        json.dumps(
            {"token": "dead-owner", "pid": os.getpid(), "created_at": 1.0, "state": "pending"}
        )
    )
    terminal = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, **paths.env},
    )
    try:
        (paths.root / "terminal.json").write_text(
            json.dumps(
                {
                    "pid": terminal.pid,
                    "created_at": process.psutil.Process(terminal.pid).create_time(),
                }
            )
        )
        assert process.stop_orphaned_runtimes() == 0
        assert terminal.poll() is None
        assert reservation.exists()
    finally:
        if terminal.poll() is None:
            terminal.terminate()
        terminal.wait(timeout=5)

    assert process.stop_orphaned_runtimes() == 1
    assert not reservation.exists()
    assert not (paths.root / "terminal.json").exists()


def test_failed_launch_cleanup_can_retry_after_private_supervisor_starts(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    reservation = process.reserve_prime_launch(paths)
    terminal = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, **paths.env},
    )
    try:
        (paths.root / "terminal.json").write_text(
            json.dumps(
                {
                    "pid": terminal.pid,
                    "created_at": process.psutil.Process(terminal.pid).create_time(),
                }
            )
        )
        with pytest.raises(RuntimeError, match="supervisor is unreachable"):
            process.abandon_prime_launch(paths, reservation, dispatched=True)
        assert terminal.poll() is None
        assert json.loads((paths.root / "launch.pending.json").read_text())["state"] == "abandoned"
        assert (paths.root / "terminal.json").exists()

        with _prime_daemon(paths, tmp_path):
            stop_prime_runtime(paths)
        assert terminal.wait(timeout=5) is not None
        assert not (paths.root / "launch.pending.json").exists()
        assert not (paths.root / "terminal.json").exists()
    finally:
        if terminal.poll() is None:
            terminal.terminate()
            terminal.wait(timeout=5)


def test_unconfirmed_dispatch_blocks_stop_until_owner_exits(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    reservation = process.reserve_prime_launch(paths)

    with pytest.raises(RuntimeError, match="no ownership record"):
        process.abandon_prime_launch(paths, reservation, dispatched=True)
    with pytest.raises(RuntimeError, match="launch is in progress"):
        stop_prime_runtime(paths)
    assert (paths.root / "launch.pending.json").exists()


@pytest.mark.parametrize("malformed", ["{", '{"token":"x","pid":1,"created_at":1.0,"state":[]}'])
def test_stop_retains_runtime_when_launch_reservation_is_invalid(
    tmp_path: Path, malformed: str
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    (paths.root / "launch.pending.json").write_text(malformed)

    with pytest.raises(RuntimeError, match="launch reservation is invalid"):
        stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


@pytest.mark.parametrize("compact", [False, True])
def test_maintenance_recovers_dead_launch_owner_after_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compact: bool
) -> None:
    from omnigent.native.native_bridge_common import reap_orphaned_native_bridge_dirs

    paths = PrimeRuntimePaths(bridge.bridge_roots()[int(compact)] / "runtime")
    source = (
        "import sys; from pathlib import Path; "
        "from omnigent.harnesses.prime_native import bridge,process; "
        "bridge._DATA_ROOT=Path(sys.argv[1]); "
        "bridge._COMPACT_ROOT=Path(sys.argv[2]); "
        "paths=bridge.PrimeRuntimePaths(Path(sys.argv[3])); "
        "process.build_prime_launch(paths,executable=sys.argv[4],"
        "extension=paths.root/'extension.js',config=paths.root/'config.json',environ={}); "
        "(paths.session_dir/'saved.jsonl').write_text('saved transcript'); "
        "(paths.agent_dir/'settings.json').write_text('saved configuration')"
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(bridge._DATA_ROOT),
            str(bridge._COMPACT_ROOT),
            str(paths.root),
            "/opt/prime-agent",
        ],
        check=True,
        timeout=10,
    )
    monkeypatch.setattr(
        "omnigent.harness_plugins.native_agents", lambda: (SimpleNamespace(key="prime-native"),)
    )
    assert len(process._ACTIVE_RUNTIMES) == 0

    assert reap_orphaned_native_bridge_dirs() == 1
    assert not (paths.root / "owner.pid").exists()
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"
    assert (paths.agent_dir / "settings.json").read_text() == "saved configuration"
    assert reap_orphaned_native_bridge_dirs() == 0


def _write_dead_owner(paths: PrimeRuntimePaths) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from omnigent.native.owner_claim import write_owner_claim; "
            "write_owner_claim(Path(sys.argv[1]))",
            str(paths.root),
        ],
        check=True,
        timeout=10,
    )


def _maintenance_paths(tmp_path: Path) -> PrimeRuntimePaths:
    return _shutdown_paths(tmp_path)


def test_maintenance_protects_live_launch_owner(tmp_path: Path) -> None:
    paths = _maintenance_paths(tmp_path)
    build_prime_launch(
        paths,
        executable=(paths.root / "executable").read_text(),
        extension=Path("e"),
        config=Path("c"),
        environ={},
    )
    assert bridge.prune_orphaned_bridge_dirs() == 0
    assert (paths.root / "owner.pid").exists()
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


@pytest.mark.parametrize("same_identity", [False, True])
def test_maintenance_checks_terminal_identity_after_owner_exits(
    tmp_path: Path, same_identity: bool
) -> None:
    paths = _maintenance_paths(tmp_path)
    _write_dead_owner(paths)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (paths.root / "terminal.json").write_text(
            json.dumps(
                {
                    "pid": child.pid,
                    "created_at": process.psutil.Process(child.pid).create_time()
                    if same_identity
                    else 0,
                }
            )
        )
        assert bridge.prune_orphaned_bridge_dirs() == (0 if same_identity else 1)
        assert (paths.root / "owner.pid").exists() is same_identity
        assert child.poll() is None
        assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("claim", [None, "invalid", "99999999\n", "99999999\npid_ns=foreign\n"])
def test_maintenance_retains_unqualified_runtime(tmp_path: Path, claim: str | None) -> None:
    paths = _maintenance_paths(tmp_path)
    if claim is not None:
        (paths.root / "owner.pid").write_text(claim)
    assert bridge.prune_orphaned_bridge_dirs() == 0
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


@pytest.mark.parametrize("filename", ["owner.pid", "terminal.json", "executable"])
@pytest.mark.parametrize("hard_link", [False, True])
def test_maintenance_rejects_linked_ownership_files(
    tmp_path: Path, filename: str, hard_link: bool
) -> None:
    paths = _maintenance_paths(tmp_path)
    _write_dead_owner(paths)
    destination = paths.root / filename
    outside = tmp_path / "outside"
    outside.write_text(destination.read_text() if destination.exists() else "{}")
    destination.unlink(missing_ok=True)
    if hard_link:
        os.link(outside, destination)
    else:
        destination.symlink_to(outside)
    assert bridge.prune_orphaned_bridge_dirs() == 0
    assert outside.exists()
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


@pytest.mark.parametrize("record", ["not JSON", "{}", "null"])
def test_maintenance_retains_invalid_terminal_record(tmp_path: Path, record: str) -> None:
    paths = _maintenance_paths(tmp_path)
    _write_dead_owner(paths)
    (paths.root / "terminal.json").write_text(record)
    assert bridge.prune_orphaned_bridge_dirs() == 0
    assert (paths.root / "owner.pid").exists()


def test_maintenance_retains_owner_on_shutdown_failure_for_retry(tmp_path: Path) -> None:
    paths = _maintenance_paths(tmp_path)
    _write_dead_owner(paths)
    owner = (paths.root / "owner.pid").read_text()
    with _prime_daemon(paths, tmp_path, response="failed"):
        assert bridge.prune_orphaned_bridge_dirs() == 0
    assert (paths.root / "owner.pid").read_text() == owner
    with _prime_daemon(paths, tmp_path) as daemon:
        assert bridge.prune_orphaned_bridge_dirs() == 1
        daemon.process.wait(timeout=5)
    assert daemon.command_path.exists()
    assert not (paths.root / "owner.pid").exists()
    assert bridge.prune_orphaned_bridge_dirs() == 0


def _wrapper_command(paths: PrimeRuntimePaths, *child_command: str) -> list[str]:
    source = (
        "import sys; from pathlib import Path; "
        "from omnigent.harnesses.prime_native import bridge,process; "
        "bridge._DATA_ROOT=Path(sys.argv[1]); bridge._COMPACT_ROOT=Path(sys.argv[2]); "
        "sys.argv=sys.argv[:1]+sys.argv[3:]; raise SystemExit(process.main())"
    )
    return [
        sys.executable,
        "-c",
        source,
        str(bridge._DATA_ROOT),
        str(bridge._COMPACT_ROOT),
        str(paths.root),
        *child_command,
    ]


def test_terminal_wrapper_owns_runtime_until_terminal_exits(tmp_path: Path) -> None:
    from omnigent.native.owner_claim import read_owner_claim

    paths = _maintenance_paths(tmp_path)
    launch = build_prime_launch(
        paths,
        executable=(paths.root / "executable").read_text(),
        extension=Path("e"),
        config=Path("c"),
        environ={"OMNIGENT_DATA_DIR": str(tmp_path)},
    )
    with _prime_daemon(paths, tmp_path) as daemon:
        wrapper = subprocess.Popen(
            _wrapper_command(
                paths,
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ),
            env={**os.environ, **launch.env},
        )
        try:
            for _ in range(100):
                if (paths.root / "terminal.json").is_file():
                    break
                time.sleep(0.02)
            claim = read_owner_claim(paths.root)
            assert claim is not None and claim.pid == wrapper.pid
            assert bridge.prune_orphaned_bridge_dirs() == 0
            wrapper.terminate()
            wrapper.wait(timeout=10)
            daemon.process.wait(timeout=5)
            assert daemon.command_path.exists()
            assert not (paths.root / "owner.pid").exists()
            assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"
        finally:
            if wrapper.poll() is None:
                wrapper.terminate()
                wrapper.wait(timeout=10)


def test_stale_wrapper_finalization_leaves_replacement_records_and_terminal(
    tmp_path: Path,
) -> None:
    from omnigent.harnesses.pi_native.bridge import _atomic_json
    from omnigent.native.owner_claim import read_owner_claim

    paths = _maintenance_paths(tmp_path)
    launch = build_prime_launch(
        paths,
        executable=(paths.root / "executable").read_text(),
        extension=Path("e"),
        config=Path("c"),
        environ={},
    )
    wrapper = subprocess.Popen(
        _wrapper_command(
            paths,
            sys.executable,
            "-c",
            "import time; time.sleep(0.3)",
        ),
        env={**os.environ, **launch.env},
    )
    replacement = None
    try:
        for _ in range(100):
            if (paths.root / "terminal.json").is_file():
                break
            time.sleep(0.01)
        with process.native_bridge_common.bridge_dir_preparation_lock(paths.root):
            time.sleep(0.5)
            replacement = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            replacement_record: dict[str, object] = {
                "pid": replacement.pid,
                "created_at": process.psutil.Process(replacement.pid).create_time(),
            }
            process.owner_claim.write_owner_claim(paths.root)
            _atomic_json(paths.root / "terminal.json", replacement_record)

        wrapper.wait(timeout=10)
        assert replacement.poll() is None
        assert json.loads((paths.root / "terminal.json").read_text()) == replacement_record
        claim = read_owner_claim(paths.root)
        assert claim is not None and claim.pid == os.getpid()
        assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=5)
        if replacement is not None and replacement.poll() is None:
            replacement.terminate()
            replacement.wait(timeout=5)


def test_public_stop_and_wrapper_finalization_do_not_deadlock(tmp_path: Path) -> None:
    paths = _maintenance_paths(tmp_path)
    launch = build_prime_launch(
        paths,
        executable=(paths.root / "executable").read_text(),
        extension=Path("e"),
        config=Path("c"),
        environ={},
    )
    with _prime_daemon(paths, tmp_path) as daemon:
        wrapper = subprocess.Popen(
            _wrapper_command(
                paths,
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ),
            env={**os.environ, **launch.env},
        )
        try:
            for _ in range(100):
                if (paths.root / "terminal.json").is_file():
                    break
                time.sleep(0.01)
            stop_prime_runtime(paths)
            wrapper.wait(timeout=10)
            daemon.process.wait(timeout=5)
            assert daemon.command_path.exists()
            assert not (paths.root / "owner.pid").exists()
            assert not (paths.root / "terminal.json").exists()
        finally:
            if wrapper.poll() is None:
                wrapper.terminate()
                wrapper.wait(timeout=5)


def test_maintenance_skips_runtime_during_preparation(tmp_path: Path) -> None:
    paths = _maintenance_paths(tmp_path)
    _write_dead_owner(paths)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from omnigent.native.native_bridge_common import bridge_dir_preparation_lock; "
            "lock=bridge_dir_preparation_lock(Path(sys.argv[1])); lock.__enter__(); "
            "print('LOCKED',flush=True); input(); lock.__exit__(None,None,None)",
            str(paths.root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "LOCKED\n"
        assert bridge.prune_orphaned_bridge_dirs() == 0
        assert (paths.root / "owner.pid").exists()
        holder.communicate("\n", timeout=10)
        assert holder.returncode == 0
        assert bridge.prune_orphaned_bridge_dirs() == 1
        assert not (paths.root / "owner.pid").exists()
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_stop_sends_one_qualified_socket_shutdown_and_preserves_history(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    with _prime_daemon(paths, tmp_path) as daemon:
        stop_prime_runtime(paths)
        daemon.process.wait(timeout=5)

    command = json.loads(daemon.command_path.read_text())
    assert command == {
        "type": "command",
        "id": command["id"],
        "protocol": {"name": "prime-agent.daemon", "version": 7},
        "clientId": "fixture-client",
        "command": {"type": "shutdown", "force": True},
    }
    assert isinstance(command["id"], str) and command["id"]
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


def test_stop_accepts_eof_after_command_only_when_runtime_disappears(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    with _prime_daemon(paths, tmp_path, response="eof") as daemon:
        stop_prime_runtime(paths)
        daemon.process.wait(timeout=5)

    assert json.loads(daemon.command_path.read_text())["command"] == {
        "type": "shutdown",
        "force": True,
    }
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


@pytest.mark.parametrize(
    "hello",
    [
        {"protocol": {"name": "prime-agent.daemon", "version": 6}},
        {"schemaId": "protocol-7-schema-29-wrong", "schemaRevision": 29},
        {"appVersion": "0.9.7"},
        {"socketPath": "/tmp/not-this-runtime.sock"},
        {"supervisorPid": os.getpid()},
    ],
)
def test_stop_rejects_unqualified_hello_and_retains_lifecycle_records(
    tmp_path: Path, hello: dict[str, object]
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    with _prime_daemon(paths, tmp_path, hello=hello) as daemon:
        with pytest.raises(RuntimeError, match="runtime retained"):
            stop_prime_runtime(paths)

    assert not daemon.command_path.exists()
    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


@pytest.mark.parametrize("response", ["malformed", "failed", "wrong_id"])
def test_stop_rejects_invalid_response_and_retains_lifecycle_records(
    tmp_path: Path, response: str
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    with _prime_daemon(paths, tmp_path, response=response):
        with pytest.raises(RuntimeError, match="runtime retained"):
            stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


def test_stop_rejects_success_response_while_private_supervisor_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    monkeypatch.setattr(process, "_SHUTDOWN_SETTLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(process, "_SHUTDOWN_POLL_INTERVAL_S", 0.01)
    with _prime_daemon(paths, tmp_path, response="survive"):
        with pytest.raises(RuntimeError, match="left a scoped process or socket"):
            stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


def test_stop_without_live_private_runtime_is_idempotent_and_preserves_history(
    tmp_path: Path,
) -> None:
    paths = _shutdown_paths(tmp_path)
    _write_lifecycle_records(paths)
    (paths.agent_dir / "settings.json").write_text("saved configuration")

    stop_prime_runtime(paths)
    stop_prime_runtime(paths)

    assert not (paths.root / "owner.pid").exists()
    assert not (paths.root / "terminal.json").exists()
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"
    assert (paths.agent_dir / "settings.json").read_text() == "saved configuration"


def test_stop_rejects_missing_endpoint_with_a_surviving_private_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = process._ProcessIdentity(
            survivor.pid, process.psutil.Process(survivor.pid).create_time()
        )
        monkeypatch.setattr(process, "_owned_process_identities", lambda _: {identity})
        monkeypatch.setattr(process, "_SHUTDOWN_SETTLE_TIMEOUT_S", 0.01)
        monkeypatch.setattr(process, "_SHUTDOWN_POLL_INTERVAL_S", 0.001)
        with pytest.raises(RuntimeError, match="left a scoped process or socket"):
            stop_prime_runtime(paths)
    finally:
        survivor.terminate()
        survivor.wait(timeout=5)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


@pytest.mark.parametrize("recorded_launch", [False, True])
def test_stop_retains_records_for_an_orphaned_private_python_process(
    tmp_path: Path, recorded_launch: bool
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    if recorded_launch:
        build_prime_launch(
            paths,
            executable="/opt/prime-agent",
            extension=paths.root / "extension.js",
            config=paths.root / "config.json",
            environ={},
        )
    package = tmp_path / "rlm"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "repl.py").write_text("import time; time.sleep(60)")
    kernel = subprocess.Popen(
        [sys.executable, "-m", "rlm.repl"],
        env={
            **os.environ,
            "PRIME_AGENT_CODING_AGENT_DIR": str(paths.agent_dir),
            "PYTHONPATH": str(tmp_path),
        },
    )
    try:
        identity = process._ProcessIdentity(
            kernel.pid, process.psutil.Process(kernel.pid).create_time()
        )
        assert identity in process._owned_process_identities(paths)
        with pytest.raises(RuntimeError, match="left a scoped process or socket"):
            stop_prime_runtime(paths)
        assert kernel.poll() is None
        assert (paths.root / "owner.pid").read_text() == owner
        assert (paths.root / "terminal.json").read_text() == terminal
    finally:
        kernel.terminate()
        kernel.wait(timeout=5)


@pytest.mark.parametrize("via_symlink", [False, True])
def test_stop_retains_records_for_an_orphaned_renamed_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_symlink: bool
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    executable = tmp_path / "prime-kernel-probe"
    shutil.copy2(sys.executable, executable)
    configured = executable
    if via_symlink:
        configured = tmp_path / "kernel-link"
        configured.symlink_to(executable)
    package = tmp_path / "rlm"
    package.mkdir()
    (package / "__init__.py").write_text("")
    ready = tmp_path / "kernel-ready"
    (package / "repl.py").write_text(
        "from pathlib import Path\nimport time\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(60)\n"
    )
    launch = build_prime_launch(
        paths,
        executable="/opt/prime-agent",
        extension=paths.root / "extension.js",
        config=paths.root / "config.json",
        environ={"PRIME_AGENT_KERNEL_PYTHON": str(configured)},
    )
    kernel = subprocess.Popen(
        [str(configured), "-m", "rlm.repl"],
        env={
            **os.environ,
            "PRIME_AGENT_KERNEL_PYTHON": str(configured),
            **launch.env,
            "PYTHONHOME": sys.base_prefix,
            "PYTHONPATH": str(tmp_path),
        },
    )
    try:
        for _ in range(200):
            if ready.exists() or kernel.poll() is not None:
                break
            time.sleep(0.01)
        assert ready.read_text() == "ready"
        assert process.psutil.Process(kernel.pid).name().startswith("prime-kernel-pr")
        assert kernel.poll() is None
        monkeypatch.setattr(process, "_SHUTDOWN_SETTLE_TIMEOUT_S", 0.05)
        with pytest.raises(RuntimeError, match="left a scoped process or socket"):
            stop_prime_runtime(paths)
        assert kernel.poll() is None
        assert (paths.root / "owner.pid").read_text() == owner
        assert (paths.root / "terminal.json").read_text() == terminal
    finally:
        kernel.terminate()
        kernel.wait(timeout=5)


@pytest.mark.parametrize("via_symlink", [False, True])
def test_stop_retains_records_for_an_orphaned_renamed_prime_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_symlink: bool
) -> None:
    paths = _shutdown_paths(tmp_path)
    executable = tmp_path / "renamed-prime-agent-probe"
    shutil.copy2(sys.executable, executable)
    configured = executable
    if via_symlink:
        configured = tmp_path / "prime-link"
        configured.symlink_to(executable)
    launch = build_prime_launch(
        paths,
        executable=str(configured),
        extension=paths.root / "extension.js",
        config=paths.root / "config.json",
        environ={"PRIME_AGENT_CODING_AGENT_DIR": str(tmp_path / "empty-source")},
    )
    owner, terminal = _write_lifecycle_records(paths)
    ready = tmp_path / "prime-ready"
    child = subprocess.Popen(
        [
            str(configured),
            "-c",
            "from pathlib import Path; import time; "
            f"Path({str(ready)!r}).write_text('ready'); time.sleep(60)",
        ],
        env={**os.environ, **launch.env, "PYTHONHOME": sys.base_prefix},
    )
    try:
        for _ in range(200):
            if ready.exists() or child.poll() is not None:
                break
            time.sleep(0.01)
        assert ready.read_text() == "ready"
        reported_name = process.psutil.Process(child.pid).name()
        assert reported_name.casefold().startswith("renamed-prime-a")
        assert child.poll() is None
        monkeypatch.setattr(process, "_SHUTDOWN_SETTLE_TIMEOUT_S", 0.05)
        with pytest.raises(RuntimeError, match="left a scoped process or socket"):
            stop_prime_runtime(paths)
        assert child.poll() is None
        assert (paths.root / "owner.pid").read_text() == owner
        assert (paths.root / "terminal.json").read_text() == terminal
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("record", ["", "/", "\0"])
def test_stop_retains_records_for_invalid_prime_executable_record(
    tmp_path: Path, record: str
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    (paths.root / "executable").write_text(record)

    with pytest.raises(RuntimeError, match="executable record is invalid"):
        stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


@pytest.mark.parametrize("record", ["{", "{}", "[42]", '["Unnormalized"]'])
def test_stop_retains_records_for_invalid_kernel_names_record(tmp_path: Path, record: str) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    (paths.root / bridge.KERNEL_PROCESS_NAMES_FILE).write_text(record)

    with pytest.raises(RuntimeError, match="kernel process names record is invalid"):
        stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


def test_orphan_maintenance_retains_invalid_kernel_names_record(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from omnigent.native import owner_claim; "
            "owner_claim.write_owner_claim(Path(sys.argv[1]))",
            str(paths.root),
        ],
        check=True,
        timeout=10,
    )
    owner = (paths.root / "owner.pid").read_text()
    terminal = json.dumps({"pid": 99_999_999, "created_at": 1.0})
    (paths.root / "terminal.json").write_text(terminal)
    (paths.root / bridge.KERNEL_PROCESS_NAMES_FILE).write_text("{}")

    assert process.stop_orphaned_runtimes() == 0
    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


@pytest.mark.parametrize("alive", [False, True])
@pytest.mark.parametrize(
    ("candidate_name", "kernel_executable", "prime_executable"),
    [
        ("python3.12", None, None),
        ("prime-kernel-pro", "prime-kernel-probe", None),
        ("renamed-prime-ag", None, "renamed-prime-agent-probe"),
    ],
)
def test_unreadable_candidate_is_checked_for_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alive: bool,
    candidate_name: str,
    kernel_executable: str | None,
    prime_executable: str | None,
) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    paths.prepare()
    if kernel_executable is not None:
        build_prime_launch(
            paths,
            executable="/opt/prime-agent",
            extension=paths.root / "extension.js",
            config=paths.root / "config.json",
            environ={"PRIME_AGENT_KERNEL_PYTHON": str(tmp_path / kernel_executable)},
        )
    if prime_executable is not None:
        (paths.root / "executable").write_text(str(tmp_path / prime_executable))

    class Candidate:
        pid = 999_999
        info = {"name": candidate_name}

        def uids(self) -> SimpleNamespace:
            return SimpleNamespace(real=os.getuid())

        def environ(self) -> dict[str, str]:
            raise process.psutil.AccessDenied(self.pid)

        def cmdline(self) -> list[str]:
            if prime_executable is not None:
                return [str(tmp_path / prime_executable), "interactive"]
            return ["/usr/bin/python3", "-m", "rlm.repl"]

        def is_running(self) -> bool:
            return alive

        def status(self) -> str:
            return process.psutil.STATUS_RUNNING

    monkeypatch.setattr(process.psutil, "process_iter", lambda *_args: iter([Candidate()]))
    if alive:
        with pytest.raises(RuntimeError, match="ownership could not be observed"):
            process._owned_process_identities(paths)
    else:
        assert process._owned_process_identities(paths) == set()


@pytest.mark.parametrize(
    ("candidate_name", "kernel_executable", "prime_executable"),
    [
        ("python3.12", None, None),
        ("prime-kernel-pro", "prime-kernel-probe", None),
        ("renamed-prime-", None, "renamed-prime-agent-probe"),
    ],
)
def test_unrelated_candidate_does_not_require_environment_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_name: str,
    kernel_executable: str | None,
    prime_executable: str | None,
) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    paths.prepare()
    if kernel_executable is not None:
        build_prime_launch(
            paths,
            executable="/opt/prime-agent",
            extension=paths.root / "extension.js",
            config=paths.root / "config.json",
            environ={"PRIME_AGENT_KERNEL_PYTHON": str(tmp_path / kernel_executable)},
        )
    if prime_executable is not None:
        (paths.root / "executable").write_text(str(tmp_path / prime_executable))

    class Candidate:
        pid = 999_999
        info = {"name": candidate_name}

        def uids(self) -> SimpleNamespace:
            return SimpleNamespace(real=os.getuid())

        def cmdline(self) -> list[str]:
            return ["/usr/bin/python3", "-m", "unrelated.module"]

        def environ(self) -> dict[str, str]:
            raise process.psutil.AccessDenied(self.pid)

        def is_running(self) -> bool:
            return True

        def status(self) -> str:
            return process.psutil.STATUS_RUNNING

    monkeypatch.setattr(process.psutil, "process_iter", lambda *_args: iter([Candidate()]))
    assert process._owned_process_identities(paths) == set()


def test_unreadable_python_that_exits_during_inspection_is_not_a_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    paths.prepare()

    class Candidate:
        pid = 999_999
        info = {"name": "python3.12"}

        def __init__(self) -> None:
            self.probes = 0

        def uids(self) -> SimpleNamespace:
            return SimpleNamespace(real=os.getuid())

        def cmdline(self) -> list[str]:
            raise process.psutil.AccessDenied(self.pid)

        def is_running(self) -> bool:
            self.probes += 1
            return self.probes == 1

        def status(self) -> str:
            return process.psutil.STATUS_RUNNING

    monkeypatch.setattr(process.psutil, "process_iter", lambda *_args: iter([Candidate()]))
    assert process._owned_process_identities(paths) == set()


def test_stop_fails_closed_when_private_process_observation_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)

    def unreadable(_: PrimeRuntimePaths) -> set[object]:
        raise RuntimeError("Prime process ownership could not be observed; runtime retained.")

    monkeypatch.setattr(process, "_owned_process_identities", unreadable)
    with pytest.raises(RuntimeError, match="could not be observed"):
        stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


def test_stop_fails_closed_without_a_private_windows_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    monkeypatch.setattr(process, "IS_WINDOWS", True)

    with pytest.raises(RuntimeError, match="unavailable on Windows"):
        stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


@pytest.mark.parametrize("failure", ["windows", "bad_hello"])
def test_unqualified_shutdown_keeps_live_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    paths = _shutdown_paths(tmp_path)
    _write_lifecycle_records(paths)
    terminal = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = {
            "pid": terminal.pid,
            "created_at": process.psutil.Process(terminal.pid).create_time(),
        }
        (paths.root / "terminal.json").write_text(json.dumps(record))
        if failure == "windows":
            monkeypatch.setattr(process, "IS_WINDOWS", True)
            with pytest.raises(RuntimeError, match="unavailable on Windows"):
                stop_prime_runtime(paths)
        else:
            with _prime_daemon(paths, tmp_path, hello={"appVersion": "0.9.7"}):
                with pytest.raises(RuntimeError, match="runtime retained"):
                    stop_prime_runtime(paths)
        assert terminal.poll() is None
        assert json.loads((paths.root / "terminal.json").read_text()) == record
        assert (paths.root / "owner.pid").exists()
    finally:
        if terminal.poll() is None:
            terminal.terminate()
        terminal.wait(timeout=5)


def test_stop_never_sends_shutdown_to_a_worker_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    monkeypatch.setattr(process, "_SHUTDOWN_SETTLE_TIMEOUT_S", 0.0)
    with _prime_daemon(paths, tmp_path, socket_name="worker-private.sock") as worker:
        with pytest.raises(RuntimeError, match="left a scoped process or socket"):
            stop_prime_runtime(paths)

    assert not worker.command_path.exists()
    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


@pytest.mark.parametrize("target", ["directory", "endpoint"])
def test_stop_rejects_untrusted_supervisor_socket_filesystem_entries(
    tmp_path: Path, target: str
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    socket_dir = paths.temp_dir / f"prime-agent-{os.getuid()}"
    outside = tmp_path / "outside"
    if target == "directory":
        outside.mkdir()
        socket_dir.symlink_to(outside, target_is_directory=True)
    else:
        socket_dir.mkdir(mode=0o700)
        (socket_dir / "daemon.sock").write_text("not a socket")

    with pytest.raises(RuntimeError, match="runtime retained"):
        stop_prime_runtime(paths)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


def test_each_shutdown_uses_a_fresh_command_id(tmp_path: Path) -> None:
    paths = _shutdown_paths(tmp_path)
    with _prime_daemon(paths, tmp_path) as first:
        stop_prime_runtime(paths)
        first.process.wait(timeout=5)
    with _prime_daemon(paths, tmp_path) as second:
        stop_prime_runtime(paths)
        second.process.wait(timeout=5)

    first_command = json.loads(first.command_path.read_text())
    second_command = json.loads(second.command_path.read_text())
    assert first_command["id"] != second_command["id"]


def test_stop_observes_a_private_process_that_appears_during_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    owner, terminal = _write_lifecycle_records(paths)
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = process._ProcessIdentity(
            survivor.pid, process.psutil.Process(survivor.pid).create_time()
        )
        observations = iter([set(), {identity}])
        monkeypatch.setattr(process, "_owned_process_identities", lambda _: next(observations))
        monkeypatch.setattr(process, "_SHUTDOWN_SETTLE_TIMEOUT_S", 0.0)
        with pytest.raises(RuntimeError, match="left a scoped process or socket"):
            stop_prime_runtime(paths)
    finally:
        survivor.terminate()
        survivor.wait(timeout=5)

    assert (paths.root / "owner.pid").read_text() == owner
    assert (paths.root / "terminal.json").read_text() == terminal


def test_unqualified_version_fails_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "prime-agent"
    binary.write_text("#!/bin/sh\nprintf '0.9.7\\n'\n")
    binary.chmod(0o700)
    monkeypatch.setenv("OMNIGENT_PRIME_PATH", str(binary))
    with pytest.raises(click.ClickException, match=r"requires prime-agent 0\.9\.6"):
        process.resolve_prime_executable()


def test_public_command_has_no_prime_alias() -> None:
    from omnigent.cli import cli
    from omnigent.harness_plugins import harness_modules, native_provider_for_key
    from omnigent.native.native_coding_agents import native_coding_agent_for_harness

    assert "prime-native" in cli.commands
    assert "prime" not in cli.commands
    agent = native_coding_agent_for_harness("prime-native")
    assert agent is not None and agent.display_name == "Prime Native"
    assert agent.agent_name == "prime-native-ui"
    assert harness_modules()["prime-native"] == "omnigent.inner.prime_native_harness"
    provider = native_provider_for_key("prime-native")
    assert (
        provider is not None
        and provider.stop_handler == "omnigent.harnesses.prime_native.process:stop_session"
    )


@pytest.mark.parametrize("server_url", [None, "https://remote.example.test"])
def test_prime_selectors_survive_host_and_runner_env_filtering(
    monkeypatch: pytest.MonkeyPatch, server_url: str | None
) -> None:
    from omnigent.cli import _build_host_daemon_env
    from omnigent.host.connect import _build_runner_env

    env = {
        "OMNIGENT_PRIME_PATH": "/custom/prime-agent",
        "PRIME_AGENT_CODING_AGENT_DIR": "/custom/prime-config",
        "PRIME_AGENT_KERNEL_PYTHON": "/custom/kernel/bin/python",
        "UNRELATED_SECRET": "private",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    host = _build_host_daemon_env(server_url=server_url)
    assert host["OMNIGENT_PRIME_PATH"] == "/custom/prime-agent"
    assert host["PRIME_AGENT_CODING_AGENT_DIR"] == "/custom/prime-config"
    assert host["PRIME_AGENT_KERNEL_PYTHON"] == "/custom/kernel/bin/python"
    assert "UNRELATED_SECRET" not in host
    runner = _build_runner_env(
        host,
        server_url=server_url or "http://localhost:6767",
        runner_id="r",
        binding_token="binding",
        workspace="/tmp",
        parent_pid=1,
    )
    assert runner["OMNIGENT_PRIME_PATH"] == "/custom/prime-agent"
    assert runner["PRIME_AGENT_CODING_AGENT_DIR"] == "/custom/prime-config"
    assert runner["PRIME_AGENT_KERNEL_PYTHON"] == "/custom/kernel/bin/python"
    assert "UNRELATED_SECRET" not in runner


@pytest.mark.parametrize("effort,thinking", [("high", "high"), ("none", "off"), ("ultra", "max")])
def test_launch_transports_effort_and_composed_instructions(
    tmp_path: Path, effort: str, thinking: str
) -> None:
    launch = build_prime_launch(
        PrimeRuntimePaths(tmp_path / "prime-native" / "runtime"),
        executable="prime-agent",
        extension=Path("ext"),
        config=Path("config"),
        reasoning_effort=effort,
        instructions="Author instructions\n\nFramework instructions",
        environ={},
    )
    assert launch.argv[-4:] == (
        "--thinking",
        thinking,
        "--append-system-prompt",
        "Author instructions\n\nFramework instructions",
    )

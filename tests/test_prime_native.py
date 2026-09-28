from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import click
import pytest

from omnigent.harnesses.prime_native import bridge, process
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.harnesses.prime_native.process import build_prime_launch, stop_prime_runtime


@pytest.fixture(autouse=True)
def private_prime_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact")
    monkeypatch.setattr(process, "_ACTIVE_RUNTIMES", set())


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


@pytest.mark.parametrize("linked_path", ["ancestor", "root", "executable", "terminal.json"])
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


def test_missing_runtime_stop_does_not_create_state(tmp_path: Path) -> None:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    stop_prime_runtime(paths)
    assert not paths.root.parent.exists()
    paths.root.parent.mkdir()
    stop_prime_runtime(paths)
    assert list(paths.root.parent.iterdir()) == []


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
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    paths.prepare()
    (paths.root / "executable").write_text("/opt/prime-agent")
    (paths.session_dir / "saved.jsonl").write_text("saved transcript")
    return paths


def test_stop_accepts_valid_empty_exit_one_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    run = Mock(
        return_value=subprocess.CompletedProcess([], 1, '{"stopped": [], "failed": []}', "")
    )
    monkeypatch.setattr(process.subprocess, "run", run)
    monkeypatch.setattr(process, "_owned_processes", lambda _: [])
    stop_prime_runtime(paths)
    assert run.call_args.args[0] == ["/opt/prime-agent", "shutdown", "--force", "--json"]
    assert run.call_args.kwargs["env"]["TMPDIR"] == str(paths.temp_dir)
    assert run.call_args.kwargs["env"]["PRIME_AGENT_CODING_AGENT_DIR"] == str(paths.agent_dir)
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


@pytest.mark.parametrize(
    "output",
    ["Unknown option --daemon-socket", '{"failed": ["still running"], "stopped": []}', "{}"],
)
def test_stop_rejects_false_success_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    paths = _shutdown_paths(tmp_path)
    monkeypatch.setattr(
        process.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, output, "")
    )
    monkeypatch.setattr(process, "_owned_processes", lambda _: [])
    with pytest.raises(RuntimeError):
        stop_prime_runtime(paths)
    assert paths.session_dir.is_dir()


def test_stop_rejects_live_worker_after_success_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _shutdown_paths(tmp_path)
    monkeypatch.setattr(
        process.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, '{"stopped": [], "failed": []}', ""),
    )
    worker = Mock()
    worker.is_running.return_value = True
    worker.status.return_value = "running"
    monkeypatch.setattr(process, "_owned_processes", lambda _: [worker])
    ticks = iter([0, 10])
    monkeypatch.setattr(process.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="scoped process"):
        stop_prime_runtime(paths)
    assert paths.root.exists()


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


def test_prime_selectors_survive_host_and_runner_env_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.cli import _build_host_daemon_env
    from omnigent.host.connect import _build_runner_env

    env = {
        "OMNIGENT_PRIME_PATH": "/custom/prime-agent",
        "PRIME_AGENT_CODING_AGENT_DIR": "/custom/prime-config",
        "UNRELATED_SECRET": "private",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    host = _build_host_daemon_env(server_url="http://localhost:6767")
    assert host["OMNIGENT_PRIME_PATH"] == "/custom/prime-agent"
    assert host["PRIME_AGENT_CODING_AGENT_DIR"] == "/custom/prime-config"
    assert "UNRELATED_SECRET" not in host
    runner = _build_runner_env(
        env,
        server_url="http://localhost:6767",
        runner_id="r",
        binding_token="binding",
        workspace="/tmp",
        parent_pid=1,
    )
    assert runner["OMNIGENT_PRIME_PATH"] == "/custom/prime-agent"
    assert runner["PRIME_AGENT_CODING_AGENT_DIR"] == "/custom/prime-config"
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

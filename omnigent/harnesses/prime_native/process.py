from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import click
import psutil

from omnigent._platform import resolve_cli_binary
from omnigent.harnesses.pi_native.bridge import _atomic_text
from omnigent.harnesses.prime_native.bridge import (
    PRIME_NATIVE_CONFIG_ENV_VAR,
    PrimeRuntimePaths,
    runtime_paths,
)

QUALIFIED_VERSION = "0.9.6"
_ACTIVE_RUNTIMES: set[PrimeRuntimePaths] = set()


@dataclass(frozen=True)
class PrimeLaunch:
    executable: str
    argv: tuple[str, ...]
    env: Mapping[str, str]


def resolve_prime_executable() -> str:
    command = os.environ.get("OMNIGENT_PRIME_PATH", "").strip() or "prime-agent"
    executable = resolve_cli_binary(command, which=shutil.which)
    if executable is None:
        raise click.ClickException(
            "Prime Native requires prime-agent 0.9.6. "
            "Set OMNIGENT_PRIME_PATH or install it on PATH."
        )
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=10, check=True
    )
    if result.stdout.strip() != QUALIFIED_VERSION:
        raise click.ClickException(
            f"Prime Native requires prime-agent {QUALIFIED_VERSION}; "
            f"found {result.stdout.strip()!r}."
        )
    return executable


def build_prime_launch(
    paths: PrimeRuntimePaths,
    *,
    executable: str,
    extension: Path,
    config: Path,
    extra_args: Sequence[str] = (),
    external_session_id: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    instructions: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> PrimeLaunch:
    reserved = {
        "--resume",
        "-r",
        "--continue",
        "-c",
        "--session",
        "--session-dir",
        "--daemon-socket",
        "--extension",
        "-e",
        "--no-extensions",
        "--mode",
        "--print",
        "-p",
        "--no-session",
    }
    if any(arg.split("=", 1)[0] in reserved for arg in extra_args):
        raise ValueError(
            "Prime Native owns its session, daemon, extension, and interactive mode arguments."
        )
    paths.prepare()
    source_env = os.environ if environ is None else environ
    source_dir = Path(
        source_env.get("PRIME_AGENT_CODING_AGENT_DIR") or Path.home() / ".prime" / "agent"
    ).expanduser()
    for name in ("auth.json", "settings.json", "models.json"):
        source, target = source_dir / name, paths.agent_dir / name
        if source.is_file() and not target.exists():
            _atomic_text(target, source.read_text(encoding="utf-8"))
    args = ["--extension", str(extension), "--session-dir", str(paths.session_dir), *extra_args]
    if external_session_id:
        args.extend(["--resume", external_session_id])
    if model and not any(arg == "--model" or arg.startswith("--model=") for arg in extra_args):
        args.extend(["--model", model])
    if reasoning_effort and not any(
        arg == "--thinking" or arg.startswith("--thinking=") for arg in extra_args
    ):
        thinking = {"none": "off", "ultra": "max"}.get(reasoning_effort, reasoning_effort)
        if thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported Prime Native thinking level: {reasoning_effort}")
        args.extend(["--thinking", thinking])
    if instructions:
        args.extend(["--append-system-prompt", instructions])
    env = {**paths.env, PRIME_NATIVE_CONFIG_ENV_VAR: str(config)}
    if source_env.get("OMNIGENT_DATA_DIR"):
        env["OMNIGENT_DATA_DIR"] = source_env["OMNIGENT_DATA_DIR"]
    _atomic_text(paths.root / "executable", executable)
    _ACTIVE_RUNTIMES.add(paths)
    return PrimeLaunch(executable, tuple(args), env)


def _live_sockets(paths: PrimeRuntimePaths) -> list[Path]:
    live = []
    for path in paths.temp_dir.rglob("*.sock"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.1)
            try:
                client.connect(str(path))
            except OSError:
                continue
            live.append(path)
    return live


def _owned_processes(paths: PrimeRuntimePaths) -> list[psutil.Process]:
    processes: dict[int, psutil.Process] = {}
    for process in psutil.process_iter(["name"]):
        try:
            if process.uids().real != os.getuid() or process.name() != "prime-agent":
                continue
            if process.environ().get("PRIME_AGENT_CODING_AGENT_DIR") == str(paths.agent_dir):
                for owned in (process, *process.children(recursive=True)):
                    processes[owned.pid] = owned
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, SystemError):
            continue
    return list(processes.values())


def _stop_terminal(paths: PrimeRuntimePaths) -> None:
    try:
        record = json.loads((paths.root / "terminal.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return
    if not isinstance(record, dict) or not isinstance(record.get("pid"), int):
        raise RuntimeError("Prime terminal ownership record is invalid.")
    try:
        terminal = psutil.Process(record["pid"])
        if terminal.create_time() != record.get("created_at"):
            return
        terminal.terminate()
        try:
            terminal.wait(timeout=5)
        except psutil.TimeoutExpired:
            terminal.kill()
            terminal.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass


def stop_prime_runtime(paths: PrimeRuntimePaths) -> None:
    from omnigent.native.native_bridge_common import bridge_dir_preparation_lock

    if not paths.validate_existing():
        _ACTIVE_RUNTIMES.discard(paths)
        return
    _stop_terminal(paths)
    with bridge_dir_preparation_lock(paths.root):
        _stop_prime_runtime(paths)
    _ACTIVE_RUNTIMES.discard(paths)


def _stop_prime_runtime(paths: PrimeRuntimePaths) -> None:
    executable_file = paths.root / "executable"
    if not executable_file.is_file():
        return
    executable = executable_file.read_text(encoding="utf-8").strip()
    processes = _owned_processes(paths)
    completed = subprocess.run(
        [executable, "shutdown", "--force", "--json"],
        env={**os.environ, **paths.env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("Prime shutdown returned no valid result; runtime retained.") from exc
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("failed"), list)
        or not isinstance(result.get("stopped"), list)
        or result["failed"]
    ):
        raise RuntimeError(f"Prime shutdown failed; runtime retained: {completed.stdout.strip()}")
    deadline = time.monotonic() + 5
    while _live_sockets(paths) or any(_process_alive(process) for process in processes):
        if time.monotonic() >= deadline:
            raise RuntimeError("Prime shutdown left a scoped process or socket; runtime retained.")
        time.sleep(0.1)


def _process_alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def stop_session(session_id: str) -> None:
    stop_prime_runtime(runtime_paths(session_id))


def stop_all_runtimes() -> None:
    errors = []
    for paths in tuple(_ACTIVE_RUNTIMES):
        try:
            stop_prime_runtime(paths)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))


def main() -> int:
    paths = PrimeRuntimePaths(Path(sys.argv[1]))
    paths.prepare()
    child = subprocess.Popen(sys.argv[2:])
    terminal = psutil.Process(child.pid)
    record: dict[str, object] = {"pid": child.pid, "created_at": terminal.create_time()}
    from omnigent.harnesses.pi_native.bridge import _atomic_json

    _atomic_json(paths.root / "terminal.json", record)

    def forward_signal(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGHUP, forward_signal)
    try:
        return child.wait()
    finally:
        stop_prime_runtime(paths)


if __name__ == "__main__":
    raise SystemExit(main())

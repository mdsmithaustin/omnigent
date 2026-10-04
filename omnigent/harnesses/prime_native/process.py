from __future__ import annotations

import contextlib
import errno
import json
import logging
import math
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import click
import psutil

from omnigent._platform import IS_WINDOWS, resolve_cli_binary
from omnigent.harnesses.pi_native.bridge import _atomic_text
from omnigent.harnesses.prime_native.bridge import (
    KERNEL_PROCESS_NAMES_FILE,
    PRIME_NATIVE_CONFIG_ENV_VAR,
    PrimeRuntimePaths,
    bridge_roots,
    runtime_paths,
)
from omnigent.native import native_bridge_common, owner_claim

QUALIFIED_VERSION = "0.9.6"
_DAEMON_PROTOCOL = {"name": "prime-agent.daemon", "version": 7}
_DAEMON_SCHEMA_ID = "protocol-7-schema-30-f908f493c9e1"
_DAEMON_SCHEMA_REVISION = 30
_SOCKET_IO_TIMEOUT_S = 1.5
_SHUTDOWN_SETTLE_TIMEOUT_S = 5.0
_SHUTDOWN_POLL_INTERVAL_S = 0.1
_LAUNCH_IDENTITY_TIMEOUT_S = 5.0
_LAUNCH_RESERVATION_FILE = "launch.pending.json"
_ACTIVE_RUNTIMES: set[PrimeRuntimePaths] = set()
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrimeLaunch:
    executable: str
    argv: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    created_at: float


@dataclass(frozen=True)
class _TerminalIdentity:
    pid: int
    created_at: float


@dataclass(frozen=True)
class PrimeLaunchReservation:
    token: str
    owner: _ProcessIdentity
    state: Literal["pending", "abandoned"] = "pending"


@dataclass(frozen=True)
class _SupervisorEndpoint:
    path: Path
    device: int
    inode: int


class _EndpointUnreachable(RuntimeError):
    pass


class _JsonlReader:
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._buffer = bytearray()

    def read(self, description: str, *, allow_eof: bool = False) -> dict[str, object] | None:
        while b"\n" not in self._buffer:
            try:
                chunk = self._connection.recv(65536)
            except TimeoutError as exc:
                raise RuntimeError(f"Prime {description} timed out; runtime retained.") from exc
            except OSError as exc:
                raise RuntimeError(
                    f"Prime {description} could not be read; runtime retained."
                ) from exc
            if not chunk:
                if allow_eof and not self._buffer:
                    return None
                raise RuntimeError(f"Prime {description} was incomplete; runtime retained.")
            self._buffer.extend(chunk)
            if len(self._buffer) > 1_048_576:
                raise RuntimeError(
                    f"Prime {description} exceeded the protocol limit; runtime retained."
                )
        line, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"Prime {description} was malformed; runtime retained.") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Prime {description} was malformed; runtime retained.")
        return value


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


def _process_name_aliases(executable: str) -> set[str]:
    path = Path(executable)
    return {path.name.casefold(), path.resolve().name.casefold()} - {""}


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
    if IS_WINDOWS:
        raise RuntimeError("Prime Native private shutdown is unavailable on Windows.")
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
    args = ["--extension", str(extension), "--session-dir", str(paths.session_dir), *extra_args]
    if external_session_id:
        args.extend(["--resume", external_session_id])
    if model and not any(arg == "--model" or arg.startswith("--model=") for arg in extra_args):
        args.extend(["--model", model])
    if reasoning_effort and not any(
        arg == "--thinking" or arg.startswith("--thinking=") for arg in extra_args
    ):
        from omnigent.util.reasoning_effort import PI_EFFORTS, to_pi_thinking_level

        if reasoning_effort not in PI_EFFORTS:
            raise ValueError(f"Unsupported Prime Native thinking level: {reasoning_effort}")
        args.extend(["--thinking", to_pi_thinking_level(reasoning_effort)])
    if instructions:
        args.extend(["--append-system-prompt", instructions])
    env = {**paths.env, PRIME_NATIVE_CONFIG_ENV_VAR: str(config)}
    if source_env.get("OMNIGENT_DATA_DIR"):
        env["OMNIGENT_DATA_DIR"] = source_env["OMNIGENT_DATA_DIR"]
    kernel_executable = source_env.get("PRIME_AGENT_KERNEL_PYTHON", "")
    kernel_process_names: list[str] = []
    if kernel_executable:
        resolved = (
            shutil.which(kernel_executable, path=source_env.get("PATH")) or kernel_executable
        )
        kernel_process_names = sorted(
            _process_name_aliases(kernel_executable) | _process_name_aliases(resolved)
        )
    with native_bridge_common.bridge_dir_preparation_lock(paths.root):
        paths.validate_existing()
        for name in ("auth.json", "settings.json", "models.json"):
            source, target = source_dir / name, paths.agent_dir / name
            if source.is_file() and not target.exists():
                _atomic_text(target, source.read_text(encoding="utf-8"))
        _atomic_text(paths.root / "executable", executable)
        _atomic_text(paths.root / KERNEL_PROCESS_NAMES_FILE, json.dumps(kernel_process_names))
        owner_claim.write_owner_claim(paths.root)
        _ACTIVE_RUNTIMES.add(paths)
    return PrimeLaunch(executable, tuple(args), env)


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise RuntimeError(
            "Prime private Unix socket ownership cannot be verified; runtime retained."
        )
    return getuid()


def _default_supervisor_socket(paths: PrimeRuntimePaths) -> Path:
    return paths.temp_dir / f"prime-agent-{_current_uid()}" / "daemon.sock"


def _owned_socket_metadata(path: Path, *, expected: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Prime {expected} could not be inspected; runtime retained.") from exc
    if metadata.st_uid != _current_uid():
        raise RuntimeError(f"Prime {expected} is owned by another user; runtime retained.")
    return metadata


def _supervisor_endpoint(paths: PrimeRuntimePaths) -> _SupervisorEndpoint | None:
    if IS_WINDOWS:
        raise RuntimeError(
            "Prime private supervisor shutdown is unavailable on Windows; runtime retained."
        )
    socket_path = _default_supervisor_socket(paths)
    try:
        directory = _owned_socket_metadata(socket_path.parent, expected="socket directory")
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(directory.st_mode):
        raise RuntimeError("Prime socket directory is not a directory; runtime retained.")
    try:
        endpoint = _owned_socket_metadata(socket_path, expected="supervisor socket")
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(endpoint.st_mode):
        raise RuntimeError("Prime supervisor endpoint is not a Unix socket; runtime retained.")
    return _SupervisorEndpoint(socket_path, endpoint.st_dev, endpoint.st_ino)


def _endpoint_is_unchanged(endpoint: _SupervisorEndpoint) -> None:
    try:
        metadata = _owned_socket_metadata(endpoint.path, expected="supervisor socket")
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Prime supervisor socket changed during connection; runtime retained."
        ) from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_dev != endpoint.device
        or metadata.st_ino != endpoint.inode
    ):
        raise RuntimeError("Prime supervisor socket changed during connection; runtime retained.")


def _live_sockets(paths: PrimeRuntimePaths) -> list[Path]:
    live = []
    try:
        candidates = list(paths.temp_dir.rglob("*.sock"))
    except OSError as exc:
        raise RuntimeError(
            "Prime private sockets could not be enumerated; runtime retained."
        ) from exc
    for path in candidates:
        try:
            metadata = _owned_socket_metadata(path, expected="private socket")
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(metadata.st_mode):
            raise RuntimeError(f"Prime private socket path is not a socket: {path}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.1)
            try:
                client.connect(str(path))
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                    continue
                raise RuntimeError(f"Prime private socket could not be observed: {path}") from exc
            live.append(path)
    return live


def _read_kernel_process_names(paths: PrimeRuntimePaths) -> set[str]:
    try:
        value = json.loads((paths.root / KERNEL_PROCESS_NAMES_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            "Prime kernel process names record is invalid; runtime retained."
        ) from exc
    if (
        not isinstance(value, list)
        or len(value) > 2
        or any(not isinstance(name, str) or not name or name != name.casefold() for name in value)
    ):
        raise RuntimeError("Prime kernel process names record is invalid; runtime retained.")
    return set(value)


def _read_prime_process_names(paths: PrimeRuntimePaths) -> set[str]:
    try:
        executable = (paths.root / "executable").read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Prime executable record is invalid; runtime retained.") from exc
    if not executable:
        raise RuntimeError("Prime executable record is invalid; runtime retained.")
    try:
        names = _process_name_aliases(executable)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError("Prime executable record is invalid; runtime retained.") from exc
    if not names:
        raise RuntimeError("Prime executable record is invalid; runtime retained.")
    return names


def _name_matches_alias(name: str, aliases: set[str]) -> bool:
    return name in aliases or (
        len(name) in {15, 16} and any(alias.startswith(name) for alias in aliases)
    )


def _owned_processes(paths: PrimeRuntimePaths) -> list[psutil.Process]:
    processes: dict[int, psutil.Process] = {}
    kernel_process_names = _read_kernel_process_names(paths)
    prime_process_names = _read_prime_process_names(paths)
    claim = owner_claim.read_owner_claim(paths.root)
    for process in psutil.process_iter(["name"]):
        name = str(process.info.get("name") or "").casefold()
        # Some processes report executable names through a 15/16-character field.
        prime_name = name == "prime-agent" or _name_matches_alias(name, prime_process_names)
        kernel_name = _name_matches_alias(name, kernel_process_names)
        if not prime_name and not name.startswith("python") and not kernel_name:
            continue
        try:
            if process.uids().real != os.getuid():
                continue
            if not prime_name and tuple(process.cmdline()[1:3]) not in {
                ("-m", "rlm.repl"),
                ("-m", "omnigent.harnesses.prime_native.process"),
            }:
                continue
            if process.environ().get("PRIME_AGENT_CODING_AGENT_DIR") == str(paths.agent_dir):
                if claim is not None and process.pid == claim.pid:
                    continue
                for owned in (process, *process.children(recursive=True)):
                    processes[owned.pid] = owned
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError, SystemError) as exc:
            for attempt in range(3):
                if not _process_alive(process):
                    break
                if attempt < 2:
                    time.sleep(_SHUTDOWN_POLL_INTERVAL_S)
            else:
                raise RuntimeError(
                    "Prime process ownership could not be observed; runtime retained."
                ) from exc
    return list(processes.values())


def _process_identity(process: psutil.Process) -> _ProcessIdentity | None:
    try:
        return _ProcessIdentity(process.pid, process.create_time())
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except (psutil.AccessDenied, OSError, SystemError) as exc:
        raise RuntimeError(
            "Prime process identity could not be observed; runtime retained."
        ) from exc


def _owned_process_identities(paths: PrimeRuntimePaths) -> set[_ProcessIdentity]:
    identities = (_process_identity(process) for process in _owned_processes(paths))
    return {identity for identity in identities if identity is not None}


def _identity_alive(identity: _ProcessIdentity) -> bool:
    try:
        process = psutil.Process(identity.pid)
        if process.create_time() != identity.created_at:
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.AccessDenied, OSError, SystemError) as exc:
        raise RuntimeError(
            "Prime process liveness could not be observed; runtime retained."
        ) from exc


def _read_terminal_identity(paths: PrimeRuntimePaths) -> _TerminalIdentity | None:
    try:
        record = json.loads((paths.root / "terminal.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("Prime terminal ownership record is invalid.") from exc
    if not isinstance(record, dict):
        raise RuntimeError("Prime terminal ownership record is invalid.")
    pid = record.get("pid")
    created_at = record.get("created_at")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(created_at, (int, float))
        or isinstance(created_at, bool)
        or not math.isfinite(created_at)
    ):
        raise RuntimeError("Prime terminal ownership record is invalid.")
    return _TerminalIdentity(pid, float(created_at))


def _launch_reservation_path(paths: PrimeRuntimePaths) -> Path:
    return paths.root / _LAUNCH_RESERVATION_FILE


def _read_launch_reservation(paths: PrimeRuntimePaths) -> PrimeLaunchReservation | None:
    path = _launch_reservation_path(paths)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Prime launch reservation could not be inspected.") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != _current_uid()
    ):
        raise RuntimeError("Prime launch reservation is not a private regular file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("Prime launch reservation is invalid.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Prime launch reservation is invalid.")
    token = value.get("token")
    pid = value.get("pid")
    created_at = value.get("created_at")
    state = value.get("state")
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(created_at, (int, float))
        or isinstance(created_at, bool)
        or not math.isfinite(created_at)
        or not isinstance(state, str)
        or state not in {"pending", "abandoned"}
    ):
        raise RuntimeError("Prime launch reservation is invalid.")
    return PrimeLaunchReservation(token, _ProcessIdentity(pid, float(created_at)), state)


def _write_launch_reservation(
    paths: PrimeRuntimePaths, reservation: PrimeLaunchReservation
) -> None:
    _atomic_text(
        _launch_reservation_path(paths),
        json.dumps(
            {
                "token": reservation.token,
                "pid": reservation.owner.pid,
                "created_at": reservation.owner.created_at,
                "state": reservation.state,
            }
        ),
    )


def reserve_prime_launch(paths: PrimeRuntimePaths) -> PrimeLaunchReservation:
    if IS_WINDOWS:
        raise RuntimeError("Prime Native private shutdown is unavailable on Windows.")
    with native_bridge_common.bridge_dir_preparation_lock(paths.root):
        paths.prepare()
        previous = _read_launch_reservation(paths)
        if previous is not None:
            if _identity_alive(previous.owner):
                raise RuntimeError(
                    "Prime terminal launch is already in progress; runtime retained."
                )
            _stop_prime_runtime(paths)
            _launch_reservation_path(paths).unlink()
        owner = _process_identity(psutil.Process(os.getpid()))
        if owner is None:
            raise RuntimeError("Prime launch owner could not be identified.")
        reservation = PrimeLaunchReservation(uuid.uuid4().hex, owner)
        _write_launch_reservation(paths, reservation)
        return reservation


def complete_prime_launch(paths: PrimeRuntimePaths, reservation: PrimeLaunchReservation) -> None:
    deadline = time.monotonic() + _LAUNCH_IDENTITY_TIMEOUT_S
    while True:
        with native_bridge_common.bridge_dir_preparation_lock(paths.root):
            paths.validate_existing()
            if _read_launch_reservation(paths) != reservation:
                raise RuntimeError("Prime launch reservation changed; runtime retained.")
            identity = _read_terminal_identity(paths)
            terminal = _terminal_process(identity) if identity is not None else None
            if terminal is not None and _process_alive(terminal):
                _launch_reservation_path(paths).unlink()
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("Prime terminal identity did not become live; runtime retained.")
        time.sleep(_SHUTDOWN_POLL_INTERVAL_S)


def abandon_prime_launch(
    paths: PrimeRuntimePaths, reservation: PrimeLaunchReservation, *, dispatched: bool
) -> None:
    with native_bridge_common.bridge_dir_preparation_lock(paths.root):
        paths.validate_existing()
        if _read_launch_reservation(paths) != reservation:
            raise RuntimeError("Prime launch reservation changed; runtime retained.")
        if dispatched:
            if _read_terminal_identity(paths) is None:
                raise RuntimeError(
                    "Prime terminal dispatch has no ownership record; runtime retained."
                )
            _write_launch_reservation(
                paths, PrimeLaunchReservation(reservation.token, reservation.owner, "abandoned")
            )
            _stop_prime_runtime(paths)
        _launch_reservation_path(paths).unlink()


def _terminal_process(identity: _TerminalIdentity) -> psutil.Process | None:
    try:
        terminal = psutil.Process(identity.pid)
        if terminal.create_time() != identity.created_at:
            return None
        return terminal
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except (psutil.AccessDenied, OSError, SystemError) as exc:
        raise RuntimeError(
            "Prime terminal process could not be observed; runtime retained."
        ) from exc


def _stop_terminal(paths: PrimeRuntimePaths) -> None:
    terminal_identity = _read_terminal_identity(paths)
    if terminal_identity is None:
        return
    terminal = _terminal_process(terminal_identity)
    if terminal is None:
        return
    try:
        terminal.terminate()
        try:
            terminal.wait(timeout=5)
        except psutil.TimeoutExpired:
            terminal.kill()
            terminal.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass
    except (psutil.AccessDenied, OSError, SystemError) as exc:
        raise RuntimeError(
            "Prime terminal process could not be stopped; runtime retained."
        ) from exc


def _qualify_supervisor_process(
    pid: object, paths: PrimeRuntimePaths
) -> tuple[_ProcessIdentity, set[_ProcessIdentity]]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError("Prime daemon hello has no qualified supervisor PID; runtime retained.")
    try:
        supervisor = psutil.Process(pid)
        if supervisor.uids().real != _current_uid():
            raise RuntimeError("Prime supervisor belongs to another user; runtime retained.")
        if supervisor.environ().get("PRIME_AGENT_CODING_AGENT_DIR") != str(paths.agent_dir):
            raise RuntimeError(
                "Prime supervisor does not belong to this private runtime; runtime retained."
            )
        identity = _process_identity(supervisor)
        if identity is None:
            raise RuntimeError("Prime supervisor exited during qualification; runtime retained.")
        child_identities = (
            _process_identity(child) for child in supervisor.children(recursive=True)
        )
        descendants = {
            child_identity for child_identity in child_identities if child_identity is not None
        }
    except (psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
        raise RuntimeError(
            "Prime supervisor exited during qualification; runtime retained."
        ) from exc
    except (psutil.AccessDenied, OSError, SystemError) as exc:
        raise RuntimeError(
            "Prime supervisor ownership could not be observed; runtime retained."
        ) from exc
    return identity, descendants


def _validate_daemon_hello(
    hello: dict[str, object], endpoint: _SupervisorEndpoint, paths: PrimeRuntimePaths
) -> tuple[str, set[_ProcessIdentity]]:
    expected_path = str(endpoint.path)
    if (
        hello.get("type") != "daemon_hello"
        or hello.get("protocol") != _DAEMON_PROTOCOL
        or hello.get("schemaId") != _DAEMON_SCHEMA_ID
        or hello.get("schemaRevision") != _DAEMON_SCHEMA_REVISION
        or hello.get("appVersion") != QUALIFIED_VERSION
        or hello.get("socketPath") != expected_path
        or hello.get("supervisorSocketPath") != expected_path
    ):
        raise RuntimeError(
            "Prime daemon hello did not qualify the private endpoint; runtime retained."
        )
    client_id = hello.get("clientId")
    if not isinstance(client_id, str) or not client_id:
        raise RuntimeError("Prime daemon hello has no client identity; runtime retained.")
    supervisor, descendants = _qualify_supervisor_process(hello.get("supervisorPid"), paths)
    return client_id, {supervisor, *descendants}


def _send_shutdown(
    endpoint: _SupervisorEndpoint, paths: PrimeRuntimePaths
) -> set[_ProcessIdentity]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(_SOCKET_IO_TIMEOUT_S)
        try:
            connection.connect(str(endpoint.path))
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                raise _EndpointUnreachable("Prime supervisor endpoint is unreachable.") from exc
            raise RuntimeError(
                "Prime supervisor endpoint could not be observed; runtime retained."
            ) from exc
        _endpoint_is_unchanged(endpoint)
        reader = _JsonlReader(connection)
        hello = reader.read("daemon hello")
        assert hello is not None
        client_id, identities = _validate_daemon_hello(hello, endpoint, paths)
        command_id = f"omnigent-shutdown-{uuid.uuid4().hex}"
        envelope = {
            "type": "command",
            "id": command_id,
            "protocol": _DAEMON_PROTOCOL,
            "clientId": client_id,
            "command": {"type": "shutdown", "force": True},
        }
        try:
            connection.sendall(json.dumps(envelope, separators=(",", ":")).encode("utf-8") + b"\n")
        except OSError as exc:
            raise RuntimeError(
                "Prime shutdown command could not be sent; runtime retained."
            ) from exc
        response = reader.read("shutdown response", allow_eof=True)
        if response is None:
            return identities
        if response != {
            "id": command_id,
            "type": "response",
            "command": "shutdown",
            "success": True,
        }:
            raise RuntimeError("Prime shutdown response was invalid; runtime retained.")
        return identities


def _wait_for_runtime_absence(paths: PrimeRuntimePaths, captured: set[_ProcessIdentity]) -> None:
    deadline = time.monotonic() + _SHUTDOWN_SETTLE_TIMEOUT_S
    while True:
        fresh = _owned_process_identities(paths)
        observed = captured | fresh
        survivors = [identity for identity in observed if _identity_alive(identity)]
        live_sockets = _live_sockets(paths)
        if not survivors and not live_sockets:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Prime shutdown left a scoped process or socket; runtime retained. "
                f"PIDs: {[identity.pid for identity in survivors]}; "
                f"sockets: {[str(path) for path in live_sockets]}"
            )
        time.sleep(_SHUTDOWN_POLL_INTERVAL_S)


def _remove_runtime_records(paths: PrimeRuntimePaths) -> None:
    (paths.root / owner_claim.OWNER_PID_FILENAME).unlink(missing_ok=True)
    (paths.root / "terminal.json").unlink(missing_ok=True)


def stop_prime_runtime(
    paths: PrimeRuntimePaths, *, launch_reservation: PrimeLaunchReservation | None = None
) -> None:
    paths.validate_existing()
    with native_bridge_common.bridge_dir_preparation_lock(paths.root):
        if not paths.validate_existing():
            _ACTIVE_RUNTIMES.discard(paths)
            return
        active_reservation = _read_launch_reservation(paths)
        clear_reservation = False
        if active_reservation is None:
            if launch_reservation is not None:
                raise RuntimeError("Prime launch reservation disappeared; runtime retained.")
        elif active_reservation != launch_reservation:
            if launch_reservation is not None:
                raise RuntimeError("Prime launch reservation changed; runtime retained.")
            if _identity_alive(active_reservation.owner):
                if active_reservation.state != "abandoned":
                    raise RuntimeError("Prime terminal launch is in progress; runtime retained.")
                if _read_terminal_identity(paths) is None:
                    raise RuntimeError("Prime terminal dispatch is unconfirmed; runtime retained.")
            clear_reservation = True
        _stop_prime_runtime(paths)
        if clear_reservation:
            _launch_reservation_path(paths).unlink()
    _ACTIVE_RUNTIMES.discard(paths)


def _stop_prime_runtime(paths: PrimeRuntimePaths) -> None:
    endpoint = _supervisor_endpoint(paths)
    captured = _owned_process_identities(paths)
    shutdown_sent = False
    if endpoint is not None:
        try:
            captured.update(_send_shutdown(endpoint, paths))
            shutdown_sent = True
        except _EndpointUnreachable:
            pass
    if shutdown_sent:
        _stop_terminal(paths)
    else:
        terminal_identity = _read_terminal_identity(paths)
        terminal = _terminal_process(terminal_identity) if terminal_identity else None
        if terminal is not None and _process_alive(terminal):
            raise RuntimeError(
                "Prime private supervisor is unreachable with a live terminal; runtime retained."
            )
    _wait_for_runtime_absence(paths, captured)
    _remove_runtime_records(paths)


def stop_orphaned_runtimes() -> int:
    from omnigent.harnesses.claude_native.bridge import ensure_secure_dir
    from omnigent.inner.terminal import _process_alive as owner_process_alive

    stopped = 0
    for root in bridge_roots():
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        ensure_secure_dir(root)
        for entry in root.iterdir():
            if entry.name == ".locks":
                continue
            paths = PrimeRuntimePaths(entry)
            try:
                if not paths.validate_existing():
                    continue
                with native_bridge_common._try_bridge_dir_cleanup_lock(entry) as acquired:
                    if not acquired or not paths.validate_existing():
                        continue
                    reservation = _read_launch_reservation(paths)
                    if reservation is not None and _identity_alive(reservation.owner):
                        continue
                    claim = owner_claim.read_owner_claim(entry)
                    if reservation is None:
                        if claim is None or not owner_claim.owner_is_gone(
                            claim, process_alive=owner_process_alive
                        ):
                            continue
                    terminal_identity = _read_terminal_identity(paths)
                    terminal = (
                        _terminal_process(terminal_identity)
                        if terminal_identity is not None
                        else None
                    )
                    if terminal is not None and _process_alive(terminal):
                        continue
                    _stop_prime_runtime(paths)
                    if reservation is not None:
                        _launch_reservation_path(paths).unlink()
                    _ACTIVE_RUNTIMES.discard(paths)
                    stopped += 1
            except (OSError, RuntimeError, subprocess.SubprocessError):
                _logger.exception("Error stopping orphaned Prime runtime %s", entry)
    return stopped


def _process_alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.AccessDenied, OSError, SystemError) as exc:
        raise RuntimeError(
            "Prime process liveness could not be observed; runtime retained."
        ) from exc


def _wrapper_still_owns_runtime(paths: PrimeRuntimePaths) -> bool:
    claim = owner_claim.read_owner_claim(paths.root)
    if claim is None or claim.pid != os.getpid():
        return False
    return (
        claim.pid_ns == owner_claim.current_pid_namespace()
        and claim.boot_id == owner_claim.current_boot_id()
    )


def _finalize_wrapper_runtime(
    paths: PrimeRuntimePaths, expected_terminal: _TerminalIdentity
) -> None:
    with native_bridge_common.bridge_dir_preparation_lock(paths.root):
        if not paths.validate_existing():
            return
        if _read_terminal_identity(paths) != expected_terminal:
            return
        if not _wrapper_still_owns_runtime(paths):
            return
        _stop_prime_runtime(paths)
    _ACTIVE_RUNTIMES.discard(paths)


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
    from omnigent.harnesses.pi_native.bridge import _atomic_json

    with native_bridge_common.bridge_dir_preparation_lock(paths.root):
        paths.validate_existing()
        owner_claim.write_owner_claim(paths.root)
        child = subprocess.Popen(sys.argv[2:])
        terminal = psutil.Process(child.pid)
        terminal_identity = _TerminalIdentity(child.pid, terminal.create_time())
        record: dict[str, object] = {
            "pid": terminal_identity.pid,
            "created_at": terminal_identity.created_at,
        }
        _atomic_json(paths.root / "terminal.json", record)

    def forward_signal(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGHUP, forward_signal)
    try:
        return child.wait()
    finally:
        _finalize_wrapper_runtime(paths, terminal_identity)


if __name__ == "__main__":
    raise SystemExit(main())

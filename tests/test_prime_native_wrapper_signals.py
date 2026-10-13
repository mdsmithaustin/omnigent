from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from omnigent.harnesses.prime_native import bridge, process
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.harnesses.prime_native.process import build_prime_launch
from omnigent.native import native_bridge_common
from omnigent.native.owner_claim import read_owner_claim
from tests.test_prime_native import _prime_daemon, _PrimeDaemon
from tests.test_prime_native import admitted_wrapper_paths as admitted_wrapper_paths
from tests.test_prime_native import private_prime_roots as private_prime_roots

_STOP_SIGNALS = pytest.mark.parametrize(
    "signum", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"]
)

_SIGNAL_AT_POINT = r"""
import errno, json, os, subprocess, sys
from pathlib import Path
import filelock, psutil
from omnigent.harnesses.pi_native import bridge as pi_bridge
from omnigent.harnesses.prime_native import bridge, process
from omnigent.native import admission

bridge._DATA_ROOT = Path(sys.argv[1])
bridge._COMPACT_ROOT = Path(sys.argv[2])
point, signum, spawned_record, failing_record = (
    sys.argv[3], int(sys.argv[4]), Path(sys.argv[5]), sys.argv[6]
)
sys.argv = sys.argv[:1] + sys.argv[7:]
fired = False

def fire(at):
    global fired
    if at == point and not fired:
        fired = True
        os.kill(os.getpid(), signum)

def before(owner, name, at):
    original = getattr(owner, name)
    def wrapped(*args, **kwargs):
        fire(at)
        return original(*args, **kwargs)
    setattr(owner, name, wrapped)

def after(owner, name, at):
    original = getattr(owner, name)
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        fire(at(*args))
        return result
    setattr(owner, name, wrapped)

def failing_json(original):
    def wrapped(path, payload):
        if path.name == failing_record:
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        return original(path, payload)
    return wrapped

popen_init = subprocess.Popen.__init__
def spawn(self, *args, **kwargs):
    fire("popen")
    popen_init(self, *args, **kwargs)
    created_at = psutil.Process(self.pid).create_time()
    spawned_record.write_text(json.dumps({"pid": self.pid, "created_at": created_at}))

pi_bridge._atomic_json = failing_json(pi_bridge._atomic_json)
before(process.native_bridge_common, "bridge_dir_preparation_lock", "prep-lock")
after(filelock.UnixFileLock, "_acquire", lambda lock: None if lock.is_locked else "lock-wait")
before(admission, "validate_native_sync", "validate")
after(process, "_atomic_text", lambda path, _text: path.name)
after(process.owner_claim, "write_owner_claim", lambda _root: "owner.pid")
after(pi_bridge, "_atomic_json", lambda path, _payload: path.name)
subprocess.Popen.__init__ = spawn
before(subprocess.Popen, "wait", "wait")
raise SystemExit(process.main())
"""


def _terminal_alive(record: dict[str, float]) -> bool:
    try:
        terminal = psutil.Process(int(record["pid"]))
        return (
            terminal.create_time() == record["created_at"]
            and terminal.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def _run_wrapper_signalled_at(
    paths: PrimeRuntimePaths,
    tmp_path: Path,
    daemon: _PrimeDaemon,
    point: str,
    signum: signal.Signals,
    *,
    hold_prep_lock: bool = False,
    failing_record: str = "",
) -> dict[str, object]:
    spawned_record = tmp_path / "spawned-terminal.json"
    launch = build_prime_launch(
        paths,
        executable=(paths.root / "executable").read_text(),
        extension=Path("e"),
        config=Path("c"),
        environ={"OMNIGENT_DATA_DIR": str(tmp_path)},
    )
    command = [
        sys.executable,
        "-c",
        _SIGNAL_AT_POINT,
        str(bridge._DATA_ROOT),
        str(bridge._COMPACT_ROOT),
        point,
        str(int(signum)),
        str(spawned_record),
        failing_record,
        str(paths.root),
        process.reserve_prime_launch(paths).token,
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    ]
    terminal = None
    try:
        with (
            native_bridge_common.bridge_dir_preparation_lock(paths.root)
            if hold_prep_lock
            else contextlib.nullcontext()
        ):
            wrapper = subprocess.run(
                command, env={**os.environ, **launch.env}, timeout=30, check=False
            )
        terminal = json.loads(spawned_record.read_text()) if spawned_record.exists() else None
        reservation = paths.root / "launch.pending.json"
        return {
            "wrapper_returncode": wrapper.returncode,
            "terminal_spawned": terminal is not None,
            "terminal_alive": terminal is not None and _terminal_alive(terminal),
            "terminal_record": (paths.root / "terminal.json").exists(),
            "reservation": json.loads(reservation.read_text()) if reservation.exists() else None,
            "shutdown_sent": daemon.command_path.exists(),
        }
    finally:
        if terminal is None and spawned_record.exists():
            terminal = json.loads(spawned_record.read_text())
        if terminal is not None and _terminal_alive(terminal):
            psutil.Process(int(terminal["pid"])).kill()


@_STOP_SIGNALS
@pytest.mark.parametrize(
    ("point", "hold_prep_lock"),
    [("prep-lock", False), ("lock-wait", True), ("validate", False)],
    ids=["prep-lock", "lock-wait", "validate"],
)
def test_signal_before_claim_ends_wrapper_by_default_action(
    admitted_wrapper_paths: PrimeRuntimePaths,
    tmp_path: Path,
    point: str,
    hold_prep_lock: bool,
    signum: signal.Signals,
) -> None:
    paths = admitted_wrapper_paths
    with _prime_daemon(paths, tmp_path) as daemon:
        observed = _run_wrapper_signalled_at(
            paths, tmp_path, daemon, point, signum, hold_prep_lock=hold_prep_lock
        )
        claim = read_owner_claim(paths.root)
        observed["owner_is_launcher"] = claim is not None and claim.pid == os.getpid()
        reservation = observed.pop("reservation")
        assert isinstance(reservation, dict)
        assert (reservation["state"], reservation["wrapper"]) == ("pending", None)
        assert observed == {
            "wrapper_returncode": -signum,
            "terminal_spawned": False,
            "terminal_alive": False,
            "terminal_record": False,
            "shutdown_sent": False,
            "owner_is_launcher": True,
        }


@_STOP_SIGNALS
@pytest.mark.parametrize("point", ["launch.pending.json", "owner.pid"])
def test_signal_after_claim_releases_runtime_without_starting_terminal(
    admitted_wrapper_paths: PrimeRuntimePaths,
    tmp_path: Path,
    point: str,
    signum: signal.Signals,
) -> None:
    paths = admitted_wrapper_paths
    with _prime_daemon(paths, tmp_path) as daemon:
        observed = _run_wrapper_signalled_at(paths, tmp_path, daemon, point, signum)
        observed["owner_record"] = (paths.root / "owner.pid").exists()
        assert observed == {
            "wrapper_returncode": -signum & 0xFF,
            "terminal_spawned": False,
            "terminal_alive": False,
            "terminal_record": False,
            "reservation": None,
            "shutdown_sent": True,
            "owner_record": False,
        }
    assert process.reserve_prime_launch(paths).state == "pending"
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


@_STOP_SIGNALS
@pytest.mark.parametrize("point", ["popen", "terminal.json", "wait"])
def test_signal_after_terminal_spawn_stops_terminal_and_cleans_runtime(
    admitted_wrapper_paths: PrimeRuntimePaths,
    tmp_path: Path,
    point: str,
    signum: signal.Signals,
) -> None:
    paths = admitted_wrapper_paths
    with _prime_daemon(paths, tmp_path) as daemon:
        observed = _run_wrapper_signalled_at(paths, tmp_path, daemon, point, signum)
        observed["owner_record"] = (paths.root / "owner.pid").exists()
        assert observed == {
            "wrapper_returncode": -signum & 0xFF,
            "terminal_spawned": True,
            "terminal_alive": False,
            "terminal_record": False,
            "reservation": None,
            "shutdown_sent": True,
            "owner_record": False,
        }
        assert json.loads(daemon.command_path.read_text())["command"] == {
            "type": "shutdown",
            "force": True,
        }
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"


@_STOP_SIGNALS
def test_signal_then_terminal_record_failure_stops_terminal_and_releases_runtime(
    admitted_wrapper_paths: PrimeRuntimePaths,
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    paths = admitted_wrapper_paths
    with _prime_daemon(paths, tmp_path) as daemon:
        observed = _run_wrapper_signalled_at(
            paths, tmp_path, daemon, "popen", signum, failing_record="terminal.json"
        )
        observed["owner_record"] = (paths.root / "owner.pid").exists()
        assert observed == {
            "wrapper_returncode": 1,
            "terminal_spawned": True,
            "terminal_alive": False,
            "terminal_record": False,
            "reservation": None,
            "shutdown_sent": True,
            "owner_record": False,
        }
    assert process.reserve_prime_launch(paths).state == "pending"


def _ignore_stop(_signum: int, _frame: object) -> None:
    pass


@pytest.fixture
def caller_stop_handlers() -> Iterator[None]:
    originals = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)}
    for signum in originals:
        signal.signal(signum, _ignore_stop)
    try:
        yield
    finally:
        for signum, handler in originals.items():
            signal.signal(signum, handler)


@pytest.mark.parametrize(
    ("terminal", "outcome"),
    [
        ((sys.executable, "-c", "import time; time.sleep(1)"), "returned 0"),
        (("/nonexistent/prime-agent",), "raised FileNotFoundError"),
    ],
    ids=["terminal-exits", "spawn-fails"],
)
def test_main_restores_caller_stop_handlers(
    admitted_wrapper_paths: PrimeRuntimePaths,
    caller_stop_handlers: None,
    monkeypatch: pytest.MonkeyPatch,
    terminal: tuple[str, ...],
    outcome: str,
) -> None:
    paths = admitted_wrapper_paths
    token = process.reserve_prime_launch(paths).token
    monkeypatch.setattr(sys, "argv", ["process", str(paths.root), token, *terminal])
    try:
        observed = f"returned {process.main()}"
    except FileNotFoundError:
        observed = "raised FileNotFoundError"
    assert observed == outcome
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == (
        _ignore_stop,
        _ignore_stop,
    )

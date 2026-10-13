from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from omnigent.harnesses.prime_native import bridge, process
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.harnesses.prime_native.process import build_prime_launch
from tests.test_prime_native import _prime_daemon, _PrimeDaemon
from tests.test_prime_native import admitted_wrapper_paths as admitted_wrapper_paths
from tests.test_prime_native import private_prime_roots as private_prime_roots

_SIGNAL_AT_POINT = r"""
import json, os, signal, subprocess, sys
from pathlib import Path
from omnigent.harnesses.pi_native import bridge as pi_bridge
from omnigent.harnesses.prime_native import bridge, process

bridge._DATA_ROOT = Path(sys.argv[1])
bridge._COMPACT_ROOT = Path(sys.argv[2])
point, signum, spawned_record = sys.argv[3], int(sys.argv[4]), Path(sys.argv[5])
sys.argv = sys.argv[:1] + sys.argv[6:]
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

def published_json(path, payload):
    if path.name == "terminal.json":
        spawned_record.write_text(json.dumps(payload))
    return path.name

before(process.native_bridge_common, "bridge_dir_preparation_lock", "prep-lock")
after(process, "_atomic_text", lambda path, _text: path.name)
after(process.owner_claim, "write_owner_claim", lambda _root: "owner.pid")
after(pi_bridge, "_atomic_json", published_json)
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
        str(paths.root),
        process.reserve_prime_launch(paths).token,
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    ]
    terminal = None
    try:
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


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
@pytest.mark.parametrize("point", ["launch.pending.json", "owner.pid", "terminal.json", "wait"])
def test_signal_after_wrapper_claims_launch_stops_terminal_and_cleans_runtime(
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


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_signal_before_wrapper_claims_launch_leaves_pending_reservation(
    admitted_wrapper_paths: PrimeRuntimePaths,
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    from omnigent.native.owner_claim import read_owner_claim

    paths = admitted_wrapper_paths
    with _prime_daemon(paths, tmp_path) as daemon:
        observed = _run_wrapper_signalled_at(paths, tmp_path, daemon, "prep-lock", signum)
        claim = read_owner_claim(paths.root)
        observed["owner_is_launcher"] = claim is not None and claim.pid == os.getpid()
        reservation = observed.pop("reservation")
        assert isinstance(reservation, dict)
        assert (reservation["state"], reservation["wrapper"]) == ("pending", None)
        assert observed == {
            "wrapper_returncode": -signum & 0xFF,
            "terminal_spawned": False,
            "terminal_alive": False,
            "terminal_record": False,
            "shutdown_sent": False,
            "owner_is_launcher": True,
        }

#!/usr/bin/env -S uv run --no-sync python

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pexpect
import psutil

FAULT = "PRIME_LAUNCH_ACK_LOST"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def gated_wrapper(directory: Path, command: list[str]) -> int:
    write_json(
        directory / "gate-ready.json",
        {
            "pid": os.getpid(),
            "created_at": psutil.Process().create_time(),
            "command": command,
            "ready_at": time.monotonic(),
        },
    )
    deadline = time.monotonic() + 90
    while not (directory / "release").exists():
        if time.monotonic() >= deadline:
            return 74
        time.sleep(0.05)
    write_json(
        directory / f"gate-released-{os.getpid()}.json",
        {"pid": os.getpid(), "released_at": time.monotonic()},
    )
    os.execv(command[0], command)
    return 74


def write_tmux_proxy(directory: Path, real_tmux: str, ordering: str) -> Path:
    shim_dir = directory / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(
        f"#!{sys.executable}\nimport runpy, sys\n"
        f"sys.argv[1:1] = {['--tmux-proxy', str(directory), real_tmux, ordering]!r}\n"
        f"runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')\n"
    )
    shim.chmod(0o700)
    return shim


def tmux_proxy(directory: Path, real_tmux: str, ordering: str, arguments: list[str]) -> int:
    matches = [
        index
        for index, argument in enumerate(arguments)
        if "omnigent.harnesses.prime_native.process" in argument
    ]
    if (
        "new-session" not in arguments
        or len(matches) != 1
        or (directory / "fault-disabled").exists()
    ):
        os.execv(real_tmux, [real_tmux, *arguments])
    try:
        (directory / "fault-selected").touch(exist_ok=False)
        first_fault = True
    except FileExistsError:
        first_fault = False
    command_index = matches[0]
    original_command = arguments[command_index]
    original_parts = shlex.split(original_command)
    module_index = original_parts.index("omnigent.harnesses.prime_native.process")
    runtime = Path(original_parts[module_index + 1])
    token = original_parts[module_index + 2]
    injected_error = f"{FAULT} attempt_sha256={hashlib.sha256(token.encode()).hexdigest()}"
    if ordering == "delayed":
        arguments[command_index] = shlex.join(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--gate",
                str(directory),
                "/bin/sh",
                "-c",
                original_command,
            ]
        )
    result = subprocess.run([real_tmux, *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        sys.stderr.write(result.stderr)
        return result.returncode
    target = directory / "gate-ready.json" if ordering == "delayed" else runtime / "terminal.json"
    deadline = time.monotonic() + 30
    while True:
        try:
            witness = json.loads(target.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            witness = None
        if isinstance(witness, dict) and (
            ordering == "claimed" or witness.get("command") == ["/bin/sh", "-c", original_command]
        ):
            break
        if time.monotonic() >= deadline:
            sys.stderr.write("Prime launch probe did not reach its requested ordering\n")
            return 74
        time.sleep(0.05)
    server = subprocess.run(
        [real_tmux, "-S", arguments[arguments.index("-S") + 1], "display-message", "-p", "#{pid}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    server_identity = None
    if server.returncode == 0 and server.stdout.strip().isdigit():
        process = psutil.Process(int(server.stdout.strip()))
        server_identity = {"pid": process.pid, "created_at": process.create_time()}
    witness = {
        "socket": arguments[arguments.index("-S") + 1],
        "runtime": str(runtime),
        "original_command": original_command,
        "tmux_returncode": result.returncode,
        "injected_returncode": 73,
        "error": injected_error,
        "launch_token": token,
        "runtime_at_fault": {
            name: (runtime / name).read_text()
            for name in ("launch.pending.json", "terminal.json", "owner.pid")
            if (runtime / name).exists()
        },
        "ordering": ordering,
        "order_witness": witness,
        "server_identity": server_identity,
        "fault_at": time.monotonic(),
    }
    if first_fault:
        write_json(directory / "backend-fault.json", witness)
    write_json(directory / f"backend-fault-{os.getpid()}.json", witness)
    sys.stdout.write(result.stdout)
    sys.stderr.write(injected_error + "\n")
    return 73


def identity_alive(identity: dict[str, object]) -> bool:
    try:
        process = psutil.Process(int(identity["pid"]))
        return (
            process.create_time() == identity["created_at"]
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def terminal_observation(real_tmux: str, fault: dict[str, object]) -> dict[str, object]:
    try:
        query = subprocess.run(
            [
                real_tmux,
                "-S",
                str(fault["socket"]),
                "list-panes",
                "-a",
                "-F",
                "#{pane_pid} #{pane_dead}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        identity = fault.get("server_identity")
        alive = identity_alive(identity) if isinstance(identity, dict) else None
    except (psutil.Error, OSError, SystemError, subprocess.SubprocessError) as exc:
        return {"closed": False, "inspection_error": f"{type(exc).__name__}: {exc}"}
    return {
        "returncode": query.returncode,
        "stdout": query.stdout,
        "stderr": query.stderr,
        "server_alive": alive,
        "closed": alive is False
        and query.returncode != 0
        and query.stderr.strip()
        in {
            f"no server running on {fault['socket']}",
            f"error connecting to {fault['socket']} (No such file or directory)",
        },
    }


def primary_launch_failures(log_root: Path) -> list[dict[str, str]]:
    errors = []
    for log in sorted(log_root.glob("runner/*.log")):
        contents = log.read_text(errors="replace")
        matches = list(
            re.finditer(
                r"^.*\| Native Prime Native terminal start failed; "
                r"error_id=(err_[a-f0-9]+): (.*)$",
                contents,
                re.MULTILINE,
            )
        )
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(contents)
            errors.append(
                {
                    "error_id": match.group(1),
                    "message": match.group(2),
                    "context": re.split(
                        r"(?m)^(?:DEBUG|INFO|WARN|ERROR|CRITICAL)\s",
                        contents[match.end() : end],
                        maxsplit=1,
                    )[0],
                    "log": str(log),
                }
            )
    return errors


def launch_error_observation(log_root: Path, injected_error: str) -> dict[str, object]:
    errors = primary_launch_failures(log_root)
    attributable = [
        error
        for error in errors
        if injected_error in error["message"] or injected_error in error["context"]
    ]
    original = attributable[0] if len(attributable) == 1 else None
    return {
        "original_error": original,
        "original_error_is_primary": original is not None
        and original["message"] == f"tmux launch failed (rc=73): {injected_error}",
        "later_errors": [error for error in errors if error is not original],
    }


def qualified_fault(fault: dict[str, object], ordering: str) -> bool:
    witness = fault.get("order_witness")
    identity = fault.get("server_identity")
    parts = shlex.split(str(fault.get("original_command", "")))
    try:
        module_index = parts.index("omnigent.harnesses.prime_native.process")
        token = parts[module_index + 2]
        if parts[module_index + 1] != fault.get("runtime") or not re.fullmatch(
            r"[a-f0-9]{32}", token
        ):
            return False
    except (ValueError, IndexError):
        return False
    if not isinstance(witness, dict) or not isinstance(identity, dict):
        return False
    if not isinstance(identity.get("pid"), int) or not isinstance(
        identity.get("created_at"), (float, int)
    ):
        return False
    if (
        fault.get("tmux_returncode") != 0
        or fault.get("injected_returncode") != 73
        or fault.get("error")
        != f"{FAULT} attempt_sha256={hashlib.sha256(token.encode()).hexdigest()}"
        or fault.get("launch_token") != token
        or fault.get("ordering") != ordering
    ):
        return False
    snapshot = fault.get("runtime_at_fault")
    if not isinstance(snapshot, dict):
        return False
    try:
        reservation = json.loads(snapshot["launch.pending.json"])
        if reservation.get("token") != token:
            return False
        if ordering == "claimed" and json.loads(snapshot["terminal.json"]) != witness:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    if ordering == "delayed":
        return (
            witness.get("command") == ["/bin/sh", "-c", fault.get("original_command")]
            and isinstance(witness.get("pid"), int)
            and isinstance(witness.get("created_at"), (float, int))
            and isinstance(witness.get("ready_at"), (float, int))
            and witness["ready_at"] <= fault["fault_at"]
        )
    return (
        isinstance(witness.get("pid"), int)
        and isinstance(witness.get("created_at"), (float, int))
        and witness.get("launch_token") == token
    )


def reconcile_claimed_launch(
    run: Any,
    adapter: Any,
    fault: dict,
    real_tmux: str,
    native_observations: dict,
    terminals: list[dict],
    recovery: dict,
) -> None:
    if len(run.owned_session_ids) != 1:
        raise RuntimeError("Recovery requires exactly one owned conversation")
    session_id = run.owned_session_ids[0]
    runtime = Path(fault["runtime"])
    with run.client() as client:
        baseline = client.get(f"/v1/sessions/{session_id}")
        baseline.raise_for_status()
        history = client.get(f"/v1/sessions/{session_id}/items")
        history.raise_for_status()
    recovery["baseline"] = {"session": baseline.json(), "items": history.json()}
    if baseline.json().get("id") != session_id:
        raise RuntimeError("Recovery baseline has a different conversation")
    recovery["stop_attempts"] = []
    deadline = time.monotonic() + 30
    while True:
        with run.client(timeout=10) as client:
            response = client.post(
                f"/v1/sessions/{session_id}/events", json={"type": "stop_session"}
            )
        run.evidence.action(
            "launch_reconciliation",
            "HTTP POST /v1/sessions/{id}/events",
            {"session_id": session_id, "type": "stop_session"},
            response,
        )
        recovery["stop_attempts"].append({"status": response.status_code, "body": response.text})
        for record in run.process_records():
            native_observations[(record.get("pid"), record.get("started"))] = record
        if response.is_success:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Public scoped stop did not reconcile the retained launch")
        time.sleep(0.25)

    def settled() -> bool:
        for record in run.process_records():
            native_observations[(record.get("pid"), record.get("started"))] = record
        recovery["after_stop"] = {
            "launch_pending": (runtime / "launch.pending.json").exists(),
            "terminal_record": (runtime / "terminal.json").exists(),
            "native_survivors": adapter.live_initial_process_records(
                list(native_observations.values())
            ),
            "terminals": [terminal_observation(real_tmux, item) for item in terminals],
        }
        state = recovery["after_stop"]
        return (
            not state["launch_pending"]
            and not state["terminal_record"]
            and not state["native_survivors"]
            and all(item["closed"] for item in state["terminals"])
        )

    adapter.wait_for(settled, "token and native absence after public stop", 10)
    with run.client() as client:
        preserved = client.get(f"/v1/sessions/{session_id}")
        preserved.raise_for_status()
        preserved_history = client.get(f"/v1/sessions/{session_id}/items")
        preserved_history.raise_for_status()
    recovery["preserved"] = {"session": preserved.json(), "items": preserved_history.json()}
    if preserved.json().get("id") != session_id or preserved_history.json() != history.json():
        raise RuntimeError("Scoped stop did not preserve the conversation and its history")
    (run.evidence.root / "fault-disabled").touch()
    argv = ["prime-native", "--server", str(run.url), "--resume", session_id]
    run.evidence.record_launch(
        "prime_native_cli_recovered",
        [str(adapter.REPO / ".venv/bin/omnigent"), *argv],
        run.workspace,
        run.env(),
    )
    terminal = pexpect.spawn(
        str(adapter.REPO / ".venv/bin/omnigent"),
        argv,
        cwd=str(run.workspace),
        env=run.env(),
        encoding="utf-8",
        timeout=90,
        dimensions=(36, 120),
    )
    run.evidence.record_launch_start("prime_native_cli_recovered", terminal.pid)
    handle = (run.evidence.root / "terminal-recovered.txt").open("w", encoding="utf-8")
    terminal.logfile_read = handle
    session = adapter.PrimeSession("recovered", session_id, terminal, handle)
    run.sessions.append(session)
    if adapter.expect_conversation_id(terminal) != session_id:
        raise RuntimeError("Recovery launched a different conversation")
    with run.client() as client:
        response = client.get(
            f"/v1/sessions/{session_id}/resources/terminals/terminal_prime-native_main"
        )
        response.raise_for_status()
    metadata = response.json()["metadata"]
    query = subprocess.run(
        [real_tmux, "-S", metadata["tmux_socket"], "display-message", "-p", "#{pid}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    server = psutil.Process(int(query.stdout.strip()))
    fresh_terminal = {
        "socket": metadata["tmux_socket"],
        "server_identity": {"pid": server.pid, "created_at": server.create_time()},
    }
    terminals.append(fresh_terminal)
    fresh_record = json.loads((runtime / "terminal.json").read_text())
    recovery["fresh_launch"] = {"terminal": fresh_terminal, "native": fresh_record}
    if (
        not fresh_record.get("launch_token")
        or fresh_record["launch_token"] == fault["launch_token"]
        or not identity_alive(fresh_record)
    ):
        raise RuntimeError("Recovery lacks a fresh live token-qualified native child")
    run.fixture.admission_release.set()
    prompt = f"ADAPTER_HTTP recovery-{run.nonce}"
    response = run.post_event("launch_recovery", session, adapter.message_action(prompt))
    if response.status_code != 202:
        raise RuntimeError("Recovered conversation did not admit the fresh message")
    recovery["completed_items"] = run.wait_for_reply(session, adapter.PONG_HTTP, prompt)
    run.wait_for_idle(session)
    recovery["verified"] = True


def main() -> int:
    import adapter_probe as adapter

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prime-path", type=Path, required=True)
    parser.add_argument("--kernel-python", type=Path, required=True)
    parser.add_argument("--ordering", choices=("delayed", "claimed"), required=True)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    args.expected_tool = None
    args.expected_mcp = None
    args.expected_side_effect = "absent"
    root = args.evidence_dir or Path(tempfile.mkdtemp(prefix="prime-launch-proof-", dir="/tmp"))
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise RuntimeError("The evidence directory must be empty")
    real_tmux = shutil.which("tmux")
    if real_tmux is None:
        raise RuntimeError("tmux is required")
    shim = write_tmux_proxy(root, real_tmux, args.ordering)

    class LaunchRun(adapter.AdapterRun):
        def env(self) -> dict[str, str]:
            result = super().env()
            result["PATH"] = str(shim.parent) + os.pathsep + result["PATH"]
            return result

    evidence = adapter.Evidence(root)
    evidence.scenarios.update(
        {name: adapter.Scenario() for name in ("launch_reconciliation", "launch_recovery")}
    )
    before = adapter.source_snapshot()
    write_json(root / "source-before.json", asdict(before))
    run = None
    safe = False
    observed: dict[str, object] = {"ordering": args.ordering}
    native_observations: dict[tuple[object, object], dict] = {}
    faults: list[dict] = []
    terminals: list[dict] = []
    cleanup_ok = False

    def sample_native() -> None:
        if run is not None:
            for record in run.process_records():
                native_observations[(record.get("pid"), record.get("started"))] = record

    try:
        artifact, kernel = adapter.source_identity(args, before)
        write_json(root / "artifact.json", artifact)
        run = LaunchRun(args, evidence, kernel)
        try:
            run.start()
        except pexpect.ExceptionPexpect as exc:
            observed["cli_failure"] = str(exc)
        else:
            raise RuntimeError("The CLI succeeded despite the injected launch failure")
        faults = [
            json.loads(path.read_text()) for path in sorted(root.glob("backend-fault-*.json"))
        ]
        terminals.extend(faults)
        if not faults or not all(qualified_fault(fault, args.ordering) for fault in faults):
            observed["target_fault"] = "not run"
            raise RuntimeError("The requested tmux failure and ordering were not qualified")
        observed["target_fault"] = "qualified"
        observed["faults"] = faults
        if args.ordering == "claimed":
            for fault in faults:
                child = fault["order_witness"]
                native_observations[(child["pid"], child["created_at"])] = {
                    "pid": child["pid"],
                    "started": child["created_at"],
                    "ownership": "fault_child",
                }
        first_fault = json.loads((root / "backend-fault.json").read_text())
        observed.update(
            launch_error_observation(run.runtime / "data" / "logs", first_fault["error"])
        )
        observed["injected_attempt_errors"] = [
            {
                "token": fault["launch_token"],
                **launch_error_observation(run.runtime / "data" / "logs", fault["error"]),
            }
            for fault in faults
        ]
        for fault in faults:
            paths = Path(fault["runtime"])
            fault["before_release"] = {
                name: (paths / name).read_text()
                for name in ("launch.pending.json", "terminal.json", "owner.pid")
                if (paths / name).exists()
            }
        sample_native()
        observed["release_at"] = time.monotonic()
        (root / "release").touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            sample_native()
            time.sleep(0.1)
        observed["terminals_after_failure"] = [
            terminal_observation(real_tmux, fault) for fault in faults
        ]
        observed["gates_settled"] = args.ordering != "delayed" or all(
            not identity_alive(fault["order_witness"]) for fault in faults
        )
        authority = []
        for fault in faults:
            reservation_path = Path(fault["runtime"]) / "launch.pending.json"
            reservation = (
                json.loads(reservation_path.read_text()) if reservation_path.exists() else None
            )
            authority.append(reservation)
        observed["launch_authority"] = authority
        observed["authority_settled"] = all(
            record is None
            or (
                record.get("protocol") == 1
                and record.get("state") == "revoked"
                and record.get("wrapper") is None
            )
            for record in authority
        )
        observed["recovery_outcome"] = (
            "settled" if observed["authority_settled"] else "retained_unknown"
        )
        if args.ordering == "claimed":
            recovery = {"verified": False}
            observed["recovery"] = recovery
            reconcile_claimed_launch(
                run, adapter, first_fault, real_tmux, native_observations, terminals, recovery
            )
        safe = (
            bool(observed["original_error_is_primary"])
            and all(
                attempt["original_error_is_primary"]
                for attempt in observed["injected_attempt_errors"]
            )
            and bool(observed["gates_settled"])
            and (
                bool(observed["authority_settled"])
                if args.ordering == "delayed"
                else observed.get("recovery", {}).get("verified") is True
            )
            and (
                args.ordering == "claimed"
                or all(terminal["closed"] for terminal in observed["terminals_after_failure"])
            )
            and (args.ordering == "claimed" or not native_observations)
        )
    except adapter.FINALIZATION_ERRORS as exc:
        observed["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        (root / "release").touch()
        if run is not None:
            try:
                sample_native()
                cleanup_ok, cleanup = run.cleanup()
                for key in ("survivors_before_force", "owned_survivors"):
                    for record in cleanup.get(key, []):
                        native_observations[(record.get("pid"), record.get("started"))] = record
                observed["forced_native_fallback"] = cleanup.get("forced_native_fallback", True)
            except adapter.FINALIZATION_ERRORS as exc:
                observed["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
        fallback = []
        for fault in terminals:
            terminal = terminal_observation(real_tmux, fault)
            if not terminal["closed"]:
                try:
                    result = subprocess.run(
                        [real_tmux, "-S", fault["socket"], "kill-server"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                    fallback.append(
                        {
                            "socket": fault["socket"],
                            "returncode": result.returncode,
                            "stderr": result.stderr,
                        }
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    fallback.append(
                        {"socket": fault["socket"], "error": f"{type(exc).__name__}: {exc}"}
                    )
        observed["exact_socket_fallback"] = fallback
        observed["final_terminals"] = [
            terminal_observation(real_tmux, fault) for fault in terminals
        ]
        observed["observed_native_processes"] = list(native_observations.values())
        observed["final_owned_census"] = run.own_data_process_records() if run else []
        after = adapter.source_snapshot()
        try:
            adapter.assert_same_sources(before, after)
        except adapter.SourceIntegrityError as exc:
            safe = False
            observed["source_integrity_error"] = str(exc)
        write_json(root / "source-after.json", asdict(after))
        cleanup_ok = (
            cleanup_ok
            and not fallback
            and observed.get("forced_native_fallback") is False
            and not observed["final_owned_census"]
            and all(item["closed"] for item in observed["final_terminals"])
        )
        safe = safe and (args.ordering == "claimed" or not native_observations)
        observed["safe"] = safe
        observed["cleanup_ok"] = cleanup_ok
        write_json(root / "observation.json", observed)
        write_json(
            root / "manifest.json", {"safe": safe, "cleanup_ok": cleanup_ok, "evidence": str(root)}
        )
        print(("PASS" if safe and cleanup_ok else "FAIL") + " launch recovery", flush=True)
        print(f"Evidence {root}", flush=True)
    return 0 if safe and cleanup_ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--tmux-proxy":
        raise SystemExit(tmux_proxy(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--gate":
        raise SystemExit(gated_wrapper(Path(sys.argv[2]), sys.argv[3:]))
    raise SystemExit(main())

#!/usr/bin/env -S uv run --no-sync python
"""Probe a published Prime daemon through an isolated public socket and native TUI."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pexpect
import psutil
from verify import wait_for

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = (
    "negotiation",
    "native_attachment",
    "python_memory",
    "completed_duplicate",
    "lost_reply",
    "cursor_reconnect",
    "generation_gap",
    "supervisor_adoption",
    "runtime_replacement",
    "worker_recovery",
    "owned_cleanup",
)


@dataclass
class Operation:
    identity: str
    admission: str = "Unsent"
    execution: str = "Unknown"
    response: dict | None = None


@dataclass
class RuntimeReceipt:
    configuration: str
    socket_path: str
    workspace: str
    client_id: str
    active_session_id: str | None = None
    public_identity: dict | None = None
    process_lifetimes: list | None = None


def save(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def append(path: Path, data: object) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(data) + "\n")


def owned_processes(configuration: Path) -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter():
        try:
            if process.pid != os.getpid() and process.environ().get(
                "PRIME_AGENT_CODING_AGENT_DIR"
            ) == str(configuration):
                found.append(process)
        except (psutil.Error, OSError, SystemError):
            continue
    return found


def lifetimes(configuration: Path) -> list[dict]:
    result = []
    for process in owned_processes(configuration):
        with contextlib.suppress(psutil.Error):
            result.append(
                {
                    "pid": process.pid,
                    "started": process.create_time(),
                    "argv": process.cmdline(),
                    "status": process.status(),
                }
            )
    return result


class Peer:
    def __init__(self, path: Path, evidence: Path, client_id: str):
        self.evidence = evidence
        self.client_id = client_id
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(30)
        self.socket.connect(str(path))
        self.buffer = b""
        self.records = []
        self.hello = self.receive()
        if self.hello.get("type") != "daemon_hello":
            raise RuntimeError(f"Expected daemon_hello, received {self.hello}")
        self.protocol = self.hello["protocol"]
        required = {"attach_snapshot", "event_sequence", "session_input_admission"}
        if (
            self.protocol != {"name": "prime-agent.daemon", "version": 7}
            or self.hello.get("schemaRevision") != 30
            or self.hello.get("schemaId") != "protocol-7-schema-30-f908f493c9e1"
            or self.hello.get("appVersion") != "0.9.6"
            or self.hello.get("runtime", {}).get("buildId")
            != "e260085dd8f742e0def3d871860c9a888b114851"
            or not required <= set(self.hello.get("serverCapabilities", []))
        ):
            self.close()
            raise RuntimeError(
                "Daemon does not match the pinned protocol, schema, and capabilities"
            )

    def receive(self, timeout: float = 30) -> dict:
        self.socket.settimeout(timeout)
        while b"\n" not in self.buffer:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("Public daemon socket closed")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        record = json.loads(line)
        self.records.append(record)
        append(self.evidence / "wire.jsonl", {"direction": "received", "record": record})
        return record

    def send(self, command: dict, operation: Operation | None = None) -> Operation:
        operation = operation or Operation(uuid.uuid4().hex)
        envelope = {
            "type": "command",
            "id": operation.identity,
            "protocol": self.protocol,
            "clientId": self.client_id,
            "command": command,
        }
        append(self.evidence / "wire.jsonl", {"direction": "sent", "record": envelope})
        operation.admission = "Uncertain"
        self.socket.sendall(json.dumps(envelope).encode() + b"\n")
        return operation

    def request(self, command: dict, operation: Operation | None = None) -> dict:
        operation = self.send(command, operation)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            record = self.receive(max(0.1, deadline - time.monotonic()))
            if record.get("type") == "response" and record.get("id") == operation.identity:
                operation.response = record
                operation.admission = "Accepted" if record.get("success") else "Rejected"
                if record.get("errorInfo", {}).get("code") == "command_result_uncertain":
                    operation.admission = "Uncertain"
                append(self.evidence / "operations.jsonl", asdict(operation))
                return record
        raise TimeoutError("No command result before deadline")

    def close(self):
        self.socket.close()


def require_success(response: dict) -> dict:
    if response.get("success") is not True:
        raise RuntimeError(f"Public command rejected: {response}")
    return response.get("data", {})


def owned_process(runtime: Runtime, pid: int) -> psutil.Process:
    matches = [process for process in owned_processes(runtime.config) if process.pid == pid]
    if len(matches) != 1:
        raise RuntimeError(f"PID {pid} is not an independently observed owned process")
    return matches[0]


def same_identity(snapshot: dict, root: dict) -> None:
    summary = snapshot["snapshot"]["summary"]
    state = snapshot["snapshot"]["state"]
    for field in ("activeSessionId", "sessionId", "sessionFile", "workerPid"):
        if summary.get(field) != root.get(field) or field not in root:
            raise RuntimeError(f"Continuity identity changed or is absent: {field}")
    if state["sessionId"] != root["sessionId"] or state["sessionFile"] != root["sessionFile"]:
        raise RuntimeError("Snapshot state and root summary disagree")


class Fixture:
    def __init__(self, evidence: Path, nonce: str, workspace: Path):
        self.evidence = evidence
        self.calls = []
        self.errors = []
        self.completed = []
        self.codes = {
            "SEED": (
                "import socket\n"
                f"prime_probe_value = {{'token': '{nonce}', 'count': 0, "
                "'socket': socket.socket()}\n"
                "print(prime_probe_value['token'], prime_probe_value['count'])"
            ),
            "READ": "print(prime_probe_value['token'], prime_probe_value['count'])",
            "MUTATE": (
                "prime_probe_value['count'] += 1\nfrom pathlib import Path\n"
                f"with Path({str(workspace / 'mutations.txt')!r}).open('a') as f:\n"
                "    f.write('mutation\\n')\n"
                "print(prime_probe_value['token'], prime_probe_value['count'])"
            ),
            "LOST": (
                "prime_probe_value['count'] += 1\nfrom pathlib import Path\n"
                f"with Path({str(workspace / 'lost.txt')!r}).open('a') as f:\n"
                "    f.write('lost\\n')\n"
                "print(prime_probe_value['token'], prime_probe_value['count'])"
            ),
            "RECOVERED": (
                "print('PRIME_MEMORY_PRESENT' if 'prime_probe_value' in globals() "
                "else 'PRIME_MEMORY_ABSENT')"
            ),
            "UNCERTAIN": (
                "from pathlib import Path\nimport time\n"
                f"with Path({str(workspace / 'uncertain.txt')!r}).open('a') as f:\n"
                "    f.write('uncertain\\n')\ntime.sleep(8)\nprint('PRIME_UNCERTAIN_FINISHED')"
            ),
        }
        self.expected = {
            "SEED": f"{nonce} 0",
            "READ": f"{nonce} 0",
            "MUTATE": f"{nonce} 1",
            "LOST": f"{nonce} 2",
            "RECOVERED": "PRIME_MEMORY_ABSENT",
            "UNCERTAIN": "PRIME_UNCERTAIN_FINISHED",
        }
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                append(evidence / "model-requests.jsonl", request)
                try:
                    messages = request["messages"]
                    index = max(i for i, value in enumerate(messages) if value["role"] == "user")
                    prompt = json.dumps(messages[index]["content"])
                    scenario = next(name for name in fixture.codes if f"PROBE_{name}" in prompt)
                    tools = [value for value in messages[index + 1 :] if value["role"] == "tool"]
                    if tools:
                        actual = tools[-1]["content"]
                        expected = fixture.expected[scenario]
                        if not isinstance(actual, str) or actual.strip() != expected:
                            raise RuntimeError(
                                f"{scenario} tool output lacks {expected!r}: {actual}"
                            )
                        fixture.completed.append(scenario)
                        delta = {"role": "assistant", "content": f"PROBE_DONE_{scenario}"}
                        reason = "stop"
                    else:
                        schemas = [value["function"] for value in request["tools"]]
                        ipython = next(value for value in schemas if value["name"] == "ipython")
                        if "code" not in ipython["parameters"]["properties"]:
                            raise RuntimeError(f"Unsupported ipython tool shape: {ipython}")
                        fixture.calls.append(scenario)
                        delta = {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"probe_{uuid.uuid4().hex}",
                                    "type": "function",
                                    "function": {
                                        "name": "ipython",
                                        "arguments": json.dumps({"code": fixture.codes[scenario]}),
                                    },
                                }
                            ],
                        }
                        reason = "tool_calls"
                except (RuntimeError, ValueError, KeyError, TypeError, StopIteration) as exc:
                    fixture.errors.append(str(exc))
                    append(evidence / "fixture-errors.jsonl", {"error": str(exc)})
                    delta = {"role": "assistant", "content": "PROBE_FIXTURE_FAILED"}
                    reason = "stop"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for value, end in ((delta, None), ({}, reason)):
                        chunk = {
                            "id": "probe",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "fixture",
                            "choices": [{"index": 0, "delta": value, "finish_reason": end}],
                        }
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, scenario: str, count: int = 1):
        def ready():
            if self.errors:
                raise RuntimeError(self.errors[-1])
            return self.completed.count(scenario) >= count

        wait_for(ready, f"literal {scenario} tool result", 240)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Runtime:
    def __init__(self, prime: str, directory: Path, evidence: Path, endpoint: int, kernel: Path):
        self.prime, self.directory, self.evidence = prime, directory, evidence
        self.config = directory / "config"
        self.workspace = directory / "workspace"
        self.socket = directory / "daemon.sock"
        for path in (self.config, self.workspace, directory / "tmp", evidence):
            path.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ)
        self.env.update(
            {
                "PRIME_AGENT_CODING_AGENT_DIR": str(self.config),
                "TMPDIR": str(directory / "tmp"),
                "TERM": "xterm-256color",
                "BROWSER": "false",
            }
        )
        for key in tuple(self.env):
            if key.startswith("PRIME_AGENT_") and key != "PRIME_AGENT_CODING_AGENT_DIR":
                self.env.pop(key)
        self.env["PRIME_AGENT_KERNEL_PYTHON"] = str(kernel)
        save(
            self.config / "settings.json",
            {
                "onboardingShown": True,
                "defaultProvider": "verify",
                "defaultModel": "fixture",
            },
        )
        save(
            self.config / "models.json",
            {
                "providers": {
                    "verify": {
                        "baseUrl": f"http://127.0.0.1:{endpoint}/v1",
                        "api": "openai-completions",
                        "apiKey": "verification-fixture",
                        "models": [
                            {
                                "id": "fixture",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 32768,
                                "maxTokens": 1024,
                            }
                        ],
                    }
                }
            },
        )
        self.terminal = None
        self.thread = None
        self.receipt = RuntimeReceipt(
            str(self.config), str(self.socket), str(self.workspace), uuid.uuid4().hex
        )

    def launch(self, attach: str | None = None):
        argv = ["--daemon-socket", str(self.socket)]
        argv += [
            "--offline",
            "--provider",
            "verify",
            "--model",
            "fixture",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
        ]
        if attach:
            argv += ["--resume", attach]
        append(
            self.evidence / "launch.jsonl",
            {"argv": [self.prime, *argv], "cwd": str(self.workspace), "config": str(self.config)},
        )
        self.terminal = pexpect.spawn(
            self.prime,
            argv,
            cwd=str(self.workspace),
            env=self.env,
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(36, 120),
        )
        terminal = self.terminal
        ready = threading.Event()

        def drain():
            with (self.evidence / "terminal.txt").open("a") as handle:
                while terminal.isalive():
                    try:
                        chunk = terminal.read_nonblocking(65536, timeout=0.2)
                        handle.write(chunk)
                        handle.flush()
                        if "\x1b]0;prime-agent" in chunk:
                            ready.set()
                    except pexpect.TIMEOUT:
                        continue
                    except (pexpect.EOF, OSError):
                        return

        self.thread = threading.Thread(target=drain, daemon=True)
        self.thread.start()
        wait_for(self.socket.exists, "isolated public daemon socket", 30)
        wait_for(ready.is_set, "native terminal initial render", 30)

    def connect(self) -> Peer:
        return Peer(self.socket, self.evidence, self.receipt.client_id)

    def reconnect(self) -> Peer:
        def available():
            try:
                return self.connect()
            except (OSError, ConnectionError):
                return None

        return wait_for(available, "replacement public supervisor", 30)

    def detach(self):
        if self.terminal is not None:
            self.terminal.close(force=True)
        if self.thread is not None:
            self.thread.join(3)

    def cleanup(self) -> dict:
        self.detach()
        before = lifetimes(self.config)
        try:
            with contextlib.closing(self.connect()) as peer:
                result = peer.request({"type": "shutdown", "force": True})
            save(self.evidence / "shutdown.json", result)
        except (OSError, RuntimeError) as exc:
            save(self.evidence / "shutdown.json", {"error": str(exc)})
        deadline = time.monotonic() + 10
        while owned_processes(self.config) and time.monotonic() < deadline:
            time.sleep(0.2)
        after = lifetimes(self.config)
        for process in owned_processes(self.config):
            with contextlib.suppress(psutil.Error):
                process.terminate()
        _, alive = psutil.wait_procs(owned_processes(self.config), timeout=3)
        for process in alive:
            with contextlib.suppress(psutil.Error):
                process.kill()
        result = {
            "before": before,
            "after_public_shutdown": after,
            "final_survivors": lifetimes(self.config),
        }
        save(self.evidence / "cleanup.json", result)
        return result


def drive(runtime: Runtime, fixture: Fixture, outcomes: dict, expected: str) -> None:
    runtime.launch()
    peer = runtime.connect()
    save(runtime.evidence / "hello.json", peer.hello)
    outcomes["negotiation"] = {"verdict": "VERIFIED", "protocol": peer.hello}
    listing = wait_for(
        lambda: (
            data
            if (data := require_success(peer.request({"type": "list"}))).get("sessions")
            else None
        ),
        "resident root listing",
        180,
    )
    save(runtime.evidence / "list.json", listing)
    root = listing["sessions"][0]
    runtime.receipt.active_session_id = root["activeSessionId"]
    runtime.receipt.public_identity = root
    runtime.receipt.process_lifetimes = lifetimes(runtime.config)
    save(runtime.evidence / "receipt-before.json", asdict(runtime.receipt))
    active = root["activeSessionId"]
    attach_command = {
        "type": "attach",
        "activeSessionId": active,
        "supportsExtensionUi": False,
        "clientId": runtime.receipt.client_id,
        "capabilities": ["attach_snapshot", "event_sequence"],
    }
    attached = require_success(peer.request(attach_command))
    save(runtime.evidence / "attached.json", attached)
    same_identity(attached, root)
    if attached["snapshot"]["summary"]["attachedClients"] < 2:
        raise RuntimeError("Probe and native terminal are not attached concurrently")
    worker_started = owned_process(runtime, root["workerPid"]).create_time()
    outcomes["native_attachment"] = {
        "verdict": "VERIFIED",
        "evidence": "owned/receipt-before.json and attached.json",
    }
    require_success(
        peer.request({"type": "prompt", "activeSessionId": active, "message": "PROBE_SEED"})
    )
    fixture.wait("SEED")
    runtime.terminal.send("PROBE_READ\r")
    fixture.wait("READ")
    kernel_before = [value for value in lifetimes(runtime.config) if "rlm.repl" in value["argv"]]
    if not kernel_before:
        raise RuntimeError("No live Prime Python kernel was independently observed")
    runtime.detach()
    runtime.launch(root["sessionFile"])
    resumed = require_success(peer.request(attach_command))
    same_identity(resumed, root)
    if owned_process(runtime, root["workerPid"]).create_time() != worker_started:
        raise RuntimeError("Native reattach changed the worker lifetime")
    runtime.terminal.send("PROBE_READ\r")
    fixture.wait("READ", 2)
    kernel_after = [value for value in lifetimes(runtime.config) if "rlm.repl" in value["argv"]]
    if [(value["pid"], value["started"]) for value in kernel_before] != [
        (value["pid"], value["started"]) for value in kernel_after
    ]:
        raise RuntimeError("Native reattach changed the Python kernel lifetime")
    save(
        runtime.evidence / "native-reattach.json",
        {
            "snapshot": resumed,
            "processes": lifetimes(runtime.config),
            "kernel_before": kernel_before,
            "kernel_after": kernel_after,
        },
    )
    outcomes["python_memory"] = {"verdict": "VERIFIED", "evidence": "model-requests.jsonl"}
    command = {"type": "prompt_and_wait", "activeSessionId": active, "message": "PROBE_MUTATE"}
    operation = Operation(uuid.uuid4().hex)
    response = peer.request(command, operation)
    require_success(response)
    fixture.wait("MUTATE")
    repeated = peer.request(command, operation)
    if repeated != response:
        raise RuntimeError("Repeated completed command returned a different result")
    barrier = require_success(peer.request(attach_command))
    save(runtime.evidence / "duplicate-snapshot.json", barrier)
    if (runtime.workspace / "mutations.txt").read_text() != expected or fixture.calls.count(
        "MUTATE"
    ) != 1:
        raise RuntimeError("Completed duplicate command changed the literal side effect")
    operation.execution = "Succeeded"
    append(runtime.evidence / "operations.jsonl", asdict(operation))
    outcomes["completed_duplicate"] = {
        "verdict": "VERIFIED",
        "evidence": "owned/operations.jsonl and mutations.txt",
    }
    command = {"type": "prompt_and_wait", "activeSessionId": active, "message": "PROBE_LOST"}
    lost = peer.send(command)
    peer.close()
    append(runtime.evidence / "operations.jsonl", asdict(lost))
    fixture.wait("LOST")
    peer = runtime.connect()
    require_success(peer.request(attach_command))
    reply = peer.request(command, lost)
    require_success(reply)
    if (runtime.workspace / "lost.txt").read_text() != "lost\n" or fixture.calls.count(
        "LOST"
    ) != 1:
        raise RuntimeError("Lost-reply reconciliation duplicated the side effect")
    lost.execution = "Succeeded"
    append(runtime.evidence / "operations.jsonl", asdict(lost))
    outcomes["lost_reply"] = {
        "verdict": "VERIFIED",
        "evidence": "owned/operations.jsonl",
        "uncertain_outcome": "Closed after send. Same ID reconciled after literal completion.",
    }
    snapshot = require_success(peer.request(attach_command))
    save(runtime.evidence / "final-snapshot.json", snapshot)
    same_identity(snapshot, root)
    cursor = snapshot["lastEventCursor"]
    peer.close()
    peer = runtime.connect()
    resumed = require_success(
        peer.request({**attach_command, "resumeCursor": {"activeSessionId": active, **cursor}})
    )
    same_identity(resumed, root)
    save(runtime.evidence / "cursor-reconnect.json", resumed)
    if resumed["replay"]["status"] != "complete" or resumed["lastEventCursor"] != cursor:
        raise RuntimeError("Same-cursor reconnect did not produce a coherent baseline")
    outcomes["cursor_reconnect"] = {
        "verdict": "INCONCLUSIVE",
        "evidence": "owned/cursor-reconnect.json",
        "reason": "Snapshot reconnect succeeded. Cursor handling needs negative controls.",
    }
    gaps = []
    gap_errors = []
    for resume, reason in (
        ({**cursor, "sequence": cursor["sequence"] + 1000}, "resume_cursor_ahead_of_session"),
        ({**cursor, "generation": "probe-retired-generation"}, "event_generation_changed"),
        (attached["lastEventCursor"], "event_replay_not_available"),
    ):
        gap = require_success(
            peer.request({**attach_command, "resumeCursor": {"activeSessionId": active, **resume}})
        )
        same_identity(gap, root)
        if gap["replay"]["status"] != "unavailable" or gap["replay"].get("reason") != reason:
            gap_errors.append({"expected": reason, "observed": gap["replay"]})
        if gap["snapshot"]["messages"] != resumed["snapshot"]["messages"]:
            raise RuntimeError("Gap snapshot disagrees with the coherent reconnect baseline")
        gaps.append(gap)
    save(runtime.evidence / "cursor-gaps.json", gaps)
    outcomes["generation_gap"] = {
        "verdict": "NOT VERIFIED" if gap_errors else "VERIFIED",
        "evidence": "owned/cursor-gaps.json",
        "errors": gap_errors,
        "continuity": "Unknown after a gap. There is no public replacement epoch.",
    }
    outcomes["cursor_reconnect"]["verdict"] = "NOT VERIFIED" if gap_errors else "VERIFIED"
    outcomes["cursor_reconnect"]["reason"] = (
        "Snapshot reconnect succeeds, but invalid cursors also report complete."
        if gap_errors
        else "Same-cursor reconnect and invalid-cursor controls agree."
    )

    command = {"type": "prompt_and_wait", "activeSessionId": active, "message": "PROBE_UNCERTAIN"}
    uncertain = peer.send(command)
    wait_for((runtime.workspace / "uncertain.txt").exists, "in-flight kernel side effect", 30)
    supervisor = owned_process(runtime, peer.hello["supervisorPid"])
    before_adoption = {
        "hello": peer.hello,
        "processes": lifetimes(runtime.config),
        "operation": asdict(uncertain),
    }
    save(runtime.evidence / "adoption-before.json", before_adoption)
    supervisor.kill()
    supervisor.wait(10)
    peer.close()
    peer = runtime.reconnect()
    if peer.hello["supervisorGeneration"] == before_adoption["hello"]["supervisorGeneration"]:
        raise RuntimeError("Supervisor did not change after the owned crash")
    adopted = require_success(peer.request(attach_command))
    same_identity(adopted, root)
    if owned_process(runtime, root["workerPid"]).create_time() != worker_started:
        raise RuntimeError("Supervisor adoption restarted the worker")
    result = peer.request(command, uncertain)
    if result.get("errorInfo", {}).get("code") != "command_result_uncertain":
        raise RuntimeError("In-flight command did not remain uncertain after supervisor loss")
    fixture.wait("UNCERTAIN")
    if (runtime.workspace / "uncertain.txt").read_text() != "uncertain\n" or fixture.calls.count(
        "UNCERTAIN"
    ) != 1:
        raise RuntimeError("Uncertain command was replayed")
    fixture.expected["READ"] = fixture.expected["LOST"]
    require_success(
        peer.request(
            {"type": "prompt_and_wait", "activeSessionId": active, "message": "PROBE_READ"}
        )
    )
    fixture.wait("READ", 3)
    save(
        runtime.evidence / "adoption-after.json",
        {
            "hello": peer.hello,
            "snapshot": adopted,
            "processes": lifetimes(runtime.config),
            "operation": asdict(uncertain),
        },
    )
    outcomes["supervisor_adoption"] = {
        "verdict": "VERIFIED",
        "evidence": "owned/adoption-before.json and adoption-after.json",
        "uncertain": "Same operation remains Uncertain. Execution observed without replay.",
    }

    before_replace = require_success(peer.request(attach_command))
    replaced = require_success(peer.request({"type": "new_session", "activeSessionId": active}))
    if replaced.get("cancelled") is not False:
        raise RuntimeError("New session was cancelled")
    replacement = require_success(peer.request(attach_command))
    state = replacement["snapshot"]["state"]
    if replacement["activeSessionId"] != active or state["sessionId"] == root["sessionId"]:
        raise RuntimeError("Runtime replacement did not expose a changed saved-session identity")
    records = [record for record in peer.records if record.get("type") == "session_replaced"]
    if not records or records[-1]["state"]["sessionId"] != state["sessionId"]:
        raise RuntimeError("Runtime replacement lacked a correlated public replacement event")
    require_success(
        peer.request(
            {"type": "prompt_and_wait", "activeSessionId": active, "message": "PROBE_RECOVERED"}
        )
    )
    fixture.wait("RECOVERED")
    save(
        runtime.evidence / "runtime-replacement.json",
        {"before": before_replace, "after": replacement, "events": records},
    )
    outcomes["runtime_replacement"] = {
        "verdict": "VERIFIED",
        "evidence": "owned/runtime-replacement.json",
    }
    root = replacement["snapshot"]["summary"]
    before_recovery = require_success(peer.request(attach_command))
    old_generation = before_recovery["lastEventCursor"]["generation"]
    owned_process(runtime, root["workerPid"]).kill()

    def recovered():
        values = require_success(peer.request({"type": "list"}))["sessions"]
        return next(
            (
                value
                for value in values
                if value.get("activeSessionId") == active
                and value.get("workerState") == "ready"
                and value.get("workerPid") != root["workerPid"]
            ),
            None,
        )

    try:
        recovered_root = wait_for(recovered, "replacement resident worker", 30)
    except RuntimeError as exc:
        failed = peer.request({"type": "list"})
        save(
            runtime.evidence / "worker-recovery.json",
            {
                "before": before_recovery,
                "after": failed,
                "processes": lifetimes(runtime.config),
                "error": str(exc),
            },
        )
        outcomes["worker_recovery"] = {
            "verdict": "NOT VERIFIED",
            "evidence": "owned/worker-recovery.json",
            "reason": str(exc),
            "continuity": "Original worker was killed. Kernel continuity is Unknown.",
        }
        peer.close()
        return
    recovered_snapshot = require_success(
        peer.request(
            {
                **attach_command,
                "resumeCursor": {"activeSessionId": active, **before_recovery["lastEventCursor"]},
            }
        )
    )
    same_identity(recovered_snapshot, recovered_root)
    if (
        recovered_root["sessionId"] != root["sessionId"]
        or recovered_snapshot["lastEventCursor"]["generation"] == old_generation
    ):
        raise RuntimeError("Worker recovery did not preserve saved session and change generation")
    save(
        runtime.evidence / "worker-recovery.json",
        {
            "before": before_recovery,
            "after": recovered_snapshot,
            "processes": lifetimes(runtime.config),
        },
    )
    outcomes["worker_recovery"] = {
        "verdict": "VERIFIED",
        "evidence": "owned/worker-recovery.json",
        "continuity": "Worker lifetime lost. Saved history does not prove original Python memory.",
    }
    save(
        runtime.evidence / "receipt-after.json",
        {
            "root": require_success(peer.request({"type": "list"})),
            "processes": lifetimes(runtime.config),
        },
    )
    peer.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prime-path", required=True)
    parser.add_argument("--kernel-python", type=Path, required=True)
    parser.add_argument("--expected", default="mutation\n")
    parser.add_argument(
        "--expected-tool", help="Override the seed tool result for a negative control"
    )
    args = parser.parse_args()
    evidence = Path(tempfile.mkdtemp(prefix="prime-daemon-proof-", dir="/tmp"))
    directory = Path(tempfile.mkdtemp(prefix="pdp-", dir="/tmp"))
    prime = shutil.which(args.prime_path)
    if not prime:
        raise SystemExit("Prime binary not found")
    fixture = Fixture(evidence, uuid.uuid4().hex, directory / "owned" / "workspace")
    if args.expected_tool is not None:
        fixture.expected["SEED"] = args.expected_tool
    runtime = Runtime(
        prime,
        directory / "owned",
        evidence / "owned",
        fixture.server.server_port,
        args.kernel_python.absolute(),
    )
    unrelated = Runtime(
        prime,
        directory / "unrelated",
        evidence / "unrelated",
        fixture.server.server_port,
        args.kernel_python.absolute(),
    )
    outcomes = {
        name: {"verdict": "INCONCLUSIVE", "reason": "Scenario not reached"} for name in SCENARIOS
    }
    status = 1
    unrelated_before = None
    try:
        import omnigent

        version = subprocess.check_output([prime, "--version"], text=True).strip()
        save(
            evidence / "artifact.json",
            {
                "binary": prime,
                "sha256": hashlib.sha256(Path(prime).read_bytes()).hexdigest(),
                "version": version,
                "repo": str(REPO),
                "source_import": omnigent.__file__,
                "revision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
                ).strip(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "kernel_python": str(args.kernel_python.absolute()),
            },
        )
        if version != "0.9.6" or not Path(omnigent.__file__).resolve().is_relative_to(REPO):
            raise RuntimeError(
                "Runtime version or source import does not match the qualification target"
            )
        unrelated.launch()
        with contextlib.closing(unrelated.connect()) as sentinel:
            unrelated_before = sentinel.hello
            save(
                evidence / "unrelated-before.json",
                {"hello": sentinel.hello, "processes": lifetimes(unrelated.config)},
            )
        drive(runtime, fixture, outcomes, args.expected)
        status = 0
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        psutil.Error,
        subprocess.SubprocessError,
        pexpect.ExceptionPexpect,
    ) as exc:
        save(evidence / "failure.json", {"type": type(exc).__name__, "error": str(exc)})
        for name, result in outcomes.items():
            if result["verdict"] == "INCONCLUSIVE":
                outcomes[name] = {"verdict": "NOT VERIFIED", "reason": str(exc)}
                break
        print(f"FAIL {exc}", flush=True)
    finally:
        try:
            cleanup = runtime.cleanup()
            with contextlib.closing(unrelated.connect()) as sentinel:
                survivor = {
                    "hello": sentinel.hello,
                    "list": sentinel.request({"type": "list"}),
                    "processes": lifetimes(unrelated.config),
                }
            save(evidence / "unrelated-after.json", survivor)
            cleanup_ok = (
                not cleanup["after_public_shutdown"]
                and not cleanup["final_survivors"]
                and survivor["list"].get("success") is True
                and unrelated_before is not None
                and all(
                    survivor["hello"].get(field) == unrelated_before.get(field)
                    for field in (
                        "supervisorPid",
                        "supervisorGeneration",
                        "supervisorProcessStartId",
                    )
                )
            )
            outcomes["owned_cleanup"] = {
                "verdict": "VERIFIED" if cleanup_ok else "NOT VERIFIED",
                "evidence": "owned/cleanup.json and unrelated-after.json",
            }
            if not cleanup_ok:
                status = 1
        except (RuntimeError, OSError, ValueError, KeyError, psutil.Error) as exc:
            outcomes["owned_cleanup"] = {"verdict": "NOT VERIFIED", "reason": str(exc)}
            status = 1
        try:
            unrelated.cleanup()
        finally:
            fixture.close()
        for source in runtime.workspace.glob("*.txt"):
            shutil.copy2(source, evidence / source.name)
        for instance in (runtime, unrelated):
            logs = instance.config / "logs"
            if logs.is_dir():
                shutil.copytree(logs, instance.evidence / "logs")
        save(evidence / "outcomes.json", outcomes)
        if any(value["verdict"] != "VERIFIED" for value in outcomes.values()):
            status = 1
        artifacts = {
            str(path.relative_to(evidence)): {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in evidence.rglob("*")
            if path.is_file()
        }
        save(
            evidence / "manifest.json",
            {
                "exit": status,
                "evidence": str(evidence),
                "runtime": str(directory),
                "runtime_retained": True,
                "artifacts": artifacts,
            },
        )
        for name, result in outcomes.items():
            print(f"{result['verdict']} {name}", flush=True)
        print(f"Evidence {evidence}", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())

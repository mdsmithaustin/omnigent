#!/usr/bin/env -S uv run --no-sync python
"""Drive the published Prime extension through public CLI, HTTP, and TUI paths."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pexpect
import psutil
import yaml
from verify import REPO, assistant_text, owned_prime_processes, wait_for

from omnigent.runtime.prompt import (
    EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
    SUBAGENT_WAKE_NOTICE_INSTRUCTION,
)

PRIME_SHA256 = "27bfb28d75d3f0b1fe5674b042a5127d20cbb560d00c44b42aaa905d5600f876"
PRIME_SOURCE_COMMIT = "e260085dd8f742e0def3d871860c9a888b114851"
REQUIRED = (
    "fresh_messages",
    "model_and_effort_controls",
    "custom_prime_session",
    "living_python_memory",
    "mcp_tool",
    "root_policy_denial",
    "compaction_interrupt_followup",
    "instructions_and_skill",
    "rlm_child_root_ownership",
    "owned_cleanup",
)
PROBE_DRIVERS = (
    ".agents/skills/verify-prime-native/scripts/adapter_probe.py",
    ".agents/skills/verify-prime-native/scripts/daemon_probe.py",
    ".agents/skills/verify-prime-native/scripts/verify.py",
)
ENVIRONMENT_ARTIFACTS = (
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
)
EXERCISED_MODULES = (
    "omnigent",
    "omnigent.harness_plugins",
    "omnigent.host.connect",
    "omnigent.harnesses.prime_native.main",
    "omnigent.harnesses.prime_native.bridge",
    "omnigent.harnesses.prime_native.controls",
    "omnigent.harnesses.prime_native.catalog",
    "omnigent.harnesses.prime_native.process",
    "omnigent.runner.app",
    "omnigent.runner.native.interrupt",
    "omnigent.server.routes._sessions.helpers",
    "omnigent.server.routes.sessions.routes_core",
    "omnigent.server.routes.sessions.routes_events",
)
PRIME_BASE_SYSTEM_PROMPT_PREFIX = "You are a general purpose agent that uses code to solve tasks."
CUSTOM_AGENT_INSTRUCTION = "ADAPTER_CUSTOM_AGENT_INSTRUCTION"
COMPACTION_REQUEST_MARKER = (
    "The messages above are a conversation to summarize. Create a structured "
    "context checkpoint summary that another LLM will use to continue the work."
)
COMPACTION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI coding assistant, then produce a structured summary "
    "following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the "
    "conversation. ONLY output the structured summary."
)
COMPACTION_SPLIT_MARKER = (
    "This is the PREFIX of a turn that was too large to keep. The SUFFIX "
    "(recent work) is retained."
)
COMPACTION_SUMMARY = "ADAPTER_COMPACTION_SUMMARY"
COMPACTION_SEED_REPLY = "ADAPTER_COMPACTION_SEED_REPLY"
PONG_HTTP = "ADAPTER_HTTP_REPLY"
PONG_TUI = "ADAPTER_TUI_REPLY"
PONG_FOLLOWUP = "ADAPTER_FOLLOWUP_REPLY"
MCP_RESULT = "ADAPTER_MCP_LITERAL_RESULT"
SKILL_LITERAL = "ADAPTER_SKILL_LITERAL"
SKILL_RESULT = "ADAPTER_SKILL_COMPLETE"
SKILL_RESOURCE_PATH = "scripts/resource_probe.py"
SKILL_RESOURCE_RESULT = "ADAPTER_SKILL_RESOURCE_RESULT"
POLICY_LITERAL = "ADAPTER_POLICY_DENY_LITERAL"
RLM_ROOT_COMPLETE = "ADAPTER_RLM_ROOT_COMPLETE"
RLM_CHILD_PRIVATE_REPLY = "ADAPTER_RLM_CHILD_PRIVATE_REPLY"
RLM_NOTIFICATION_ACK = "ADAPTER_RLM_NOTIFICATION_ACK"
CLEANUP_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    httpx.HTTPError,
    pexpect.ExceptionPexpect,
    psutil.Error,
    subprocess.SubprocessError,
)
FINALIZATION_ERRORS = (Exception, KeyboardInterrupt, SystemExit)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SourceIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequestedKernel:
    entry: Path
    venv_root: Path
    rlm_module: Path
    rlm_sha256: str


@dataclass(frozen=True)
class ActualKernelIdentity:
    nonce: str
    pid: int
    executable: Path
    prefix: Path
    rlm_module: Path
    rlm_sha256: str


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started: float
    argv: tuple[str, ...]


@dataclass(frozen=True)
class SelectedRootRoles:
    tui: ProcessIdentity
    supervisor: ProcessIdentity
    worker: ProcessIdentity
    kernel: ProcessIdentity


@dataclass(frozen=True)
class SelectedRootIdentity:
    session_id: str
    config_path: str
    bridge_root: str
    agent_dir: str
    binding_pid: int
    binding_started: float
    binding_incarnation: str
    external_session_id: str
    processes: tuple[ProcessIdentity, ...]
    roles: SelectedRootRoles


def qualify_selected_root_roles(
    processes: tuple[ProcessIdentity, ...], *, binding_pid: int, kernel_entry: str
) -> SelectedRootRoles:
    def daemon_socket(process: ProcessIdentity) -> str | None:
        try:
            index = process.argv.index("--daemon-socket")
        except ValueError:
            return None
        return Path(process.argv[index + 1]).name if index + 1 < len(process.argv) else None

    selected = {
        "tui": [
            process
            for process in processes
            if "--extension" in process.argv
            and any(Path(arg).name == "omnigent_pi_native_extension.js" for arg in process.argv)
        ],
        "supervisor": [
            process for process in processes if daemon_socket(process) == "daemon.sock"
        ],
        "worker": [
            process
            for process in processes
            if process.pid == binding_pid
            and (socket_name := daemon_socket(process)) is not None
            and socket_name.startswith("worker-")
            and socket_name.endswith(".sock")
        ],
        "kernel": [
            process for process in processes if process.argv == (kernel_entry, "-m", "rlm.repl")
        ],
    }
    for role, candidates in selected.items():
        if len(candidates) != 1:
            raise RuntimeError(f"Selected Prime root has {len(candidates)} {role} roles")
    roles = SelectedRootRoles(
        selected["tui"][0],
        selected["supervisor"][0],
        selected["worker"][0],
        selected["kernel"][0],
    )
    if len({roles.tui.pid, roles.supervisor.pid, roles.worker.pid, roles.kernel.pid}) != 4:
        raise RuntimeError("Selected Prime root roles share a process identity")
    return roles


def selected_root_identity_record(identity: SelectedRootIdentity) -> dict[str, object]:
    return {
        "session_id": identity.session_id,
        "config_path": identity.config_path,
        "bridge_root": identity.bridge_root,
        "agent_dir": identity.agent_dir,
        "binding": {
            "pid": identity.binding_pid,
            "started": identity.binding_started,
            "incarnation": identity.binding_incarnation,
        },
        "external_session_id": identity.external_session_id,
        "processes": [
            {"pid": process.pid, "started": process.started, "argv": list(process.argv)}
            for process in identity.processes
        ],
        "roles": asdict(identity.roles),
    }


def assert_same_selected_root_identity(
    before: SelectedRootIdentity, after: SelectedRootIdentity
) -> None:
    if before.session_id != after.session_id:
        raise RuntimeError("Detach and resume selected a different Omnigent session")
    if before.external_session_id != after.external_session_id:
        raise RuntimeError("Detach and resume changed the native external session identity")
    if (
        before.config_path != after.config_path
        or before.bridge_root != after.bridge_root
        or before.agent_dir != after.agent_dir
    ):
        raise RuntimeError("Detach and resume changed the selected Prime bridge root")
    if (
        before.binding_pid != after.binding_pid
        or before.binding_started != after.binding_started
        or before.binding_incarnation != after.binding_incarnation
    ):
        raise RuntimeError("Detach and resume changed the selected Prime binding identity")
    if before.roles != after.roles:
        raise RuntimeError("Detach and resume changed the selected Prime root process identity")


def qualify_owned_host_record(
    record: dict[str, object], target: str, data_dir: Path, process: psutil.Process
) -> dict[str, object]:
    record_pid = record.get("pid")
    record_target = record.get("target")
    record_mode = record.get("mode")
    record_server = record.get("server_url")
    record_started = record.get("started_at")
    if not isinstance(record_pid, int) or isinstance(record_pid, bool) or record_pid <= 0:
        raise RuntimeError("Owned host registry pid is missing or malformed")
    if record_target != target or record_mode != "server" or record_server != target:
        raise RuntimeError("Owned host registry target does not match the probe server")
    if not isinstance(record_started, int | float) or isinstance(record_started, bool):
        raise RuntimeError("Owned host registry start time is missing or malformed")
    if process.pid != record_pid:
        raise RuntimeError("Owned host registry pid does not match the live process")
    started = process.create_time()
    if started > float(record_started) + 5.0:
        raise RuntimeError("Owned host registry creation time does not match the live process")
    if process.environ().get("OMNIGENT_DATA_DIR") != str(data_dir):
        raise RuntimeError("Owned host registry process has a different OMNIGENT_DATA_DIR")
    return {"pid": record_pid, "started": started, "target": target}


def cleanup_is_complete(
    *,
    final_owned_census: list[dict[str, object]],
    forced_native_fallback: bool,
    server_stopped: bool,
    fixture_stopped: bool,
    mcp_stopped: bool,
    errors: list[object],
) -> bool:
    return (
        not final_owned_census
        and not forced_native_fallback
        and server_stopped
        and fixture_stopped
        and mcp_stopped
        and not errors
    )


def live_initial_process_records(initial: list[dict[str, object]]) -> list[dict[str, object]]:
    retained: list[dict[str, object]] = []
    for record in initial:
        pid = record.get("pid")
        started = record.get("started")
        if not isinstance(pid, int) or not isinstance(started, int | float):
            continue
        try:
            process = psutil.Process(pid)
            if process.create_time() != started:
                continue
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                continue
            retained.append(
                {
                    "pid": pid,
                    "started": started,
                    "argv": process.cmdline(),
                    "ownership": f"initial_{record.get('ownership', 'unknown')}",
                }
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (OSError, psutil.Error) as exc:
            retained.append(
                {
                    "pid": pid,
                    "started": started,
                    "ownership": f"initial_{record.get('ownership', 'unknown')}",
                    "process_error": f"{type(exc).__name__}: {exc}",
                }
            )
    return retained


def _kernel_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Kernel identity {name} is missing or malformed")
    return value


def requested_kernel_identity(entry: Path) -> tuple[RequestedKernel, dict[str, object]]:
    if not entry.is_file():
        raise RuntimeError(f"Kernel venv entry is unavailable: {entry}")
    expected_root = entry.parent.parent.resolve()
    payload = json.loads(
        subprocess.check_output(
            [
                str(entry),
                "-c",
                (
                    "import hashlib,inspect,json,rlm,sys;"
                    "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
                    "'module':rlm.__file__,'module_sha256':hashlib.sha256("
                    "open(rlm.__file__,'rb').read()).hexdigest(),"
                    "'spawn':str(inspect.signature(rlm.spawn)),"
                    "'collect':str(inspect.signature(rlm.collect))}))"
                ),
            ],
            cwd=REPO,
            env={**os.environ, "PYTHONPATH": str(REPO)},
            text=True,
        )
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Kernel preflight returned malformed JSON")
    executable = Path(_kernel_string(payload, "executable"))
    prefix = Path(_kernel_string(payload, "prefix"))
    module = Path(_kernel_string(payload, "module"))
    module_sha256 = _kernel_string(payload, "module_sha256")
    if executable != entry:
        raise RuntimeError(f"Kernel preflight executable differs: {executable}")
    if prefix.resolve() != expected_root:
        raise RuntimeError(f"Kernel preflight prefix differs: {prefix}")
    if not module.is_file():
        raise RuntimeError(f"Kernel rlm module source is absent: {module}")
    if sha256(module) != module_sha256:
        raise RuntimeError("Kernel preflight rlm source hash differs")
    return (
        RequestedKernel(entry, expected_root, module.resolve(), module_sha256),
        payload,
    )


def read_actual_kernel_identity(path: Path) -> ActualKernelIdentity:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("Kernel identity sidecar is missing") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Kernel identity sidecar is malformed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Kernel identity sidecar is malformed")
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError("Kernel identity pid is missing or malformed")
    return ActualKernelIdentity(
        nonce=_kernel_string(payload, "nonce"),
        pid=pid,
        executable=Path(_kernel_string(payload, "executable")),
        prefix=Path(_kernel_string(payload, "prefix")),
        rlm_module=Path(_kernel_string(payload, "rlm_module")),
        rlm_sha256=_kernel_string(payload, "rlm_sha256"),
    )


def qualify_kernel_identity(
    requested: RequestedKernel,
    actual: ActualKernelIdentity,
    nonce: str,
    records: list[dict],
) -> None:
    if actual.nonce != nonce:
        raise RuntimeError("Kernel identity nonce differs")
    if actual.executable != requested.entry:
        raise RuntimeError("Kernel identity executable differs")
    if actual.prefix.resolve() != requested.venv_root:
        raise RuntimeError("Kernel identity prefix differs")
    if actual.rlm_module.resolve() != requested.rlm_module:
        raise RuntimeError("Kernel identity rlm module differs")
    if actual.rlm_sha256 != requested.rlm_sha256:
        raise RuntimeError("Kernel identity rlm hash differs")
    expected_argv = [str(requested.entry), "-m", "rlm.repl"]
    if any(record.get("argv") != expected_argv for record in records):
        raise RuntimeError("Kernel identity owned process argv differs")
    matches = [record for record in records if record.get("pid") == actual.pid]
    if len(matches) != 1:
        raise RuntimeError("Kernel identity must match exactly one owned process")


@dataclass(frozen=True)
class SourceArtifact:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class SourceSnapshot:
    files: tuple[SourceArtifact, ...]

    def by_path(self) -> dict[str, SourceArtifact]:
        return {artifact.path: artifact for artifact in self.files}


def _tracked_or_unignored_paths(repo: Path) -> set[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
    )
    return {Path(value.decode("utf-8")).as_posix() for value in output.split(b"\0") if value}


def source_snapshot(repo: Path = REPO) -> SourceSnapshot:
    repo = repo.resolve()
    candidates = _tracked_or_unignored_paths(repo)
    required = set(PROBE_DRIVERS) | set(ENVIRONMENT_ARTIFACTS)
    selected = {
        relative
        for relative in candidates
        if relative.startswith("omnigent/") or relative in required
    }
    missing = sorted(required - selected)
    if missing:
        raise SourceIntegrityError(f"Source inventory is missing required paths: {missing}")
    artifacts: list[SourceArtifact] = []
    for relative in sorted(selected):
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            continue
        artifacts.append(SourceArtifact(relative, path.stat().st_size, sha256(path)))
    present = {artifact.path for artifact in artifacts}
    omitted_required = sorted(required - present)
    if omitted_required:
        raise SourceIntegrityError(
            f"Source inventory excludes required regular files: {omitted_required}"
        )
    return SourceSnapshot(tuple(artifacts))


def compare_source_snapshots(
    before: SourceSnapshot, after: SourceSnapshot
) -> dict[str, list[object]]:
    before_files = before.by_path()
    after_files = after.by_path()
    added = sorted(set(after_files) - set(before_files))
    removed = sorted(set(before_files) - set(after_files))
    changed = [
        {
            "path": path,
            "before": asdict(before_files[path]),
            "after": asdict(after_files[path]),
        }
        for path in sorted(set(before_files) & set(after_files))
        if before_files[path] != after_files[path]
    ]
    return {"added": added, "removed": removed, "changed": changed}


def assert_same_sources(before: SourceSnapshot, after: SourceSnapshot) -> dict[str, list[object]]:
    comparison = compare_source_snapshots(before, after)
    if any(comparison.values()):
        raise SourceIntegrityError(
            "Source integrity changed: " + json.dumps(comparison, sort_keys=True)
        )
    return comparison


def assert_checkout_origin(module_name: str, location: Path, checkout: Path = REPO) -> Path:
    source = location.resolve()
    managed_checkout = checkout.resolve()
    if not source.is_relative_to(managed_checkout):
        raise SourceIntegrityError(f"Imported {module_name} outside the exact worktree: {source}")
    return source


def import_witnesses(checkout: Path = REPO) -> dict[str, dict[str, str]]:
    witnesses: dict[str, dict[str, str]] = {}
    for module_name in EXERCISED_MODULES:
        module = importlib.import_module(module_name)
        location = getattr(module, "__file__", None)
        if not isinstance(location, str):
            raise SourceIntegrityError(f"Imported {module_name} has no source location")
        source = assert_checkout_origin(module_name, Path(location), checkout)
        if not source.is_file():
            raise SourceIntegrityError(f"Imported {module_name} source is absent: {source}")
        witnesses[module_name] = {"path": str(source), "sha256": sha256(source)}
    return witnesses


def deployed_extension_identity(source: Path, deployed: Path) -> dict[str, object]:
    if not source.is_file() or source.is_symlink():
        raise SourceIntegrityError(f"Packaged extension is unavailable: {source}")
    if not deployed.is_file() or deployed.is_symlink():
        raise SourceIntegrityError(f"Deployed extension is unavailable: {deployed}")
    source_hash = sha256(source)
    deployed_hash = sha256(deployed)
    identity = {
        "packaged_path": str(source),
        "packaged_sha256": source_hash,
        "deployed_path": str(deployed),
        "deployed_sha256": deployed_hash,
        "matches": source_hash == deployed_hash,
    }
    if not identity["matches"]:
        raise SourceIntegrityError("Deployed extension bytes differ from the packaged source")
    return identity


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def skill_resource_source(nonce: str, marker: Path) -> str:
    return (
        "from pathlib import Path\n"
        f"adapter_skill_resource_path = Path({str(marker)!r})\n"
        f"adapter_skill_resource_path.write_text({nonce!r}, encoding='utf-8')\n"
        f"print({SKILL_RESOURCE_RESULT!r}, {nonce!r})\n"
    )


@dataclass
class PublicAction:
    entry_point: str
    request: object
    response_status: int | None = None
    response_body: object | None = None


@dataclass
class Observation:
    name: str
    value: object


@dataclass
class Scenario:
    verdict: str = "INCONCLUSIVE"
    actions: list[PublicAction] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    reason: str | None = "Scenario not reached"


class Evidence:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.scenarios = {name: Scenario() for name in REQUIRED}
        self.fatal: list[dict[str, str]] = []
        self.launches: list[dict[str, object]] = []

    def action(
        self,
        name: str,
        entry_point: str,
        request: object,
        response: httpx.Response | None = None,
    ) -> None:
        body: object | None = None
        if response is not None:
            try:
                body = response.json()
            except json.JSONDecodeError:
                body = response.text
        self.scenarios[name].actions.append(
            PublicAction(
                entry_point,
                request,
                response.status_code if response is not None else None,
                body,
            )
        )

    def observe(self, name: str, observation: str, value: object) -> None:
        self.scenarios[name].observations.append(Observation(observation, value))

    def verified(self, name: str) -> None:
        self.scenarios[name].verdict = "VERIFIED"
        self.scenarios[name].reason = None

    def failed(self, name: str, exc: BaseException) -> None:
        self.scenarios[name].verdict = "NOT VERIFIED"
        self.scenarios[name].reason = f"{type(exc).__name__}: {exc}"

    def record_fatal(self, exc: BaseException) -> None:
        self.fatal.append({"type": type(exc).__name__, "error": str(exc)})
        write_json(self.root / "failure.json", self.fatal)

    def record_launch(self, name: str, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        if env.get("PYTHONPATH") != str(REPO):
            raise SourceIntegrityError(f"{name} did not receive the managed checkout PYTHONPATH")
        self.launches.append(
            {
                "name": name,
                "argv": argv,
                "cwd": str(cwd),
                "environment": {
                    key: env[key]
                    for key in sorted(env)
                    if key == "PYTHONPATH" or key.startswith(("OMNIGENT_", "PRIME_AGENT_"))
                },
            }
        )
        write_json(self.root / "launch-witnesses.json", self.launches)

    def record_launch_start(self, name: str, pid: int) -> None:
        for launch in reversed(self.launches):
            if launch["name"] == name:
                launch["pid"] = pid
                write_json(self.root / "launch-witnesses.json", self.launches)
                return
        raise RuntimeError(f"Launch witness is missing for {name}")

    @property
    def final_verdict(self) -> str:
        if self.fatal:
            return "NOT VERIFIED"
        verdicts = [scenario.verdict for scenario in self.scenarios.values()]
        if "NOT VERIFIED" in verdicts:
            return "NOT VERIFIED"
        if all(verdict == "VERIFIED" for verdict in verdicts):
            return "VERIFIED"
        return "INCONCLUSIVE"

    def persist(self) -> None:
        write_json(
            self.root / "outcomes.json",
            {
                "final_verdict": self.final_verdict,
                "fatal": self.fatal,
                "launches": self.launches,
                "scenarios": {name: asdict(value) for name, value in self.scenarios.items()},
            },
        )

    def write_report(self) -> Path:
        report = self.root / "final-report.md"
        rows = "".join(
            f"| {name} | {scenario.verdict} | {scenario.reason or ''} |\n"
            for name, scenario in self.scenarios.items()
        )
        fatal = ""
        if self.fatal:
            fatal = "\nFatal integrity or orchestration failures:\n" + "".join(
                f"- `{item['type']}: {item['error']}`\n" for item in self.fatal
            )
        report.write_text(
            "# Prime native adapter evidence\n\n"
            f"Final verdict: **{self.final_verdict}**.\n"
            f"{fatal}\n"
            "| Scenario | Verdict | Reason |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n"
            "The run uses public CLI, HTTP, and native TUI paths with the published "
            "Prime binary and real kernel. The fixture replaces only remote inference.\n",
            encoding="utf-8",
        )
        return report


class ModelFixture:
    def __init__(
        self,
        evidence: Path,
        workspace: Path,
        nonce: str,
        expected_tool: str | None,
        expected_mcp: str | None,
    ) -> None:
        self.evidence = evidence
        self.kernel_identity_path = evidence / "kernel-identity.json"
        self.workspace = workspace
        self.nonce = nonce
        self.expected_tool = expected_tool
        self.expected_mcp = expected_mcp
        self.calls: list[str] = []
        self.call_records: list[dict[str, object]] = []
        self.tool_results: dict[str, list[str]] = {}
        self.errors: list[str] = []
        self.admission_started = threading.Event()
        self.admission_release = threading.Event()
        self.instruction_roles: dict[str, set[str]] = {
            "prime_base": set(),
            "custom_agent": set(),
            "framework_subagent_wake": set(),
            "framework_embedded_browser": set(),
        }
        self.summary_requests: list[dict[str, object]] = []
        self.conversation_logs: set[Path] = set()
        self.child_task_requests: list[str] = []
        self.child_notification_inputs: list[str] = []
        self.sleep_marker = workspace / "sleep-started.txt"
        self.child_marker = workspace / "rlm-child-nonce.txt"
        self.skill_resource_marker = workspace / "skill-resource-nonce.txt"
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                append_json(evidence / "model-requests.jsonl", request)
                try:
                    delta, finish = fixture.reply(request)
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    fixture.errors.append(message)
                    append_json(evidence / "fixture-errors.jsonl", {"error": message})
                    delta, finish = (
                        {
                            "role": "assistant",
                            "content": "ADAPTER_FIXTURE_FAILED",
                        },
                        "stop",
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for content, reason in ((delta, None), ({}, finish)):
                        chunk = {
                            "id": "adapter-probe",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "fixture-a",
                            "choices": [{"index": 0, "delta": content, "finish_reason": reason}],
                        }
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _latest_user_index(messages: list[dict]) -> int:
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "user":
                return index
        raise RuntimeError("Model request has no user message")

    @classmethod
    def _latest_user(cls, messages: list[dict]) -> str:
        return cls._message_text(messages[cls._latest_user_index(messages)])

    @staticmethod
    def _message_text(message: dict) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and "text" in block
            )
        return json.dumps(content, sort_keys=True)

    @classmethod
    def _tool_results(cls, messages: list[dict]) -> list[dict]:
        start = cls._latest_user_index(messages) + 1
        return [message for message in messages[start:] if message.get("role") == "tool"]

    @staticmethod
    def _tool_names(request: dict) -> set[str]:
        names: set[str] = set()
        for tool in request.get("tools", []):
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else tool.get("name")
            if isinstance(name, str):
                names.add(name)
        return names

    def _require_tool(self, request: dict, name: str) -> None:
        if name not in self._tool_names(request):
            raise RuntimeError(f"Prime did not expose required real tool {name!r}")

    def _tool_call(self, name: str, arguments: dict) -> tuple[dict, str]:
        self.calls.append(name)
        self.call_records.append({"name": name, "arguments": arguments})
        return (
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"adapter_{uuid.uuid4().hex}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            "tool_calls",
        )

    @staticmethod
    def _tool_result_name(messages: list[dict], tool: dict) -> str:
        tool_call_id = tool.get("tool_call_id")
        for message in reversed(messages):
            for call in message.get("tool_calls", []) or []:
                if call.get("id") == tool_call_id:
                    function = call.get("function")
                    if isinstance(function, dict) and isinstance(function.get("name"), str):
                        return function["name"]
        raise RuntimeError("Could not match the tool result to its real tool call")

    def _observe_instructions(self, messages: list[dict]) -> None:
        expected = {
            "custom_agent": f"{CUSTOM_AGENT_INSTRUCTION} {self.nonce}",
            "framework_subagent_wake": SUBAGENT_WAKE_NOTICE_INSTRUCTION,
            "framework_embedded_browser": EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
        }
        for message in messages:
            role = message.get("role")
            if not isinstance(role, str):
                continue
            content = self._message_text(message)
            if content.startswith(PRIME_BASE_SYSTEM_PROMPT_PREFIX):
                self.instruction_roles["prime_base"].add(role)
                for line in content.splitlines():
                    if line.startswith("Conversation log: "):
                        self.conversation_logs.add(Path(line.removeprefix("Conversation log: ")))
            for name, literal in expected.items():
                if literal in content:
                    self.instruction_roles[name].add(role)

    def instruction_observations(self) -> dict[str, object]:
        return {
            "prime_base": {
                "literal_prefix": PRIME_BASE_SYSTEM_PROMPT_PREFIX,
                "observed_roles": sorted(self.instruction_roles["prime_base"]),
            },
            "custom_agent": {
                "literal": f"{CUSTOM_AGENT_INSTRUCTION} {self.nonce}",
                "observed_roles": sorted(self.instruction_roles["custom_agent"]),
            },
            "framework_subagent_wake": {
                "literal": SUBAGENT_WAKE_NOTICE_INSTRUCTION,
                "observed_roles": sorted(self.instruction_roles["framework_subagent_wake"]),
            },
            "framework_embedded_browser": {
                "literal": EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
                "observed_roles": sorted(self.instruction_roles["framework_embedded_browser"]),
            },
        }

    @classmethod
    def _compaction_summary_kind(cls, request: dict, messages: list[dict]) -> str | None:
        if request.get("tools"):
            return None
        if not any(
            message.get("role") in {"developer", "system"}
            and cls._message_text(message) == COMPACTION_SYSTEM_PROMPT
            for message in messages
        ):
            return None
        user_text = "\n".join(
            cls._message_text(message) for message in messages if message.get("role") == "user"
        )
        if COMPACTION_REQUEST_MARKER in user_text:
            return "history"
        if COMPACTION_SPLIT_MARKER in user_text:
            return "split_turn_prefix"
        raise RuntimeError("Prime summary request has an unknown real prompt shape")

    def _remember_exact(self, name: str, actual: str, expected: str) -> None:
        if actual != expected:
            raise RuntimeError(
                f"{name} tool result differs: expected {expected!r}, observed {actual!r}"
            )
        self.tool_results.setdefault(name, []).append(actual)

    @staticmethod
    def _content(tool: dict) -> str:
        return str(tool.get("content", "")).strip()

    def reply(self, request: dict) -> tuple[dict, str]:
        messages = request["messages"]
        if not isinstance(messages, list):
            raise RuntimeError("Model request messages are malformed")
        self._observe_instructions(messages)
        compaction_kind = self._compaction_summary_kind(request, messages)
        if compaction_kind is not None:
            self.summary_requests.append(
                {
                    "kind": compaction_kind,
                    "model": request.get("model"),
                    "message_roles": [message.get("role") for message in messages],
                    "tool_count": len(request.get("tools", [])),
                    "system_prompt": COMPACTION_SYSTEM_PROMPT,
                    "response_literal": f"{COMPACTION_SUMMARY} {self.nonce}",
                }
            )
            return {
                "role": "assistant",
                "content": f"{COMPACTION_SUMMARY} {self.nonce}",
            }, "stop"
        prompt = self._latest_user(messages)
        tools = self._tool_results(messages)
        if "ADAPTER_HTTP" in prompt:
            self.admission_started.set()
            if not self.admission_release.wait(120):
                raise RuntimeError("Admission fixture was not released")
            return {"role": "assistant", "content": PONG_HTTP}, "stop"
        if "ADAPTER_TUI" in prompt:
            return {"role": "assistant", "content": PONG_TUI}, "stop"
        if "ADAPTER_FOLLOWUP" in prompt:
            nonce = prompt.split("ADAPTER_FOLLOWUP ", 1)[1].split()[0]
            return {
                "role": "assistant",
                "content": f"{PONG_FOLLOWUP} {nonce}",
            }, "stop"
        if prompt.startswith("ADAPTER_COMPACTION_SEED "):
            seed_index = prompt.split()[1]
            return {
                "role": "assistant",
                "content": (f"{COMPACTION_SEED_REPLY} {seed_index} " + "history " * 48).strip(),
            }, "stop"
        if "ADAPTER_MEMORY_SEED" in prompt:
            expected = f"{self.nonce} 41 True"
            if tools:
                self._remember_exact("memory_seed", self._content(tools[-1]), expected)
                return {"role": "assistant", "content": "ADAPTER_MEMORY_SEEDED"}, "stop"
            self._require_tool(request, "ipython")
            code = (
                "import hashlib\n"
                "import importlib\n"
                "import json\n"
                "import os\n"
                "import socket\n"
                "import sys\n"
                "from pathlib import Path\n"
                "adapter_probe_injected_rlm = rlm\n"
                "adapter_probe_loaded_rlm = importlib.import_module('rlm')\n"
                "assert rlm is adapter_probe_injected_rlm\n"
                "adapter_probe_kernel_identity_path = Path("
                f"{str(self.kernel_identity_path)!r})\n"
                "adapter_probe_kernel_identity_path.write_text(json.dumps({"
                f"'nonce': {self.nonce!r}, 'pid': os.getpid(), "
                "'executable': sys.executable, 'prefix': sys.prefix, "
                "'rlm_module': adapter_probe_loaded_rlm.__file__, "
                "'rlm_sha256': hashlib.sha256(Path("
                "adapter_probe_loaded_rlm.__file__).read_bytes()).hexdigest()"
                "}), encoding='utf-8')\n"
                "adapter_probe_value = "
                f"{{'token': {self.nonce!r}, 'count': 41, 'socket': socket.socket()}}\n"
                "print(adapter_probe_value['token'], adapter_probe_value['count'], "
                "adapter_probe_value['socket'].fileno() >= 0)"
            )
            return self._tool_call("ipython", {"code": code})
        if "ADAPTER_MEMORY_READ" in prompt:
            expected = self.expected_tool or f"{self.nonce} 42 True"
            if tools:
                self._remember_exact("memory_read", self._content(tools[-1]), expected)
                return {"role": "assistant", "content": "ADAPTER_MEMORY_READ"}, "stop"
            self._require_tool(request, "ipython")
            code = (
                "adapter_probe_value['count'] += 1\n"
                "print(adapter_probe_value['token'], adapter_probe_value['count'], "
                "adapter_probe_value['socket'].fileno() >= 0)"
            )
            return self._tool_call("ipython", {"code": code})
        if "ADAPTER_MCP" in prompt:
            if tools:
                expected = self.expected_mcp or MCP_RESULT
                self._remember_exact("mcp", self._content(tools[-1]), expected)
                return {"role": "assistant", "content": MCP_RESULT}, "stop"
            name = "adapter_fixture__echo"
            self._require_tool(request, name)
            return self._tool_call(name, {"value": MCP_RESULT})
        if "ADAPTER_DENIED_WRITE" in prompt:
            if tools:
                actual = self._content(tools[-1])
                if POLICY_LITERAL not in actual:
                    raise RuntimeError(f"policy result lacks {POLICY_LITERAL!r}: {actual!r}")
                self.tool_results.setdefault("denial", []).append(actual)
                return {"role": "assistant", "content": "ADAPTER_POLICY_DENIED"}, "stop"
            self._require_tool(request, "sys_os_write")
            marker = self.workspace / "policy-marker.txt"
            return self._tool_call("sys_os_write", {"path": str(marker), "content": "forbidden"})
        if "ADAPTER_SLEEP" in prompt:
            if tools:
                self.tool_results.setdefault("sleep", []).append(self._content(tools[-1]))
                return {"role": "assistant", "content": "ADAPTER_SLEEP_FINISHED"}, "stop"
            self._require_tool(request, "ipython")
            code = (
                f"open({str(self.sleep_marker)!r}, 'w').write('ADAPTER_SLEEP_STARTED')\n"
                "import time\n"
                "time.sleep(12)\n"
                "print('ADAPTER_SLEEP_FINISHED')"
            )
            return self._tool_call("ipython", {"code": code})
        if "ADAPTER_SKILL" in prompt:
            if not tools:
                self._require_tool(request, "load_skill")
                return self._tool_call("load_skill", {"name": "adapter-proof"})
            actual = self._content(tools[-1])
            tool_name = self._tool_result_name(messages, tools[-1])
            if tool_name == "load_skill":
                if SKILL_LITERAL not in actual:
                    raise RuntimeError(f"skill result lacks {SKILL_LITERAL!r}: {actual!r}")
                if f"- {SKILL_RESOURCE_PATH}" not in actual:
                    raise RuntimeError(
                        f"load_skill did not advertise {SKILL_RESOURCE_PATH!r}: {actual!r}"
                    )
                self.tool_results.setdefault("skill_load", []).append(actual)
                self._require_tool(request, "read_skill_file")
                return self._tool_call(
                    "read_skill_file",
                    {"skill_name": "adapter-proof", "path": SKILL_RESOURCE_PATH},
                )
            if tool_name == "read_skill_file":
                expected_source = skill_resource_source(
                    self.nonce, self.skill_resource_marker
                ).strip()
                self._remember_exact("skill_resource_read", actual, expected_source)
                self._require_tool(request, "ipython")
                return self._tool_call("ipython", {"code": actual})
            if tool_name == "ipython":
                expected = f"{SKILL_RESOURCE_RESULT} {self.nonce}"
                self._remember_exact("skill_resource_execution", actual, expected)
                return {"role": "assistant", "content": SKILL_RESULT}, "stop"
            raise RuntimeError(f"Unexpected skill proof tool result from {tool_name!r}")
        child_task = f"[task from parent]\n\nADAPTER_RLM_CHILD {self.nonce}"
        if prompt == child_task:
            expected = f"ADAPTER_RLM_CHILD_TOOL_RESULT {self.nonce}"
            if tools:
                self._remember_exact("rlm_child", self._content(tools[-1]), expected)
                return {"role": "assistant", "content": RLM_CHILD_PRIVATE_REPLY}, "stop"
            self.child_task_requests.append(prompt)
            self._require_tool(request, "ipython")
            code = (
                f"open({str(self.child_marker)!r}, 'w').write({self.nonce!r})\n"
                f"print('ADAPTER_RLM_CHILD_TOOL_RESULT {self.nonce}')"
            )
            return self._tool_call("ipython", {"code": code})
        if prompt.startswith("[child-exited:"):
            self.child_notification_inputs.append(prompt)
            return {"role": "assistant", "content": RLM_NOTIFICATION_ACK}, "stop"
        if prompt == f"ADAPTER_RLM_ROOT {self.nonce}":
            if tools:
                actual = self._content(tools[-1])
                literal = f"ADAPTER_RLM_ROOT_TOOL_RESULT {self.nonce} done"
                if literal not in actual:
                    raise RuntimeError(f"root RLM result lacks {literal!r}: {actual!r}")
                self.tool_results.setdefault("rlm_root", []).append(actual)
                return {"role": "assistant", "content": RLM_ROOT_COMPLETE}, "stop"
            self._require_tool(request, "ipython")
            code = (
                "import rlm\n"
                "adapter_child = await rlm.spawn("
                f"'ADAPTER_RLM_CHILD {self.nonce}', name='adapter-{self.nonce[:8]}', "
                "model='verify/fixture-a', thinking='low')\n"
                "adapter_results = await rlm.collect(adapter_child, timeout_ms=120000)\n"
                "adapter_result = adapter_results[0]\n"
                "assert adapter_result.settled\n"
                "assert adapter_result.status == 'done'\n"
                "assert adapter_result.tool_use_count >= 1\n"
                f"print('ADAPTER_RLM_ROOT_TOOL_RESULT', {self.nonce!r}, "
                "adapter_result.status, adapter_result.tool_use_count)"
            )
            return self._tool_call("ipython", {"code": code})
        return {"role": "assistant", "content": "ADAPTER_UNRECOGNIZED"}, "stop"

    def raise_error(self) -> None:
        if self.errors:
            raise RuntimeError(self.errors.pop(0))

    def close(self) -> None:
        self.admission_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class McpFixture:
    def __init__(self, evidence: Evidence, env: dict[str, str]) -> None:
        self.port = unused_port()
        self.calls_path = evidence.root / "mcp-calls.jsonl"
        self.log_handle = (evidence.root / "mcp-server.log").open("w", encoding="utf-8")
        argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve-mcp",
            "--mcp-port",
            str(self.port),
            "--mcp-calls",
            str(self.calls_path),
        ]
        evidence.record_launch("mcp_fixture", argv, REPO, env)
        self.process = subprocess.Popen(
            argv,
            cwd=REPO,
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        evidence.record_launch_start("mcp_fixture", self.process.pid)

        def listening() -> bool:
            if self.process.poll() is not None:
                raise RuntimeError(f"MCP fixture exited with {self.process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return True
            except OSError:
                return False

        try:
            wait_for(listening, "real MCP fixture socket", 30)
        except CLEANUP_ERRORS:
            with contextlib.suppress(*CLEANUP_ERRORS):
                self.close()
            raise

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log_handle.close()


@dataclass
class PrimeSession:
    purpose: str
    session_id: str
    terminal: pexpect.spawn
    log_handle: object
    before_detach: list[dict] = field(default_factory=list)
    before_selected_root: SelectedRootIdentity | None = None


class AdapterRun:
    def __init__(
        self, args: argparse.Namespace, evidence: Evidence, requested_kernel: RequestedKernel
    ) -> None:
        self.args = args
        self.evidence = evidence
        self.requested_kernel = requested_kernel
        self.kernel_identity_path = evidence.root / "kernel-identity.json"
        self.runtime = Path(tempfile.mkdtemp(prefix="prime-adapter-runtime-", dir="/tmp"))
        self.workspace = self.runtime / "workspace"
        self.prime_config = self.runtime / "prime"
        self.prime_sessions = self.runtime / "prime-sessions"
        self.agent_root = self.runtime / "agents"
        self.workspace.mkdir()
        self.prime_config.mkdir()
        self.prime_sessions.mkdir()
        self.agent_root.mkdir()
        self.nonce = uuid.uuid4().hex
        self.url: str | None = None
        self.server: subprocess.Popen[str] | None = None
        self.server_log = None
        self.sessions: list[PrimeSession] = []
        self.owned_session_ids: list[str] = []
        self.allowed: PrimeSession | None = None
        self.denied: PrimeSession | None = None
        self.fixture = ModelFixture(
            evidence.root,
            self.workspace,
            self.nonce,
            args.expected_tool,
            args.expected_mcp,
        )
        try:
            self._write_prime_config()
            self.mcp = McpFixture(evidence, self.env())
        except CLEANUP_ERRORS:
            with contextlib.suppress(*CLEANUP_ERRORS):
                self.fixture.close()
            raise

    def _write_prime_config(self) -> None:
        endpoint = self.fixture.server.server_port
        write_json(
            self.prime_config / "models.json",
            {
                "providers": {
                    "verify": {
                        "baseUrl": f"http://127.0.0.1:{endpoint}/v1",
                        "api": "openai-completions",
                        "apiKey": "fixture",
                        "models": [
                            {
                                "id": "fixture-a",
                                "reasoning": True,
                                "input": ["text"],
                                "contextWindow": 32768,
                                "maxTokens": 1024,
                            },
                            {
                                "id": "fixture-b",
                                "reasoning": True,
                                "input": ["text"],
                                "contextWindow": 32768,
                                "maxTokens": 1024,
                            },
                        ],
                    }
                }
            },
        )
        write_json(
            self.prime_config / "settings.json",
            {
                "onboardingShown": True,
                "defaultProvider": "verify",
                "defaultModel": "fixture-a",
                "sessionDir": str(self.prime_sessions),
                "compaction": {
                    "enabled": False,
                    "reserveTokens": 1024,
                    "keepRecentTokens": 128,
                },
                "autoRefine": {"enabled": False},
            },
        )

    def env(self) -> dict[str, str]:
        values = dict(os.environ)
        values.update(
            {
                "OMNIGENT_CONFIG_HOME": str(self.runtime / "config"),
                "OMNIGENT_DATA_DIR": str(self.runtime / "data"),
                "OMNIGENT_SKIP_WEB_UI": "true",
                "BROWSER": "false",
                "TERM": "xterm-256color",
                "PRIME_AGENT_CODING_AGENT_DIR": str(self.prime_config),
                "PRIME_AGENT_SESSION_DIR": str(self.prime_sessions),
                "PRIME_AGENT_KERNEL_PYTHON": str(self.args.kernel_python),
                "OMNIGENT_PRIME_PATH": str(self.args.prime_path),
                "PYTHONPATH": str(REPO),
            }
        )
        return values

    def _agent_dir(self, purpose: str, *, denied: bool) -> Path:
        directory = self.agent_root / purpose
        directory.mkdir()
        config: dict[str, object] = {
            "spec_version": 1,
            "name": f"prime-native-{purpose}",
            "prompt": f"{CUSTOM_AGENT_INSTRUCTION} {self.nonce}",
            "executor": {"type": "omnigent", "config": {"harness": "prime-native"}},
            "spawn": True,
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none"},
            },
        }
        from omnigent.native.native_coding_agents import native_shell_terminal_spec

        config["terminals"] = native_shell_terminal_spec()
        if denied:
            config["guardrails"] = {
                "policies": {
                    "deny_adapter_write": {
                        "type": "function",
                        "on": ["tool_call:sys_os_write"],
                        "function": {
                            "path": "omnigent.policies.function.make_fixed_action_callable",
                            "arguments": {
                                "action": "deny",
                                "reason": POLICY_LITERAL,
                                "on_phases": ["tool_call"],
                                "on_tools": ["sys_os_write"],
                            },
                        },
                    }
                }
            }
        else:
            skill = directory / "skills" / "adapter-proof"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: adapter-proof\n"
                "description: Prove declared Python skill resource transport.\n"
                "---\n\n"
                f"Return and preserve the exact instruction marker `{SKILL_LITERAL}`.\n"
                f"Read `{SKILL_RESOURCE_PATH}` only through `read_skill_file`.\n",
                encoding="utf-8",
            )
            (skill / "pyproject.toml").write_text(
                "[project]\n"
                'name = "adapter-proof"\n'
                'version = "0.0.0"\n'
                'requires-python = ">=3.11"\n',
                encoding="utf-8",
            )
            package = skill / "src" / "adapter_proof"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                f"RESOURCE_PATH = {SKILL_RESOURCE_PATH!r}\n",
                encoding="utf-8",
            )
            resource = skill / SKILL_RESOURCE_PATH
            resource.parent.mkdir(parents=True)
            resource.write_text(
                skill_resource_source(self.nonce, self.fixture.skill_resource_marker),
                encoding="utf-8",
            )
            mcp = directory / "tools" / "mcp"
            mcp.mkdir(parents=True)
            (mcp / "adapter-fixture.yaml").write_text(
                yaml.safe_dump(
                    {
                        "name": "adapter_fixture",
                        "transport": "http",
                        "description": "Literal adapter verification fixture.",
                        "url": self.mcp.url,
                        "timeout": 30,
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
        (directory / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        (directory / "AGENTS.md").write_text(
            "Use real tools when the model requests them.\n", encoding="utf-8"
        )
        return directory

    def start(self) -> None:
        self.url = f"http://127.0.0.1:{unused_port()}"
        self.server_log = (self.evidence.root / "server.log").open("w", encoding="utf-8")
        argv = [
            str(REPO / ".venv/bin/omnigent"),
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            self.url.rsplit(":", 1)[1],
            "--database-uri",
            f"sqlite:///{self.runtime}/data/server.db",
            "--artifact-location",
            str(self.runtime / "artifacts"),
        ]
        env = self.env()
        self.evidence.record_launch("omnigent_server", argv, self.workspace, env)
        self.server = subprocess.Popen(
            argv,
            cwd=self.workspace,
            env=env,
            stdout=self.server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.evidence.record_launch_start("omnigent_server", self.server.pid)

        def healthy() -> bool:
            if self.server is None or self.server.poll() is not None:
                raise RuntimeError("Owned Omnigent server exited before readiness")
            try:
                with self.client(timeout=2) as client:
                    return client.get("/health").status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(healthy, "owned Omnigent server health", 60)
        self.allowed = self.create_session("allowed", denied=False)
        write_json(
            self.evidence.root / "launch.json",
            {
                "url": self.url,
                "allowed_session_id": self.allowed.session_id,
                "prime": str(self.args.prime_path),
                "kernel_python_entry": str(self.args.kernel_python),
            },
        )

    def client(self, *, timeout: float = 15) -> httpx.Client:
        if self.url is None:
            raise RuntimeError("Run has not started")
        return httpx.Client(base_url=self.url, timeout=timeout)

    def create_session(self, purpose: str, *, denied: bool) -> PrimeSession:
        from omnigent.chat import _bundle_agent

        bundle = _bundle_agent(self._agent_dir(purpose, denied=denied))
        metadata = {
            "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "prime-native-ui"},
            "terminal_launch_args": [
                "--offline",
                "--provider",
                "verify",
                "--model",
                "fixture-a",
                "--no-context-files",
            ],
        }
        with self.client(timeout=60) as client:
            response = client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(metadata)},
                files={"bundle": (f"{purpose}.tar.gz", bundle, "application/gzip")},
            )
        self.evidence.action(
            "root_policy_denial" if denied else "fresh_messages",
            "HTTP multipart POST /v1/sessions",
            {"purpose": purpose, "metadata": metadata, "bundle_bytes": len(bundle)},
            response,
        )
        if response.is_error:
            raise RuntimeError(
                f"Session bundle rejected with {response.status_code}: {response.text}"
            )
        session_id = response.json()["session_id"]
        self.owned_session_ids.append(session_id)
        argv = ["prime-native", "--server", str(self.url), "--resume", session_id]
        env = self.env()
        self.evidence.record_launch(
            f"prime_native_cli_{purpose}",
            [str(REPO / ".venv/bin/omnigent"), *argv],
            self.workspace,
            env,
        )
        terminal = pexpect.spawn(
            str(REPO / ".venv/bin/omnigent"),
            argv,
            cwd=str(self.workspace),
            env=env,
            encoding="utf-8",
            timeout=120,
            dimensions=(36, 120),
        )
        self.evidence.record_launch_start(f"prime_native_cli_{purpose}", terminal.pid)
        log_handle = None
        try:
            log_handle = (self.evidence.root / f"terminal-{purpose}.txt").open(
                "w", encoding="utf-8"
            )
            terminal.logfile_read = log_handle
            terminal.expect(r"Web UI: [^\r\n]*/c/([A-Za-z0-9_-]+)")
            if terminal.match.group(1) != session_id:
                raise RuntimeError(f"{purpose} terminal attached to a different session")
        except CLEANUP_ERRORS:
            with contextlib.suppress(*CLEANUP_ERRORS):
                terminal.close(force=True)
            if log_handle is not None:
                with contextlib.suppress(*CLEANUP_ERRORS):
                    log_handle.close()
            raise
        assert log_handle is not None
        session = PrimeSession(purpose, session_id, terminal, log_handle)
        self.sessions.append(session)
        self._record_deployed_extension_identity(session)
        return session

    def _record_deployed_extension_identity(self, session: PrimeSession) -> None:
        source = REPO / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"

        def inspect() -> list[dict[str, object]] | None:
            identities: list[dict[str, object]] = []
            for process in owned_prime_processes(self.runtime):
                try:
                    config_path = process.environ().get("OMNIGENT_EXTENSION_NATIVE_CONFIG")
                    if not config_path:
                        continue
                    config = Path(config_path)
                    if not config.is_file() or config.is_symlink():
                        continue
                    payload = json.loads(config.read_text(encoding="utf-8"))
                    if payload.get("sessionId") != session.session_id:
                        continue
                    identity = deployed_extension_identity(
                        source, config.parent / "omnigent_pi_native_extension.js"
                    )
                    identity["session_id"] = session.session_id
                    identity["pid"] = process.pid
                    identities.append(identity)
                except (OSError, ValueError, psutil.Error) as exc:
                    raise SourceIntegrityError(
                        f"Could not inspect deployed extension for {session.session_id}: {exc}"
                    ) from exc
            return identities or None

        identities = wait_for(inspect, f"deployed extension for {session.session_id}", 20)
        evidence_path = self.evidence.root / "deployed-extension-identities.json"
        existing: list[dict[str, object]] = []
        if evidence_path.is_file():
            existing = json.loads(evidence_path.read_text(encoding="utf-8"))
        write_json(evidence_path, [*existing, *identities])

    def post_event(self, scenario: str, session: PrimeSession, body: dict) -> httpx.Response:
        with self.client() as client:
            response = client.post(f"/v1/sessions/{session.session_id}/events", json=body)
        self.evidence.action(scenario, "HTTP POST /v1/sessions/{id}/events", body, response)
        return response

    def patch_session(self, scenario: str, session: PrimeSession, body: dict) -> httpx.Response:
        with self.client(timeout=30) as client:
            response = client.patch(f"/v1/sessions/{session.session_id}", json=body)
        self.evidence.action(scenario, "HTTP PATCH /v1/sessions/{id}", body, response)
        return response

    def items(self, session: PrimeSession) -> object:
        with self.client() as client:
            response = client.get(f"/v1/sessions/{session.session_id}/items")
            response.raise_for_status()
            result = response.json()
        write_json(self.evidence.root / f"items-{session.purpose}-latest.json", result)
        return result

    def snapshot(self, session: PrimeSession) -> dict:
        with self.client() as client:
            response = client.get(f"/v1/sessions/{session.session_id}")
            response.raise_for_status()
            return response.json()

    def wait_for_reply(
        self, session: PrimeSession, expected: str, prompt: str, timeout: float = 120
    ) -> object:
        def replied() -> object | None:
            self.fixture.raise_error()
            items = self.items(session)
            if prompt in json.dumps(items) and expected in assistant_text(items):
                return items
            return None

        return wait_for(replied, f"literal assistant reply {expected}", timeout)

    def wait_for_idle(self, session: PrimeSession, timeout: float = 60) -> dict:
        return wait_for(
            lambda: (
                snapshot if (snapshot := self.snapshot(session)).get("status") == "idle" else None
            ),
            f"{session.purpose} session idle status",
            timeout,
        )

    def send_tui(self, scenario: str, session: PrimeSession, text: str) -> None:
        self.evidence.action(scenario, "native Prime TUI", {"text": text, "key": "Enter"})
        session.terminal.send(text + "\r")

    def process_records(self) -> list[dict]:
        records: list[dict] = []
        for process in owned_prime_processes(self.runtime):
            with contextlib.suppress(psutil.Error):
                records.append(
                    {
                        "pid": process.pid,
                        "started": process.create_time(),
                        "argv": process.cmdline(),
                    }
                )
        return sorted(records, key=lambda record: int(record["pid"]))

    def selected_root_identity(self, session: PrimeSession) -> SelectedRootIdentity:
        matching_configs: dict[Path, dict[str, object]] = {}
        for process in owned_prime_processes(self.runtime):
            try:
                config_value = process.environ().get("OMNIGENT_EXTENSION_NATIVE_CONFIG")
                if not config_value:
                    continue
                config_path = Path(config_value)
                if not config_path.is_file() or config_path.is_symlink():
                    raise RuntimeError(
                        f"Selected Prime extension config is unavailable: {config_path}"
                    )
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(config, dict) or config.get("sessionId") != session.session_id:
                    continue
                matching_configs[config_path.resolve()] = config
            except (OSError, json.JSONDecodeError, psutil.Error) as exc:
                raise RuntimeError(
                    f"Could not inspect deployed extension for {session.session_id}: {exc}"
                ) from exc
        if len(matching_configs) != 1:
            raise RuntimeError(
                f"Expected exactly one deployed extension config for {session.session_id}"
            )
        config_path, config = next(iter(matching_configs.items()))
        try:
            controls_dir = Path(str(config["primeControlsDir"])).resolve()
            bridge_root = controls_dir.parent
            agent_dir = (bridge_root / "agent").resolve()
            binding = json.loads((controls_dir / "binding.json").read_text(encoding="utf-8"))
            binding_pid = binding["pid"]
            incarnation = binding["incarnation"]
        except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Selected Prime control binding is missing or malformed") from exc
        if not isinstance(binding_pid, int) or isinstance(binding_pid, bool) or binding_pid <= 0:
            raise RuntimeError("Selected Prime control binding pid is missing or malformed")
        if not isinstance(incarnation, str) or not incarnation:
            raise RuntimeError(
                "Selected Prime control binding incarnation is missing or malformed"
            )
        selected_processes: dict[int, psutil.Process] = {}
        for process in owned_prime_processes(self.runtime):
            try:
                configured_agent_dir = process.environ().get("PRIME_AGENT_CODING_AGENT_DIR")
                if not configured_agent_dir or Path(configured_agent_dir).resolve() != agent_dir:
                    continue
                selected_processes[process.pid] = process
            except (OSError, psutil.Error) as exc:
                raise RuntimeError(
                    f"Could not inspect selected Prime root process identity: {exc}"
                ) from exc
        for process in tuple(selected_processes.values()):
            try:
                for descendant in process.children(recursive=True):
                    selected_processes[descendant.pid] = descendant
            except psutil.Error as exc:
                raise RuntimeError(
                    f"Could not inspect selected Prime root descendants: {exc}"
                ) from exc
        processes: list[ProcessIdentity] = []
        for process in selected_processes.values():
            try:
                processes.append(
                    ProcessIdentity(
                        process.pid,
                        process.create_time(),
                        tuple(process.cmdline()),
                    )
                )
            except psutil.Error as exc:
                raise RuntimeError(
                    f"Could not inspect selected Prime root process identity: {exc}"
                ) from exc
        processes.sort(key=lambda process: process.pid)
        roles = qualify_selected_root_roles(
            tuple(processes),
            binding_pid=binding_pid,
            kernel_entry=str(self.requested_kernel.entry),
        )
        binding_processes = [process for process in processes if process.pid == binding_pid]
        if len(binding_processes) != 1:
            raise RuntimeError("Selected Prime control binding pid is not in the selected root")
        external_session_id = self.snapshot(session).get("external_session_id")
        if not isinstance(external_session_id, str) or not external_session_id:
            raise RuntimeError("Selected Omnigent session has no native external session identity")
        return SelectedRootIdentity(
            session_id=session.session_id,
            config_path=str(config_path),
            bridge_root=str(bridge_root),
            agent_dir=str(agent_dir),
            binding_pid=binding_pid,
            binding_started=binding_processes[0].started,
            binding_incarnation=incarnation,
            external_session_id=external_session_id,
            processes=tuple(processes),
            roles=roles,
        )

    def detach_and_resume(self, session: PrimeSession) -> None:
        with self.client() as client:
            response = client.get(
                f"/v1/sessions/{session.session_id}/resources/terminals/terminal_prime-native_main"
            )
            response.raise_for_status()
            metadata = response.json()["metadata"]
        session.before_detach = self.process_records()
        session.before_selected_root = self.selected_root_identity(session)
        if not session.before_detach:
            raise RuntimeError("No owned Prime process exists before detach")
        target = metadata["tmux_target"].split(":", 1)[0]
        detached = subprocess.run(
            ["tmux", "-S", metadata["tmux_socket"], "detach-client", "-s", target],
            capture_output=True,
            text=True,
            timeout=10,
        )
        write_json(
            self.evidence.root / "detach.json",
            {"argv": detached.args, "exit": detached.returncode, "stderr": detached.stderr},
        )
        if detached.returncode:
            raise RuntimeError("tmux did not detach the owned Prime attachment")
        session.terminal.expect(pexpect.EOF, timeout=15)
        session.terminal.close()
        terminal = pexpect.spawn(
            str(REPO / ".venv/bin/omnigent"),
            ["prime-native", "--server", str(self.url), "--resume", session.session_id],
            cwd=str(self.workspace),
            env=self.env(),
            encoding="utf-8",
            timeout=90,
            dimensions=(36, 120),
        )
        terminal.logfile_read = session.log_handle
        terminal.expect(r"Web UI: [^\r\n]*/c/([A-Za-z0-9_-]+)")
        if terminal.match.group(1) != session.session_id:
            raise RuntimeError("Resume selected a different Omnigent session")
        session.terminal = terminal
        after = self.process_records()
        after_selected_root = self.selected_root_identity(session)
        write_json(
            self.evidence.root / "reattach.json",
            {
                "raw": {"before": session.before_detach, "after": after},
                "selected": {
                    "before": selected_root_identity_record(session.before_selected_root),
                    "after": selected_root_identity_record(after_selected_root),
                },
            },
        )
        assert_same_selected_root_identity(session.before_selected_root, after_selected_root)

    def root_control_state(self, session: PrimeSession) -> dict:
        for process in owned_prime_processes(self.runtime):
            try:
                config_path = process.environ().get("OMNIGENT_EXTENSION_NATIVE_CONFIG")
                if not config_path or not Path(config_path).is_file():
                    continue
                config = json.loads(Path(config_path).read_text(encoding="utf-8"))
                if config.get("sessionId") != session.session_id:
                    continue
                binding = json.loads(
                    (Path(config["primeControlsDir"]) / "binding.json").read_text(encoding="utf-8")
                )
                return {
                    "config_path": config_path,
                    "session_id": config["sessionId"],
                    "controls_dir": config["primeControlsDir"],
                    "incarnation": binding["incarnation"],
                    "pid": binding["pid"],
                }
            except (KeyError, json.JSONDecodeError, OSError, psutil.Error):
                continue
        raise RuntimeError("Could not locate the owned root Prime control binding")

    def native_compaction_entries(self, summary: str) -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        configured_paths = set(self.prime_sessions.rglob("*.jsonl"))
        paths = configured_paths | self.fixture.conversation_logs
        for path in sorted(paths):
            if not path.is_file():
                continue
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") == "compaction" and summary in str(
                        entry.get("summary", "")
                    ):
                        matches.append(
                            {
                                "path": str(path),
                                "line": line_number,
                                "entry": entry,
                            }
                        )
        return matches

    def own_data_process_records(self) -> list[dict[str, object]]:
        exact_data_dir = str(self.runtime / "data")
        captured_pids = {
            process.pid
            for process in (
                self.server,
                self.mcp.process,
                *(session.terminal for session in self.sessions),
            )
            if process is not None
        }
        captured_descendants: set[int] = set()
        for pid in captured_pids:
            with contextlib.suppress(psutil.Error):
                captured_descendants.update(
                    child.pid for child in psutil.Process(pid).children(recursive=True)
                )
        records: list[dict[str, object]] = []
        for process in psutil.process_iter():
            if process.pid == os.getpid():
                continue
            captured = process.pid in captured_pids or process.pid in captured_descendants
            try:
                environment = process.environ()
                exact_owner = environment.get("OMNIGENT_DATA_DIR") == exact_data_dir
            except psutil.Error:
                if not captured:
                    continue
                environment = None
                exact_owner = False
            if not exact_owner and not captured:
                continue
            try:
                record: dict[str, object] = {
                    "pid": process.pid,
                    "started": process.create_time(),
                    "argv": process.cmdline(),
                    "ownership": "exact_data_dir" if exact_owner else "captured_descendant",
                }
            except psutil.Error as exc:
                record = {
                    "pid": process.pid,
                    "process_error": f"{type(exc).__name__}: {exc}",
                    "ownership": "exact_data_dir" if exact_owner else "captured_descendant",
                }
            if environment is None:
                record["environment_error"] = "Could not read process environment"
            records.append(record)
        return sorted(records, key=lambda record: int(record["pid"]))

    def own_host_registry(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        directory = self.runtime / "data" / "daemons"
        with contextlib.suppress(OSError):
            for path in sorted(directory.glob("*.json")):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    records.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
                    continue
                records.append({"path": str(path), "record": raw})
        return records

    def copy_owned_logs(self) -> list[dict[str, object]]:
        source_root = self.runtime / "data" / "logs"
        copied: list[dict[str, object]] = []
        if not source_root.is_dir():
            return copied
        destination_root = self.evidence.root / "owned-logs"
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(source_root)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            copied.append(
                {
                    "source": str(source),
                    "evidence": str(destination.relative_to(self.evidence.root)),
                    "bytes": destination.stat().st_size,
                }
            )
        return copied

    def stop_owned_host(self, steps: list[object], errors: list[object]) -> None:
        if self.url is None:
            errors.append("host stop: probe server URL is unavailable")
            return
        registry = self.own_host_registry()
        matching = [
            item["record"]
            for item in registry
            if isinstance(item.get("record"), dict) and item["record"].get("target") == self.url
        ]
        step: dict[str, object] = {"host_registry": registry}
        if not matching:
            step["host_stop"] = "skipped_no_matching_own_registry"
            steps.append(step)
            return
        if len(matching) != 1:
            errors.append("host stop: multiple matching own host registry records")
            step["host_stop"] = "skipped_ambiguous_registry"
            steps.append(step)
            return
        raw_record = matching[0]
        assert isinstance(raw_record, dict)
        try:
            qualified = qualify_owned_host_record(
                raw_record, self.url, self.runtime / "data", psutil.Process(int(raw_record["pid"]))
            )
        except (KeyError, OSError, RuntimeError, ValueError, psutil.Error) as exc:
            errors.append(f"host stop qualification: {type(exc).__name__}: {exc}")
            step["host_stop"] = "skipped_unqualified_registry"
            steps.append(step)
            return
        argv = [
            str(REPO / ".venv/bin/omnigent"),
            "host",
            "stop",
            "--server",
            self.url,
            "--daemon-only",
        ]
        try:
            result = subprocess.run(
                argv,
                cwd=self.workspace,
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            step["host_stop"] = {
                "qualified": qualified,
                "argv": argv,
                "cwd": str(self.workspace),
                "OMNIGENT_DATA_DIR": self.env()["OMNIGENT_DATA_DIR"],
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            if result.returncode:
                errors.append(f"host stop: exit {result.returncode}")
        except CLEANUP_ERRORS as exc:
            errors.append(f"host stop: {type(exc).__name__}: {exc}")
            step["host_stop"] = "failed"
        steps.append(step)

    def cleanup(self) -> tuple[bool, dict]:
        initial_owned_census = self.own_data_process_records()
        record: dict[str, object] = {
            "runtime": str(self.runtime),
            "steps": [],
            "errors": [],
            "initial_owned_census": initial_owned_census,
        }
        steps = record["steps"]
        errors = record["errors"]
        assert isinstance(steps, list)
        assert isinstance(errors, list)
        if any("process_error" in census for census in initial_owned_census):
            errors.append("initial own-data-dir census has an unreadable process identity")
        self.delete_owned_sessions(steps, errors)
        for session in self.sessions:
            try:
                session.terminal.close(force=True)
            except CLEANUP_ERRORS as exc:
                errors.append(f"terminal {session.session_id}: {type(exc).__name__}: {exc}")
            try:
                session.log_handle.close()
            except CLEANUP_ERRORS as exc:
                errors.append(f"terminal log {session.session_id}: {type(exc).__name__}: {exc}")
        self.stop_owned_host(steps, errors)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and owned_prime_processes(self.runtime):
                time.sleep(0.2)
            native_survivors = self.process_records()
            record["survivors_before_force"] = native_survivors
            forced_native_fallback = bool(native_survivors)
            record["forced_native_fallback"] = forced_native_fallback
            if forced_native_fallback:
                errors.append("Forced native fallback was required")
            for process in owned_prime_processes(self.runtime):
                with contextlib.suppress(psutil.Error):
                    process.terminate()
            _, alive = psutil.wait_procs(owned_prime_processes(self.runtime), timeout=5)
            for process in alive:
                with contextlib.suppress(psutil.Error):
                    process.kill()
            psutil.wait_procs(alive, timeout=5)
        except CLEANUP_ERRORS as exc:
            record["forced_native_fallback"] = True
            errors.append(f"Prime cleanup: {type(exc).__name__}: {exc}")
        if self.server is not None and self.server.poll() is None:
            try:
                self.server.terminate()
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    self.server.kill()
                    self.server.wait(timeout=5)
                except CLEANUP_ERRORS as exc:
                    errors.append(f"server force-stop: {type(exc).__name__}: {exc}")
            except CLEANUP_ERRORS as exc:
                errors.append(f"server: {type(exc).__name__}: {exc}")
        try:
            self.fixture.close()
        except CLEANUP_ERRORS as exc:
            errors.append(f"model fixture: {type(exc).__name__}: {exc}")
        try:
            self.mcp.close()
        except CLEANUP_ERRORS as exc:
            errors.append(f"MCP fixture: {type(exc).__name__}: {exc}")
        if self.server_log is not None:
            with contextlib.suppress(*CLEANUP_ERRORS):
                self.server_log.close()
        survivors = self.process_records()
        fresh_final_census = self.own_data_process_records()
        retained_initial = live_initial_process_records(initial_owned_census)
        final_by_identity = {
            (record.get("pid"), record.get("started")): record
            for record in [*fresh_final_census, *retained_initial]
        }
        final_owned_census = sorted(
            final_by_identity.values(), key=lambda record: int(record["pid"])
        )
        record["owned_survivors"] = survivors
        record["final_owned_census"] = final_owned_census
        if any("process_error" in census for census in final_owned_census):
            errors.append("final own-data-dir census has an unreadable process identity")
        try:
            record["copied_logs"] = self.copy_owned_logs()
        except OSError as exc:
            errors.append(f"copy own logs: {type(exc).__name__}: {exc}")
        record["server_returncode"] = self.server.poll() if self.server else None
        record["model_fixture_thread_alive"] = self.fixture.thread.is_alive()
        record["mcp_fixture_returncode"] = self.mcp.process.poll()
        write_json(self.evidence.root / "cleanup.json", record)
        ok = cleanup_is_complete(
            final_owned_census=final_owned_census,
            forced_native_fallback=bool(record.get("forced_native_fallback")),
            server_stopped=self.server is not None and self.server.poll() is not None,
            fixture_stopped=not self.fixture.thread.is_alive(),
            mcp_stopped=self.mcp.process.poll() is not None,
            errors=errors,
        )
        return ok, record

    def delete_owned_sessions(self, steps: list[object], errors: list[object]) -> None:
        if not (self.url and self.server and self.server.poll() is None):
            return
        for session_id in self.owned_session_ids:
            try:
                with self.client(timeout=15) as client:
                    response = client.delete(f"/v1/sessions/{session_id}")
                steps.append(
                    {
                        "delete_session": session_id,
                        "status": response.status_code,
                        "body": response.text,
                    }
                )
                response.raise_for_status()
            except CLEANUP_ERRORS as exc:
                errors.append(f"delete {session_id}: {type(exc).__name__}: {exc}")


def exercise(name: str, evidence: Evidence, body: Callable[[], None]) -> bool:
    try:
        body()
    except (
        OSError,
        RuntimeError,
        httpx.HTTPError,
        pexpect.ExceptionPexpect,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        AssertionError,
    ) as exc:
        evidence.failed(name, exc)
        return False
    evidence.verified(name)
    return True


def message_action(prompt: str) -> dict:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    }


def drive(run: AdapterRun) -> None:
    evidence = run.evidence
    if run.allowed is None:
        raise RuntimeError("Allowed session did not start")
    allowed = run.allowed

    def fresh_messages() -> None:
        http_prompt = f"ADAPTER_HTTP {uuid.uuid4().hex}"
        response = run.post_event("fresh_messages", allowed, message_action(http_prompt))
        if response.status_code != 202:
            raise RuntimeError(f"HTTP message admission must be 202, got {response.status_code}")
        if not run.fixture.admission_started.wait(30):
            raise RuntimeError("Remote model fixture did not observe admitted HTTP work")
        during = run.snapshot(allowed)
        before_items = run.items(allowed)
        if PONG_HTTP in assistant_text(before_items):
            raise RuntimeError("HTTP completion appeared before controlled model release")
        evidence.observe(
            "fresh_messages",
            "admitted_before_completion",
            {"status": during.get("status"), "completion_present": False},
        )
        run.fixture.admission_release.set()
        run.wait_for_reply(allowed, PONG_HTTP, http_prompt)
        after = run.wait_for_idle(allowed)
        evidence.observe(
            "fresh_messages",
            "completion_after_release",
            {"status": after.get("status"), "completion": PONG_HTTP},
        )
        terminal_prompt = f"ADAPTER_TUI {uuid.uuid4().hex}"
        run.send_tui("fresh_messages", allowed, terminal_prompt)
        run.wait_for_reply(allowed, PONG_TUI, terminal_prompt)
        allowed.terminal.expect(PONG_TUI, timeout=30)
        allowed.log_handle.flush()
        terminal_text = (evidence.root / "terminal-allowed.txt").read_text(errors="replace")
        if PONG_TUI not in terminal_text:
            raise RuntimeError("Native Prime TUI did not render its literal reply")
        evidence.observe("fresh_messages", "native_status", run.snapshot(allowed))

    def model_and_effort_controls() -> None:
        snapshot = run.snapshot(allowed)
        options = [option.get("id") for option in snapshot.get("model_options", [])]
        if options != ["verify/fixture-a", "verify/fixture-b"]:
            raise RuntimeError(f"Prime model catalog has unexpected identities: {options!r}")
        response = run.patch_session(
            "model_and_effort_controls",
            allowed,
            {"model_override": "verify/fixture-b", "reasoning_effort": "high"},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Public model/effort PATCH must be 200: {response.text}")
        observed = wait_for(
            lambda: (
                current
                if (current := run.snapshot(allowed)).get("llm_model") == "verify/fixture-b"
                and current.get("reasoning_effort") == "high"
                else None
            ),
            "public PATCH model and effort read-back",
        )
        evidence.observe("model_and_effort_controls", "patch_read_back", observed)
        invalid = run.patch_session(
            "model_and_effort_controls", allowed, {"model_override": "fixture-b"}
        )
        if invalid.status_code < 400:
            raise RuntimeError("Bare model identity unexpectedly succeeded")
        unsupported = run.patch_session(
            "model_and_effort_controls", allowed, {"reasoning_effort": "unsupported"}
        )
        if unsupported.status_code < 400:
            raise RuntimeError("Unsupported effort unexpectedly succeeded")
        run.send_tui("model_and_effort_controls", allowed, "/model")
        allowed.terminal.expect("fixture-a", timeout=30)
        allowed.terminal.send("fixture-a\r")
        tui_model = wait_for(
            lambda: (
                current
                if (current := run.snapshot(allowed)).get("llm_model") == "verify/fixture-a"
                else None
            ),
            "native TUI model picker projection",
        )
        run.send_tui("model_and_effort_controls", allowed, "/effort low")
        tui_effort = wait_for(
            lambda: (
                current
                if (current := run.snapshot(allowed)).get("reasoning_effort") == "low"
                else None
            ),
            "native TUI effort projection",
        )
        evidence.observe(
            "model_and_effort_controls",
            "native_tui_projection",
            {"model": tui_model, "effort": tui_effort},
        )

    def custom_prime_session() -> None:
        from omnigent.chat import _bundle_agent

        scenario = "custom_prime_session"
        bundle = _bundle_agent(run._agent_dir("custom", denied=False))
        host_snapshot = run.snapshot(allowed)
        host_id = host_snapshot.get("host_id")
        workspace = host_snapshot.get("workspace")
        if not isinstance(host_id, str) or not host_id:
            raise RuntimeError("The owned CLI session has no registered host")
        if not isinstance(workspace, str) or not workspace:
            raise RuntimeError("The owned CLI session has no host workspace")
        metadata = {
            "host_id": host_id,
            "workspace": workspace,
            "terminal_launch_args": [
                "--offline",
                "--provider",
                "verify",
                "--model",
                "fixture-a",
                "--no-context-files",
            ],
        }
        with run.client(timeout=60) as client:
            created = client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(metadata)},
                files={"bundle": ("custom.tar.gz", bundle, "application/gzip")},
            )
        evidence.action(
            scenario,
            "HTTP multipart POST /v1/sessions",
            {"purpose": "custom", "metadata": metadata, "bundle_bytes": len(bundle)},
            created,
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        run.owned_session_ids.append(session_id)

        def snapshot() -> dict:
            with run.client() as client:
                response = client.get(f"/v1/sessions/{session_id}")
                response.raise_for_status()
                return response.json()

        before = snapshot()
        if before["labels"].get("omnigent.wrapper") is not None:
            raise RuntimeError("Custom Prime session unexpectedly has a wrapper label")
        prompt = f"ADAPTER_FOLLOWUP {uuid.uuid4().hex}"
        expected_reply = f"{PONG_FOLLOWUP} {prompt.split()[-1]}"
        with run.client(timeout=60) as client:
            admitted = client.post(
                f"/v1/sessions/{session_id}/events", json=message_action(prompt)
            )
        evidence.action(
            scenario, "HTTP POST /v1/sessions/{id}/events", message_action(prompt), admitted
        )
        if admitted.status_code != 202:
            raise RuntimeError(f"Custom Prime message admission must be 202: {admitted.text}")

        def reply() -> object | None:
            run.fixture.raise_error()
            with run.client() as client:
                response = client.get(f"/v1/sessions/{session_id}/items")
                response.raise_for_status()
                items = response.json()
            write_json(evidence.root / "items-custom-latest.json", items)
            return (
                items
                if prompt in json.dumps(items) and expected_reply in assistant_text(items)
                else None
            )

        wait_for(reply, "label-free custom Prime literal assistant reply", 180)
        native = wait_for(
            lambda: (
                value
                if (value := snapshot()).get("external_session_id")
                and [option.get("id") for option in value.get("model_options", [])]
                == ["verify/fixture-a", "verify/fixture-b"]
                else None
            ),
            "label-free custom Prime native identity and catalog",
        )
        body = {
            "model_override": "verify/fixture-b",
            "reasoning_effort": "high",
            "title": "Custom Prime controls verified",
        }
        with run.client(timeout=30) as client:
            applied = client.patch(f"/v1/sessions/{session_id}", json=body)
        evidence.action(scenario, "HTTP PATCH /v1/sessions/{id}", body, applied)
        if applied.status_code != 200:
            raise RuntimeError(f"Custom Prime model/effort PATCH must be 200: {applied.text}")
        observed = wait_for(
            lambda: (
                value
                if (value := snapshot()).get("llm_model") == "verify/fixture-b"
                and value.get("reasoning_effort") == "high"
                and value.get("title") == body["title"]
                else None
            ),
            "label-free custom Prime observed settings and unrelated title",
        )
        if observed["labels"].get("omnigent.wrapper") is not None:
            raise RuntimeError("Custom Prime controls changed the presentation label")
        for rejected_body in (
            {"model_override": "fixture-b"},
            {"model_override": None},
            {"reasoning_effort": "low", "silent": True},
        ):
            with run.client(timeout=30) as client:
                rejected = client.patch(f"/v1/sessions/{session_id}", json=rejected_body)
            evidence.action(scenario, "HTTP PATCH /v1/sessions/{id}", rejected_body, rejected)
            if rejected.status_code != 409:
                raise RuntimeError(
                    f"Custom Prime unsupported control must be 409: {rejected.text}"
                )
        after = snapshot()
        if after.get("llm_model") != "verify/fixture-b" or after.get("reasoning_effort") != "high":
            raise RuntimeError("Rejected custom Prime controls changed observed settings")
        evidence.observe(scenario, "native_catalog", native)
        evidence.observe(scenario, "observed_settings_without_wrapper_label", after)

    def living_python_memory() -> None:
        seed = f"ADAPTER_MEMORY_SEED {uuid.uuid4().hex}"
        run.kernel_identity_path.unlink(missing_ok=True)
        response = run.post_event("living_python_memory", allowed, message_action(seed))
        if response.status_code != 202:
            raise RuntimeError(f"Seed admission must be 202, got {response.status_code}")
        run.wait_for_reply(allowed, "ADAPTER_MEMORY_SEEDED", seed, 180)
        kernels_before = [
            record for record in run.process_records() if "rlm.repl" in record["argv"]
        ]
        if not kernels_before:
            raise RuntimeError("No live Prime Python kernel followed the seed tool call")
        evidence.observe("living_python_memory", "owned_kernels_before", kernels_before)
        actual_kernel = read_actual_kernel_identity(run.kernel_identity_path)
        qualify_kernel_identity(
            run.requested_kernel,
            actual_kernel,
            run.nonce,
            kernels_before,
        )
        evidence.observe(
            "living_python_memory",
            "kernel_identity",
            {
                "requested": {
                    "entry": str(run.requested_kernel.entry),
                    "venv_root": str(run.requested_kernel.venv_root),
                    "rlm_module": str(run.requested_kernel.rlm_module),
                    "rlm_sha256": run.requested_kernel.rlm_sha256,
                },
                "actual": {
                    "nonce": actual_kernel.nonce,
                    "pid": actual_kernel.pid,
                    "executable": str(actual_kernel.executable),
                    "prefix": str(actual_kernel.prefix),
                    "rlm_module": str(actual_kernel.rlm_module),
                    "rlm_sha256": actual_kernel.rlm_sha256,
                },
            },
        )
        run.detach_and_resume(allowed)
        read = f"ADAPTER_MEMORY_READ {uuid.uuid4().hex}"
        run.send_tui("living_python_memory", allowed, read)
        run.wait_for_reply(allowed, "ADAPTER_MEMORY_READ", read, 180)
        kernels_after = [
            record for record in run.process_records() if "rlm.repl" in record["argv"]
        ]
        qualify_kernel_identity(
            run.requested_kernel,
            actual_kernel,
            run.nonce,
            kernels_after,
        )
        before_identity = [(item["pid"], item["started"]) for item in kernels_before]
        after_identity = [(item["pid"], item["started"]) for item in kernels_after]
        if after_identity != before_identity:
            raise RuntimeError("Detach and resume changed the live Python kernel identity")
        expected = run.args.expected_tool or f"{run.nonce} 42 True"
        observed = run.fixture.tool_results.get("memory_read", [])
        if observed != [expected]:
            raise RuntimeError(f"Literal memory read result differs: {observed!r}")
        write_json(
            evidence.root / "python-memory.json",
            {
                "nonce": run.nonce,
                "kernel_before": kernels_before,
                "kernel_after": kernels_after,
                "tool_results": run.fixture.tool_results,
                "tool_calls": run.fixture.calls,
            },
        )

    def mcp_tool() -> None:
        prompt = f"ADAPTER_MCP {uuid.uuid4().hex}"
        response = run.post_event("mcp_tool", allowed, message_action(prompt))
        if response.status_code != 202:
            raise RuntimeError(f"MCP admission must be 202, got {response.status_code}")
        run.wait_for_reply(allowed, MCP_RESULT, prompt, 180)
        if run.fixture.tool_results.get("mcp") != [MCP_RESULT]:
            raise RuntimeError("The exact MCP result did not reach the remote model fixture")
        calls = run.mcp.calls_path.read_text(encoding="utf-8")
        if MCP_RESULT not in calls:
            raise RuntimeError("The real MCP server did not record its literal invocation")
        evidence.observe("mcp_tool", "mcp_server_calls", calls.splitlines())

    def root_policy_denial() -> None:
        run.denied = run.create_session("denied", denied=True)
        denied = run.denied
        marker = run.workspace / "policy-marker.txt"
        marker.unlink(missing_ok=True)
        evidence.observe(
            "root_policy_denial",
            "claim_scope",
            "Root relayed sys_os_write policy gate only; not Python confinement.",
        )
        for origin in ("HTTP", "TUI"):
            prompt = f"ADAPTER_DENIED_WRITE {origin} {uuid.uuid4().hex}"
            if origin == "HTTP":
                response = run.post_event("root_policy_denial", denied, message_action(prompt))
                if response.status_code != 202:
                    raise RuntimeError(
                        f"Policy HTTP admission must be 202, got {response.status_code}"
                    )
            else:
                run.send_tui("root_policy_denial", denied, prompt)
            run.wait_for_reply(denied, "ADAPTER_POLICY_DENIED", prompt, 180)
            actual = "present" if marker.exists() else "absent"
            evidence.observe("root_policy_denial", f"{origin.lower()}_side_effect", actual)
            if actual != run.args.expected_side_effect:
                raise RuntimeError(
                    "Denied root write side effect differs: "
                    f"expected {run.args.expected_side_effect!r}, observed {actual!r}"
                )
        denials = run.fixture.tool_results.get("denial", [])
        if len(denials) != 2 or not all(POLICY_LITERAL in value for value in denials):
            raise RuntimeError(f"Both policy origins did not yield literal denial: {denials!r}")

    def compaction_interrupt_followup() -> None:
        for index in range(4):
            seed = f"ADAPTER_COMPACTION_SEED {index} {uuid.uuid4().hex}"
            response = run.post_event(
                "compaction_interrupt_followup", allowed, message_action(seed)
            )
            if response.status_code != 202:
                raise RuntimeError(
                    f"Compaction seed admission must be 202, got {response.status_code}"
                )
            run.wait_for_reply(allowed, f"{COMPACTION_SEED_REPLY} {index}", seed, 180)
        summary_count = len(run.fixture.summary_requests)
        compact = run.post_event("compaction_interrupt_followup", allowed, {"type": "compact"})
        if compact.status_code != 202:
            raise RuntimeError(f"Compact callback completion must be 202: {compact.text}")
        if compact.json().get("queued") is not False:
            raise RuntimeError(f"Compact did not report completed callback state: {compact.text}")
        summary_requests = run.fixture.summary_requests[summary_count:]
        if not summary_requests:
            raise RuntimeError(
                "Prime compaction did not issue a separately identified summary inference request"
            )
        summary = f"{COMPACTION_SUMMARY} {run.nonce}"
        entries = wait_for(
            lambda: run.native_compaction_entries(summary) or None,
            "native Prime compaction entry with literal summary",
            30,
        )
        if len(entries) != 1:
            raise RuntimeError(f"Expected one native compaction entry, observed {entries!r}")
        entry = entries[0]["entry"]
        if not isinstance(entry, dict):
            raise RuntimeError("Native compaction evidence has a malformed entry")
        if not entry.get("firstKeptEntryId"):
            raise RuntimeError(f"Native compaction entry lacks its real span: {entry!r}")
        if entry.get("fromHook") is not False:
            raise RuntimeError(f"Compaction entry was not native Prime output: {entry!r}")
        evidence.observe(
            "compaction_interrupt_followup",
            "compaction_configuration",
            {
                "enabled": False,
                "reserveTokens": 1024,
                "keepRecentTokens": 128,
                "auto_refine_enabled": False,
                "session_dir": str(run.prime_sessions),
            },
        )
        evidence.observe(
            "compaction_interrupt_followup",
            "summary_inference_requests",
            summary_requests,
        )
        evidence.observe("compaction_interrupt_followup", "native_compaction_entry", entries[0])
        stream_ready = threading.Event()
        interrupted = threading.Event()
        stream_errors: list[str] = []
        interrupted_payloads: list[dict] = []

        def watch_interrupt() -> None:
            try:
                with (
                    run.client(timeout=90) as client,
                    client.stream("GET", f"/v1/sessions/{allowed.session_id}/stream") as response,
                ):
                    response.raise_for_status()
                    event = ""
                    for line in response.iter_lines():
                        if line.startswith("event: "):
                            event = line.removeprefix("event: ").strip()
                            if event == "session.heartbeat":
                                stream_ready.set()
                        elif line.startswith("data: ") and event == "session.interrupted":
                            payload = json.loads(line.removeprefix("data: "))
                            if not isinstance(payload, dict) or not payload.get("data", {}).get(
                                "response_id"
                            ):
                                raise RuntimeError(
                                    "Native interrupted event lacks its root loop identity"
                                )
                            interrupted_payloads.append(payload)
                            interrupted.set()
                            return
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                stream_errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                stream_ready.set()

        watcher = threading.Thread(target=watch_interrupt, daemon=True)
        watcher.start()
        if not stream_ready.wait(10):
            raise RuntimeError("Public session stream did not become ready")
        if stream_errors:
            raise RuntimeError(stream_errors[0])
        sleep = f"ADAPTER_SLEEP {uuid.uuid4().hex}"
        admitted = run.post_event("compaction_interrupt_followup", allowed, message_action(sleep))
        if admitted.status_code != 202:
            raise RuntimeError(f"Sleep admission must be 202, got {admitted.status_code}")
        wait_for(
            lambda: (
                run.fixture.sleep_marker.read_text(encoding="utf-8") == "ADAPTER_SLEEP_STARTED"
                if run.fixture.sleep_marker.is_file()
                else False
            ),
            "real Python sleep side effect",
            60,
        )
        interrupt = run.post_event("compaction_interrupt_followup", allowed, {"type": "interrupt"})
        if interrupt.status_code != 202:
            raise RuntimeError(f"Interrupt admission must be 202: {interrupt.text}")
        if interrupt.json().get("status") != "interrupt_accepted":
            raise RuntimeError(f"Interrupt lacks literal acceptance: {interrupt.text}")
        if not interrupted.wait(30):
            raise RuntimeError("Public stream did not emit session.interrupted")
        evidence.observe(
            "compaction_interrupt_followup",
            "native_interrupt_event",
            interrupted_payloads[-1],
        )
        followup = f"ADAPTER_FOLLOWUP {uuid.uuid4().hex}"
        followup_response = run.post_event(
            "compaction_interrupt_followup", allowed, message_action(followup)
        )
        if followup_response.status_code != 202:
            raise RuntimeError(
                f"Follow-up admission must be 202, got {followup_response.status_code}"
            )
        run.wait_for_reply(allowed, f"{PONG_FOLLOWUP} {followup.split()[-1]}", followup, 180)
        evidence.observe(
            "compaction_interrupt_followup", "final_status", run.wait_for_idle(allowed)
        )
        stream_ready.clear()
        interrupted.clear()
        run.fixture.sleep_marker.unlink(missing_ok=True)
        terminal_watcher = threading.Thread(target=watch_interrupt, daemon=True)
        terminal_watcher.start()
        if not stream_ready.wait(10):
            raise RuntimeError("Terminal interruption stream did not become ready")
        if stream_errors:
            raise RuntimeError(stream_errors[0])
        terminal_sleep = f"ADAPTER_SLEEP {uuid.uuid4().hex}"
        run.send_tui("compaction_interrupt_followup", allowed, terminal_sleep)
        wait_for(
            lambda: (
                run.fixture.sleep_marker.read_text(encoding="utf-8") == "ADAPTER_SLEEP_STARTED"
                if run.fixture.sleep_marker.is_file()
                else False
            ),
            "terminal-origin real Python sleep side effect",
            60,
        )
        evidence.action("compaction_interrupt_followup", "native Prime TUI", {"key": "Ctrl+C"})
        allowed.terminal.sendcontrol("c")
        if not interrupted.wait(30):
            raise RuntimeError("Terminal abort did not emit session.interrupted")
        if (
            interrupted_payloads[-1]["data"]["response_id"]
            == interrupted_payloads[0]["data"]["response_id"]
        ):
            raise RuntimeError("Terminal interruption reused the earlier API loop identity")
        evidence.observe(
            "compaction_interrupt_followup",
            "native_terminal_interrupt_event",
            interrupted_payloads[-1],
        )
        terminal_followup = f"ADAPTER_FOLLOWUP {uuid.uuid4().hex}"
        run.send_tui("compaction_interrupt_followup", allowed, terminal_followup)
        run.wait_for_reply(
            allowed,
            f"{PONG_FOLLOWUP} {terminal_followup.split()[-1]}",
            terminal_followup,
            180,
        )
        evidence.observe(
            "compaction_interrupt_followup", "terminal_final_status", run.wait_for_idle(allowed)
        )

    def instructions_and_skill() -> None:
        run.fixture.skill_resource_marker.unlink(missing_ok=True)
        prompt = f"ADAPTER_SKILL {uuid.uuid4().hex}"
        response = run.post_event("instructions_and_skill", allowed, message_action(prompt))
        if response.status_code != 202:
            raise RuntimeError(f"Skill admission must be 202, got {response.status_code}")
        run.wait_for_reply(allowed, SKILL_RESULT, prompt, 180)
        instruction_observations = run.fixture.instruction_observations()
        missing_instructions = [
            name for name, value in instruction_observations.items() if not value["observed_roles"]
        ]
        if missing_instructions:
            raise RuntimeError(
                f"Required Prime instructions were absent: {missing_instructions!r}"
            )
        loaded = run.fixture.tool_results.get("skill_load", [])
        if len(loaded) != 1 or SKILL_LITERAL not in loaded[0]:
            raise RuntimeError(
                "Declared skill instructions did not reach Prime through load_skill"
            )
        if "- pyproject.toml" not in loaded[0]:
            raise RuntimeError("load_skill did not expose the Python skill declaration")
        skill_dir = run.agent_root / "allowed" / "skills" / "adapter-proof"
        pyproject = skill_dir / "pyproject.toml"
        package = skill_dir / "src" / "adapter_proof" / "__init__.py"
        if not pyproject.is_file() or not package.is_file():
            raise RuntimeError("The declared Python-backed skill package is incomplete")
        expected_source = skill_resource_source(
            run.nonce, run.fixture.skill_resource_marker
        ).strip()
        if run.fixture.tool_results.get("skill_resource_read") != [expected_source]:
            raise RuntimeError("read_skill_file did not return the exact bundled resource text")
        expected_result = f"{SKILL_RESOURCE_RESULT} {run.nonce}"
        if run.fixture.tool_results.get("skill_resource_execution") != [expected_result]:
            raise RuntimeError("The real ipython kernel did not execute the delivered resource")
        side_effect = run.fixture.skill_resource_marker.read_text(encoding="utf-8")
        if side_effect != run.nonce:
            raise RuntimeError(f"Skill resource side effect differs: {side_effect!r}")
        evidence.observe(
            "instructions_and_skill",
            "instruction_layers",
            instruction_observations,
        )
        evidence.observe(
            "instructions_and_skill",
            "python_skill_resource",
            {
                "skill_marker": SKILL_LITERAL,
                "python_declaration": {
                    "pyproject_sha256": sha256(pyproject),
                    "package_sha256": sha256(package),
                },
                "resource_path": SKILL_RESOURCE_PATH,
                "tool_calls": run.fixture.call_records[-3:],
                "read_result_format": "plain resource text",
                "read_result_sha256": hashlib.sha256(expected_source.encode()).hexdigest(),
                "kernel_result": expected_result,
                "side_effect_path": str(run.fixture.skill_resource_marker),
                "side_effect": side_effect,
            },
        )

    def rlm_child_root_ownership() -> None:
        before_snapshot = run.snapshot(allowed)
        before_control = run.root_control_state(allowed)
        before_text = assistant_text(run.items(allowed))
        if PONG_HTTP not in before_text or SKILL_RESULT not in before_text:
            raise RuntimeError("Root transcript lacks required pre-child literals")
        run.fixture.child_marker.unlink(missing_ok=True)
        prompt = f"ADAPTER_RLM_ROOT {run.nonce}"
        response = run.post_event("rlm_child_root_ownership", allowed, message_action(prompt))
        if response.status_code != 202:
            raise RuntimeError(f"RLM root admission must be 202, got {response.status_code}")
        run.wait_for_reply(allowed, RLM_ROOT_COMPLETE, prompt, 240)
        after_snapshot = run.wait_for_idle(allowed, 120)
        after_control = run.root_control_state(allowed)
        after_text = assistant_text(run.items(allowed))
        child_file = run.fixture.child_marker.read_text(encoding="utf-8")
        if child_file != run.nonce:
            raise RuntimeError(f"Child ipython nonce file differs: {child_file!r}")
        external_session_id = before_snapshot.get("external_session_id")
        if not isinstance(external_session_id, str) or not external_session_id:
            raise RuntimeError("Root external session identity is empty")
        if external_session_id != after_snapshot.get("external_session_id"):
            raise RuntimeError("Child RLM overwrote the root external session identity")
        incarnation = before_control.get("incarnation")
        if not isinstance(incarnation, str) or not incarnation:
            raise RuntimeError("Root control incarnation is empty")
        if incarnation != after_control["incarnation"]:
            raise RuntimeError("Child RLM overwrote the root control incarnation")
        if run.fixture.child_task_requests != [
            f"[task from parent]\n\nADAPTER_RLM_CHILD {run.nonce}"
        ]:
            raise RuntimeError(
                f"Real child dispatch inputs differ: {run.fixture.child_task_requests!r}"
            )
        expected_child_result = f"ADAPTER_RLM_CHILD_TOOL_RESULT {run.nonce}"
        if run.fixture.tool_results.get("rlm_child") != [expected_child_result]:
            raise RuntimeError("Real child did not complete its ipython tool execution")
        if not run.fixture.child_notification_inputs:
            raise RuntimeError("Root did not receive the documented child-exited notification")
        if PONG_HTTP not in after_text or SKILL_RESULT not in after_text:
            raise RuntimeError("Child RLM overwrote the root transcript")
        if RLM_CHILD_PRIVATE_REPLY in after_text:
            raise RuntimeError("Child private reply leaked into the root transcript")
        if RLM_ROOT_COMPLETE not in after_text or after_snapshot.get("status") != "idle":
            raise RuntimeError("Root did not settle with its literal completion")
        evidence.observe(
            "rlm_child_root_ownership",
            "root_state",
            {
                "before_external_session_id": external_session_id,
                "after_external_session_id": after_snapshot.get("external_session_id"),
                "before_control": before_control,
                "after_control": after_control,
                "child_file": child_file,
                "child_tool_result": expected_child_result,
                "child_notification_inputs": run.fixture.child_notification_inputs,
                "child_private_reply_in_root_assistant_projection": False,
                "child_private_reply_in_root_notification_input": any(
                    RLM_CHILD_PRIVATE_REPLY in value
                    for value in run.fixture.child_notification_inputs
                ),
                "root_status": after_snapshot.get("status"),
            },
        )

    if not exercise("fresh_messages", evidence, fresh_messages):
        return
    if not exercise("model_and_effort_controls", evidence, model_and_effort_controls):
        return
    exercise("custom_prime_session", evidence, custom_prime_session)
    memory_ok = exercise("living_python_memory", evidence, living_python_memory)
    if not memory_ok and run.args.expected_tool is not None:
        return
    mcp_ok = exercise("mcp_tool", evidence, mcp_tool)
    if not mcp_ok and run.args.expected_mcp is not None:
        return
    policy_ok = exercise("root_policy_denial", evidence, root_policy_denial)
    if not policy_ok and run.args.expected_side_effect != "absent":
        return
    exercise("compaction_interrupt_followup", evidence, compaction_interrupt_followup)
    exercise("instructions_and_skill", evidence, instructions_and_skill)
    exercise("rlm_child_root_ownership", evidence, rlm_child_root_ownership)


def source_identity(
    args: argparse.Namespace, snapshot: SourceSnapshot
) -> tuple[dict[str, object], RequestedKernel]:
    if os.environ.get("PYTHONPATH") != str(REPO):
        raise SourceIntegrityError(
            "The probe process did not receive the managed checkout PYTHONPATH"
        )
    imports = import_witnesses()
    prime = args.prime_path
    if not prime.is_file() or sha256(prime) != PRIME_SHA256:
        raise RuntimeError("Prime binary does not match the published 0.9.6 artifact")
    version = subprocess.check_output([str(prime), "--version"], text=True).strip()
    if version != "0.9.6":
        raise RuntimeError(f"Published Prime version differs: {version!r}")
    requested_kernel, rlm_probe = requested_kernel_identity(args.kernel_python)
    application_sources = {}
    for relative in (
        "package.json",
        "README.md",
        "CHANGELOG.md",
        "docs/compaction.md",
        "docs/keybindings.md",
        "docs/rlm.md",
        "docs/rlm-runtime.md",
        "docs/session-format.md",
        "docs/settings.md",
        "docs/skills.md",
        "docs/extensions.md",
    ):
        path = prime.parent / relative
        if not path.is_file():
            raise RuntimeError(f"Published Prime source artifact is absent: {path}")
        application_sources[relative] = {
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
    return {
        "repo": str(REPO),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "prime": str(prime),
        "prime_sha256": sha256(prime),
        "prime_version": version,
        "prime_source_commit": PRIME_SOURCE_COMMIT,
        "prime_source_url": (
            "https://github.com/PrimeIntellect-ai/prime-agent/tree/" + PRIME_SOURCE_COMMIT
        ),
        "prime_application_sources": application_sources,
        "kernel_python_entry": str(requested_kernel.entry),
        "kernel_rlm_api": rlm_probe,
        "imports": imports,
        "source_snapshot": asdict(snapshot),
        "probe_environment": {"PYTHONPATH": os.environ["PYTHONPATH"]},
    }, requested_kernel


def write_manifest(evidence: Evidence, exit_status: int) -> None:
    inventory = {
        str(path.relative_to(evidence.root)): {
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted(evidence.root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    if "final-report.md" not in inventory:
        raise RuntimeError("Final report is absent from the evidence inventory")
    write_json(
        evidence.root / "manifest.json",
        {
            "exit": exit_status,
            "final_verdict": evidence.final_verdict,
            "evidence": str(evidence.root),
            "artifacts": inventory,
        },
    )


def serve_mcp(port: int, calls: Path) -> int:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "adapter-fixture",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )

    @server.tool()
    def echo(value: str) -> str:
        """Return and record one literal verification value."""
        append_json(calls, {"tool": "echo", "value": value})
        return value

    server.run(transport="streamable-http")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--prime-path", type=Path)
    result.add_argument("--kernel-python", type=Path)
    result.add_argument("--production-ready", type=Path)
    result.add_argument("--evidence-dir", type=Path)
    result.add_argument(
        "--expected-tool",
        help="Override the literal memory-read result as a negative control.",
    )
    result.add_argument(
        "--expected-mcp",
        help="Override the literal MCP result as a negative control.",
    )
    result.add_argument(
        "--expected-side-effect",
        choices=("absent", "present"),
        default="absent",
        help="Expected denied-write marker state; 'present' is a negative control.",
    )
    result.add_argument("--serve-mcp", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--mcp-port", type=int, help=argparse.SUPPRESS)
    result.add_argument("--mcp-calls", type=Path, help=argparse.SUPPRESS)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.serve_mcp:
        if args.mcp_port is None or args.mcp_calls is None:
            raise SystemExit("--serve-mcp requires --mcp-port and --mcp-calls")
        return serve_mcp(args.mcp_port, args.mcp_calls)
    if args.prime_path is None or args.kernel_python is None:
        raise SystemExit("--prime-path and --kernel-python are required")
    root = args.evidence_dir or Path(tempfile.mkdtemp(prefix="prime-adapter-proof-", dir="/tmp"))
    evidence = Evidence(root)
    run: AdapterRun | None = None
    before_sources: SourceSnapshot | None = None
    integrity: dict[str, object] | None = None
    try:
        before_sources = source_snapshot()
        write_json(evidence.root / "source-before.json", asdict(before_sources))
        artifact, requested_kernel = source_identity(args, before_sources)
        write_json(evidence.root / "artifact.json", artifact)
        if args.production_ready is not None:
            if not args.production_ready.is_file():
                raise RuntimeError(
                    f"Optional production readiness receipt is absent: {args.production_ready}"
                )
            artifact["production_ready"] = json.loads(
                args.production_ready.read_text(encoding="utf-8")
            )
            artifact["production_ready_path"] = str(args.production_ready)
            write_json(evidence.root / "artifact.json", artifact)
        run = AdapterRun(args, evidence, requested_kernel)
        run.start()
        drive(run)
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        pexpect.ExceptionPexpect,
        json.JSONDecodeError,
    ) as exc:
        evidence.record_fatal(exc)
        print(f"FAIL {exc}", flush=True)
    finally:
        if run is None:
            evidence.observe("owned_cleanup", "run_not_started", True)
        else:
            try:
                ok, cleanup = run.cleanup()
                evidence.observe("owned_cleanup", "cleanup", cleanup)
                if ok:
                    evidence.verified("owned_cleanup")
                else:
                    evidence.failed(
                        "owned_cleanup", RuntimeError("Owned resources or cleanup errors remain")
                    )
            except FINALIZATION_ERRORS as exc:
                evidence.record_fatal(exc)
                evidence.failed("owned_cleanup", exc)
        try:
            after_sources = source_snapshot()
            comparison = (
                compare_source_snapshots(before_sources, after_sources)
                if before_sources is not None
                else None
            )
            integrity = {
                "before": asdict(before_sources) if before_sources is not None else None,
                "after": asdict(after_sources),
                "comparison": comparison,
                "deleted": [
                    {**asdict(artifact), "deleted": True}
                    for artifact in (before_sources.by_path().values() if before_sources else ())
                    if artifact.path not in after_sources.by_path()
                ],
            }
            write_json(evidence.root / "source-after.json", asdict(after_sources))
            write_json(evidence.root / "source-integrity.json", integrity)
            if before_sources is None:
                raise SourceIntegrityError("Source integrity has no pre-launch snapshot")
            assert_same_sources(before_sources, after_sources)
        except FINALIZATION_ERRORS as exc:
            evidence.record_fatal(exc)
            if integrity is None:
                integrity = {
                    "before": asdict(before_sources) if before_sources is not None else None,
                }
            integrity["error"] = {"type": type(exc).__name__, "error": str(exc)}
            write_json(evidence.root / "source-integrity.json", integrity)
        evidence.persist()
        evidence.write_report()
        status = 0 if evidence.final_verdict == "VERIFIED" else 1
        write_manifest(evidence, status)
        for name, scenario in evidence.scenarios.items():
            print(f"{scenario.verdict} {name}", flush=True)
        print(f"{evidence.final_verdict} final", flush=True)
        print(f"Evidence {evidence.root}", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())

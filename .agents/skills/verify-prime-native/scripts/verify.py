#!/usr/bin/env -S uv run --no-sync python
"""Drive the real Prime Native CLI and HTTP message path in isolated state."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pexpect
import psutil
import yaml

from omnigent.harnesses.prime_native.bridge import bridge_roots
from omnigent.harnesses.prime_native.process import MINIMUM_VERSION, meets_minimum_version

REPO = Path(__file__).resolve().parents[4]
PONG = "PRIME_NATIVE_PONG"


def doctor(prime: str, evidence: Path, env: dict[str, str]) -> None:
    checks: list[dict[str, object]] = []
    results: dict[str, object] = {
        "checkout": str(REPO),
        "interpreter": sys.executable,
        "python_version": sys.version,
        "prime_binary": str(Path(prime).resolve()),
        "checks": checks,
    }
    for name, argv, required in (
        ("prime-version", [prime, "--version"], f">={MINIMUM_VERSION}"),
        ("prime-help", [prime, "--help"], "model"),
        ("prime-model-help", [prime, "model", "list", "--help"], "List available models"),
        ("native-help", [sys.executable, "-m", "omnigent", "prime-native", "--help"], "--server"),
    ):
        check: dict[str, object] = {"name": name, "argv": argv, "required": required}
        checks.append(check)
        output = None
        try:
            result = subprocess.run(
                argv, env=env, cwd=REPO, capture_output=True, text=True, timeout=30
            )
            valid_output = (
                meets_minimum_version(result.stdout.strip())
                if name == "prime-version"
                else required in result.stdout
            )
            check["exit"] = result.returncode
            check["outcome"] = (
                "nonzero-exit"
                if result.returncode
                else "passed"
                if valid_output
                else "missing-marker"
            )
            output = result.stdout[:32768] + result.stderr[:32768]
            if check["outcome"] != "passed":
                raise RuntimeError(f"Doctor failed for {name}, {check['outcome']}")
        except subprocess.TimeoutExpired as exc:
            check["outcome"] = "timeout"
            output = "".join(
                (value.decode(errors="replace") if isinstance(value, bytes) else value or "")[
                    :32768
                ]
                for value in (exc.stdout, exc.stderr)
            )
            raise
        except OSError:
            check["outcome"] = "os-error"
            raise
        finally:
            try:
                if output is not None:
                    (evidence / f"{name}.txt").write_text(output)
            except OSError as exc:
                check["output_write_error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                (evidence / "doctor.json").write_text(json.dumps(results, indent=2))
    revision_argv = ["git", "rev-parse", "HEAD"]
    revision_check: dict[str, object] = {"argv": revision_argv}
    results["revision_check"] = revision_check
    try:
        results["revision"] = subprocess.check_output(
            revision_argv, cwd=REPO, env=env, text=True, timeout=30
        ).strip()
        revision_check.update({"exit": 0, "outcome": "passed"})
    except subprocess.CalledProcessError as exc:
        revision_check.update({"exit": exc.returncode, "outcome": "nonzero-exit"})
        raise
    except subprocess.TimeoutExpired:
        revision_check["outcome"] = "timeout"
        raise
    except OSError:
        revision_check["outcome"] = "os-error"
        raise
    finally:
        (evidence / "doctor.json").write_text(json.dumps(results, indent=2))


def run_doctor(prime_path: str) -> int:
    evidence = Path(tempfile.mkdtemp(prefix="prime-native-verify-", dir="/tmp"))
    scratch = Path(tempfile.mkdtemp(prefix="prime-native-doctor-", dir="/tmp"))
    status = 1
    try:
        env = {"PATH": os.defpath, "TERM": "xterm-256color", "BROWSER": "false"}
        for key, directory in (
            ("HOME", "home"),
            ("TMPDIR", "tmp"),
            ("OMNIGENT_CONFIG_HOME", "config"),
            ("OMNIGENT_DATA_DIR", "data"),
            ("PRIME_AGENT_CODING_AGENT_DIR", "prime"),
            ("XDG_CONFIG_HOME", "xdg-config"),
            ("XDG_DATA_HOME", "xdg-data"),
            ("XDG_CACHE_HOME", "xdg-cache"),
        ):
            path = scratch / directory
            path.mkdir()
            env[key] = str(path)
        prime = shutil.which(prime_path)
        if prime is None:
            raise RuntimeError("The requested Prime binary is unavailable")
        doctor(prime, evidence, env)
        shutil.rmtree(scratch)
        status = 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        (evidence / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
        print(f"FAIL {exc}")
    (evidence / "manifest.json").write_text(
        json.dumps(
            {
                "exit": status,
                "doctor_only": True,
                "evidence": str(evidence),
                "scratch": str(scratch),
                "scratch_removed": not scratch.exists(),
            },
            indent=2,
        )
    )
    print(f"Evidence {evidence}")
    return status


def wait_for(check, description: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}")


def pong(_request: dict) -> list[tuple[dict, str | None]]:
    return [({"role": "assistant", "content": PONG}, None), ({}, "stop")]


def make_model(evidence: Path, respond=pong) -> ThreadingHTTPServer:
    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with (evidence / "model-requests.jsonl").open("a") as handle:
                handle.write(json.dumps(request) + "\n")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            # Distinct per completion, like real providers; Omnigent dedupes replies by it.
            completion_id = f"verify-completion-{uuid.uuid4().hex}"
            for delta, reason in respond(request):
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def assistant_text(value: object) -> str:
    if isinstance(value, dict):
        if value.get("role") == "assistant":
            return "".join(
                block.get("text", "")
                for block in value.get("content", [])
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        return "".join(assistant_text(child) for child in value.values())
    if isinstance(value, list):
        return "".join(assistant_text(child) for child in value)
    return ""


def owned_prime_processes(runtime: Path) -> list[psutil.Process]:
    owned = []
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        try:
            if process.uids().real != os.getuid():
                continue
            if (
                process.name() == "tmux"
                or "omnigent.harnesses.prime_native.process" in process.cmdline()
            ):
                continue
            process_env = process.environ()
            directory = process_env.get("PRIME_AGENT_CODING_AGENT_DIR", "")
            data_dir = process_env.get("OMNIGENT_DATA_DIR", "")
            if directory and Path(directory).resolve() == (runtime / "prime").resolve():
                continue
            if (directory and Path(directory).resolve().is_relative_to(runtime.resolve())) or (
                directory and data_dir and Path(data_dir).resolve() == (runtime / "data").resolve()
            ):
                owned.append(process)
        except (psutil.Error, OSError, SystemError):
            continue
    return owned


def write_prime_config(runtime: Path, model: ThreadingHTTPServer) -> None:
    prime_config = runtime / "prime"
    prime_config.mkdir()
    (prime_config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "verify": {
                        "baseUrl": f"http://127.0.0.1:{model.server_port}/v1",
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
            }
        )
    )
    (prime_config / "settings.json").write_text(
        json.dumps(
            {
                "onboardingShown": True,
                "defaultProvider": "verify",
                "defaultModel": "fixture",
            }
        )
    )


def tmux(metadata: dict, *args: str) -> str:
    return subprocess.run(
        ["tmux", "-S", metadata["tmux_socket"], *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout


def pane(metadata: dict) -> str:
    return tmux(metadata, "capture-pane", "-p", "-t", metadata["tmux_target"])


def type_into_prime(metadata: dict, text: str, *, submit: bool = True) -> None:
    tmux(metadata, "send-keys", "-t", metadata["tmux_target"], "-l", text)
    if submit:
        tmux(metadata, "send-keys", "-t", metadata["tmux_target"], "Enter")


def keep_bridge_log(session: str, runtime: Path, evidence: Path) -> None:
    """Copy this session's bridge.log into the evidence before session deletion removes it."""
    roots = [*(runtime / "data" / "prime-native").glob("*"), *bridge_roots()[1].glob("*")]
    for root in roots:
        with contextlib.suppress(OSError, ValueError, KeyError):
            if json.loads((root / "config.json").read_text())["sessionId"] == session:
                shutil.copy(root / "bridge.log", evidence / "bridge.log")


def switch_chats(
    client: httpx.Client,
    session: str,
    metadata: dict,
    evidence: Path,
    runtime: Path,
    original_prompt: str,
) -> None:
    """Move the terminal to a new Prime chat and back; the web session must stay on its chat."""
    external_id = client.get(f"/v1/sessions/{session}").json()["external_session_id"]
    prompt = f"Queued while away {uuid.uuid4().hex}. Reply with {PONG}."
    model_requests = evidence / "model-requests.jsonl"

    def items() -> object:
        response = client.get(f"/v1/sessions/{session}/items")
        response.raise_for_status()
        return response.json()

    def notices(code: str) -> int:
        return json.dumps(items()).count(f'"code": "{code}"')

    def replies() -> int:
        return assistant_text(items()).count(PONG)

    def external_id_unchanged() -> None:
        current = client.get(f"/v1/sessions/{session}").json()["external_session_id"]
        if current != external_id:
            raise RuntimeError(f"external_session_id changed from {external_id} to {current}")

    type_into_prime(metadata, "/new")
    wait_for(lambda: notices("prime_terminal_detached") == 1, "the chat-switch notice")
    (evidence / "pane-after-new.txt").write_text(pane(metadata))
    action = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    }
    client.post(f"/v1/sessions/{session}/events", json=action).raise_for_status()
    time.sleep(4)
    (evidence / "pane-while-away.txt").write_text(pane(metadata))
    if prompt in model_requests.read_text() or prompt in pane(metadata):
        raise RuntimeError("A web message reached the Prime chat the web session does not mirror")
    if replies() != 2 or notices("prime_terminal_detached") != 1:
        raise RuntimeError(
            "The other Prime chat changed the web session while the terminal was away"
        )
    external_id_unchanged()

    type_into_prime(metadata, "/resume")
    wait_for(lambda: "Search sessions" in pane(metadata), "the resume picker")
    type_into_prime(metadata, original_prompt[:24], submit=False)
    time.sleep(1)
    (evidence / "pane-resume-picker.txt").write_text(pane(metadata))
    tmux(metadata, "send-keys", "-t", metadata["tmux_target"], "Enter")
    wait_for(lambda: notices("prime_terminal_returned") == 1, "the return notice")
    wait_for(lambda: replies() == 3, "the queued message's reply")
    time.sleep(3)
    (evidence / "switch-items.json").write_text(json.dumps(items(), indent=2))
    if (
        replies() != 3
        or json.dumps(items()).count(prompt) != 1
        or model_requests.read_text().count(prompt) != 1
    ):
        raise RuntimeError(
            "The queued web message was not delivered exactly once after the return"
        )
    if notices("prime_terminal_detached") != 1 or notices("prime_terminal_returned") != 1:
        raise RuntimeError("The chat-switch notices were not posted exactly once each")
    external_id_unchanged()
    keep_bridge_log(session, runtime, evidence)


def drive(
    prime: str,
    evidence: Path,
    runtime: Path,
    env: dict[str, str],
    expected: str,
    inspect_seconds: int = 0,
) -> None:
    model = make_model(evidence)
    write_prime_config(runtime, model)
    prime_config = runtime / "prime"
    env["PRIME_AGENT_CODING_AGENT_DIR"] = str(prime_config)
    env["OMNIGENT_PRIME_PATH"] = prime
    help_text = subprocess.run(
        [prime, "--help"], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    omni = str(REPO / ".venv/bin/omnigent")
    process = None
    terminal = None
    terminal_log = None
    session = None
    try:
        with (evidence / "server.log").open("w") as server_log:
            process = subprocess.Popen(
                [
                    omni,
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--database-uri",
                    f"sqlite:///{runtime}/data/server.db",
                    "--artifact-location",
                    str(runtime / "artifacts"),
                ],
                cwd=runtime / "workspace",
                env=env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            with httpx.Client(base_url=url, timeout=5) as client:

                def healthy():
                    if process.poll() is not None:
                        raise RuntimeError("Omnigent server exited before readiness")
                    try:
                        return client.get("/health").status_code == 200
                    except httpx.HTTPError:
                        return False

                wait_for(healthy, "owned server /health")
                argv = [
                    "prime-native",
                    "--server",
                    url,
                    "--",
                    "--offline",
                    "--provider",
                    "verify",
                    "--model",
                    "fixture",
                    # Prime 0.10 has no --no-tools and rejects unknown options.
                    *(["--no-tools"] if "--no-tools" in help_text else []),
                    "--no-context-files",
                ]
                (evidence / "launch.json").write_text(
                    json.dumps({"argv": [omni, *argv], "url": url})
                )
                terminal = pexpect.spawn(
                    omni,
                    argv,
                    cwd=str(runtime / "workspace"),
                    env=env,
                    encoding="utf-8",
                    timeout=120,
                    dimensions=(32, 120),
                )
                terminal_log = (evidence / "terminal.txt").open("w")
                terminal.logfile_read = terminal_log
                terminal.expect(r"Web UI: [^\r\n]*/c/([A-Za-z0-9_-]+)")
                session = terminal.match.group(1)
                (evidence / "session.json").write_text(
                    json.dumps({"session_id": session, "url": url})
                )
                prompt = f"Verification request {uuid.uuid4().hex}. Reply with {PONG}."
                action = {
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                }
                (evidence / "action.json").write_text(json.dumps(action, indent=2))
                response = client.post(f"/v1/sessions/{session}/events", json=action)
                response.raise_for_status()

                def answered():
                    with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
                        terminal.read_nonblocking(65536, timeout=0.05)
                    response = client.get(f"/v1/sessions/{session}/items")
                    response.raise_for_status()
                    items = response.json()
                    (evidence / "items.json").write_text(json.dumps(items, indent=2))
                    text = assistant_text(items)
                    if text:
                        return items
                    return None

                items = wait_for(answered, "Prime assistant reply")
                if assistant_text(items).strip() != expected:
                    raise RuntimeError("Assistant reply differs from the literal expected value")
                if prompt not in json.dumps(items):
                    raise RuntimeError("Submitted user message is absent from the transcript")
                if not (evidence / "model-requests.jsonl").is_file():
                    raise RuntimeError("The real Prime worker made no model request")
                requests = (evidence / "model-requests.jsonl").read_text().splitlines()
                if not any(
                    prompt in json.dumps(json.loads(line).get("messages")) for line in requests
                ):
                    raise RuntimeError("The submitted prompt never reached the model endpoint")
                native_prompt = f"Terminal request {uuid.uuid4().hex}. Reply with {PONG}."
                (evidence / "terminal-action.json").write_text(
                    json.dumps({"text": native_prompt, "key": "Enter"})
                )
                terminal.send(native_prompt + "\r")

                def terminal_answered():
                    current = answered()
                    if (
                        current
                        and native_prompt in json.dumps(current)
                        and assistant_text(current).strip() == PONG * 2
                    ):
                        return current
                    return None

                terminal_items = wait_for(terminal_answered, "terminal-originated Prime reply")
                (evidence / "terminal-items.json").write_text(json.dumps(terminal_items, indent=2))
                response = client.get(f"/v1/sessions/{session}")
                response.raise_for_status()
                snapshot = response.json()
                (evidence / "session-snapshot.json").write_text(json.dumps(snapshot, indent=2))
                if not snapshot.get("external_session_id"):
                    raise RuntimeError("Prime never reported its native session identity")
                if snapshot.get("harness") != "prime-native" or [
                    option["id"] for option in snapshot.get("model_options", [])
                ] != ["verify/fixture"]:
                    raise RuntimeError(
                        "The session did not expose Prime's configured model catalog"
                    )
                resource = client.get(
                    f"/v1/sessions/{session}/resources/terminals/terminal_prime-native_main"
                )
                resource.raise_for_status()
                metadata = resource.json()["metadata"]
                if "--extension" not in help_text:
                    switch_chats(client, session, metadata, evidence, runtime, prompt)
                else:
                    print("SKIP chat switch: Prime 0.9.x runs the extension adapter")
                before = sorted(process.pid for process in owned_prime_processes(runtime))
                if not before:
                    raise RuntimeError("No owned Prime process exists before detach")
                detached = subprocess.run(
                    [
                        "tmux",
                        "-S",
                        metadata["tmux_socket"],
                        "detach-client",
                        "-s",
                        metadata["tmux_target"].split(":", 1)[0],
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                (evidence / "detach.json").write_text(
                    json.dumps({"argv": detached.args, "exit": detached.returncode})
                )
                terminal.expect(pexpect.EOF, timeout=15)
                terminal.close()
                resume_args = ["prime-native", "--server", url, "--resume", session]
                terminal = pexpect.spawn(
                    omni,
                    resume_args,
                    cwd=str(runtime / "workspace"),
                    env=env,
                    encoding="utf-8",
                    timeout=60,
                    dimensions=(32, 120),
                )
                terminal.logfile_read = terminal_log
                terminal.expect(r"Web UI: [^\r\n]*/c/([A-Za-z0-9_-]+)")
                if terminal.match.group(1) != session:
                    raise RuntimeError("Resume attached a different Omnigent conversation")
                resumed = client.get(f"/v1/sessions/{session}")
                resumed.raise_for_status()
                after = sorted(process.pid for process in owned_prime_processes(runtime))
                if (
                    resumed.json().get("external_session_id") != snapshot["external_session_id"]
                    or after != before
                ):
                    raise RuntimeError("Detach and reattach changed the live Prime session")
                (evidence / "reattach.json").write_text(
                    json.dumps(
                        {
                            "argv": [omni, *resume_args],
                            "external_session_id": snapshot["external_session_id"],
                            "owned_pids_before": before,
                            "owned_pids_after": after,
                        },
                        indent=2,
                    )
                )
                (evidence / "result.json").write_text(
                    json.dumps(
                        {
                            "scenario": "messages.http-and-terminal-with-reattach",
                            "result": "PASS",
                            "session_id": session,
                            "expected": expected,
                            "model_boundary": "local fixture",
                        },
                        indent=2,
                    )
                )
                if inspect_seconds:
                    print(f"Inspect {url}/c/{session} for {inspect_seconds} seconds", flush=True)
                    time.sleep(inspect_seconds)
    finally:
        cleanup_error = None
        if session is not None and process is not None and process.poll() is None:
            try:
                response = httpx.delete(f"{url}/v1/sessions/{session}", timeout=30)
                (evidence / "session-delete.json").write_text(
                    json.dumps({"status": response.status_code, "body": response.text}, indent=2)
                )
                response.raise_for_status()
                wait_for(lambda: not owned_prime_processes(runtime), "owned Prime shutdown", 10)
            except (httpx.HTTPError, RuntimeError) as exc:
                cleanup_error = exc
                (evidence / "prime-shutdown-survivors.json").write_text(
                    json.dumps(
                        [
                            {"pid": owned.pid, "name": owned.name(), "argv": owned.cmdline()}
                            for owned in owned_prime_processes(runtime)
                        ],
                        indent=2,
                    )
                )
        if terminal is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                attachment = psutil.Process(terminal.pid)
                for child in attachment.children(recursive=True):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
                attachment.kill()
            terminal.close(force=True)
        if terminal_log is not None:
            terminal_log.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        model.shutdown()
        model.server_close()
        if cleanup_error is not None:
            raise RuntimeError(f"Session cleanup failed: {cleanup_error}") from cleanup_error


RELAY_DENIAL = "RELAY_POLICY_DENY_LITERAL"


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def relay_responder(read_path: Path, write_path: Path):
    """Drive the omnigent MCP server the way Prime 0.10 exposes it: through the python tool."""

    def respond(request: dict) -> list[tuple[dict, str | None]]:
        messages = request["messages"]
        user = max(i for i, message in enumerate(messages) if message["role"] == "user")
        results = [message_text(m) for m in messages[user + 1 :] if m["role"] == "tool"]
        if results:
            reply = "RELAY_RESULT " + results[-1]
            return [({"role": "assistant", "content": reply}, None), ({}, "stop")]
        prompt = message_text(messages[user])
        if "RELAY_READ" in prompt:
            call = f'{{"path": {json.dumps(str(read_path))}}}'
            tool = "sys_os_read"
        elif "RELAY_DENY" in prompt:
            call = f'{{"path": {json.dumps(str(write_path))}, "content": "denied"}}'
            tool = "sys_os_write"
        else:
            return pong(request)
        code = f'print(await mcp.call_tool("omnigent", {tool!r}, {call}))'
        tool_call = {
            "index": 0,
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": "ipython", "arguments": json.dumps({"code": code})},
        }
        return [({"role": "assistant", "tool_calls": [tool_call]}, None), ({}, "tool_calls")]

    return respond


def relay_agent(directory: Path) -> Path:
    from omnigent.native.native_coding_agents import native_shell_terminal_spec

    directory.mkdir()
    config = {
        "spec_version": 1,
        "name": "prime-native-relay",
        "prompt": "Use real tools when the model requests them.",
        "executor": {"type": "omnigent", "config": {"harness": "prime-native"}},
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        "terminals": native_shell_terminal_spec(),
        "guardrails": {
            "policies": {
                "deny_relay_write": {
                    "type": "function",
                    "on": ["tool_call:sys_os_write"],
                    "function": {
                        "path": "omnigent.policies.function.make_fixed_action_callable",
                        "arguments": {
                            "action": "deny",
                            "reason": RELAY_DENIAL,
                            "on_phases": ["tool_call"],
                            "on_tools": ["sys_os_write"],
                        },
                    },
                }
            }
        },
    }
    (directory / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (directory / "AGENTS.md").write_text("Use real tools when the model requests them.\n")
    return directory


def drive_relay(prime: str, evidence: Path, runtime: Path, env: dict[str, str]) -> None:
    """Call Omnigent's relay tools from Prime 0.10, one allowed and one policy-denied."""
    from omnigent.chat import _bundle_agent

    help_text = subprocess.run(
        [prime, "--help"], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    if "--extension" in help_text:
        raise RuntimeError("The relay scenario needs Prime 0.10, which exposes tools through MCP")
    workspace = runtime / "workspace"
    nonce = uuid.uuid4().hex
    read_path, write_path = workspace / "relay-read.txt", workspace / "relay-denied.txt"
    read_path.write_text(f"RELAY_FILE_{nonce}\n")
    model = make_model(evidence, relay_responder(read_path, write_path))
    write_prime_config(runtime, model)
    env["PRIME_AGENT_CODING_AGENT_DIR"] = str(runtime / "prime")
    env["OMNIGENT_PRIME_PATH"] = prime
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    omni = str(REPO / ".venv/bin/omnigent")
    process = terminal = terminal_log = session = None
    try:
        with (evidence / "server.log").open("w") as server_log:
            process = subprocess.Popen(
                [
                    omni,
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    url.rsplit(":", 1)[1],
                    "--database-uri",
                    f"sqlite:///{runtime}/data/server.db",
                    "--artifact-location",
                    str(runtime / "artifacts"),
                ],
                cwd=workspace,
                env=env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            with httpx.Client(base_url=url, timeout=30) as client:

                def healthy():
                    if process.poll() is not None:
                        raise RuntimeError("Omnigent server exited before readiness")
                    try:
                        return client.get("/health").status_code == 200
                    except httpx.HTTPError:
                        return False

                wait_for(healthy, "owned server /health")
                bundle = _bundle_agent(relay_agent(runtime / "relay-agent"))
                metadata = {
                    "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "prime-native-ui"},
                    "terminal_launch_args": [
                        "--offline",
                        "--provider",
                        "verify",
                        "--model",
                        "fixture",
                        "--no-context-files",
                    ],
                }
                created = client.post(
                    "/v1/sessions",
                    data={"metadata": json.dumps(metadata)},
                    files={"bundle": ("relay.tar.gz", bundle, "application/gzip")},
                )
                created.raise_for_status()
                session = created.json()["session_id"]
                (evidence / "session.json").write_text(
                    json.dumps({"session_id": session, "url": url})
                )
                terminal = pexpect.spawn(
                    omni,
                    ["prime-native", "--server", url, "--resume", session],
                    cwd=str(workspace),
                    env=env,
                    encoding="utf-8",
                    timeout=120,
                    dimensions=(32, 120),
                )
                terminal_log = (evidence / "terminal.txt").open("w")
                terminal.logfile_read = terminal_log
                terminal.expect(r"Web UI: [^\r\n]*/c/([A-Za-z0-9_-]+)")

                def ask(prompt: str, replies: int) -> tuple[str, str]:
                    action = {
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": prompt}],
                        },
                    }
                    client.post(f"/v1/sessions/{session}/events", json=action).raise_for_status()

                    def answered():
                        with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
                            terminal.read_nonblocking(65536, timeout=0.05)
                        items = client.get(f"/v1/sessions/{session}/items")
                        items.raise_for_status()
                        if assistant_text(items.json()).count("RELAY_RESULT") >= replies:
                            return items.json()
                        return None

                    items = wait_for(answered, f"relay reply {replies}", 120)
                    (evidence / f"relay-items-{replies}.json").write_text(
                        json.dumps(items, indent=2)
                    )
                    outputs = "".join(
                        item.get("output", "")
                        for item in items["data"]
                        if item.get("type") == "function_call_output"
                    )
                    return assistant_text(items).split("RELAY_RESULT")[replies], outputs

                allowed, allowed_output = ask(f"RELAY_READ {uuid.uuid4().hex}", 1)
                if f"RELAY_FILE_{nonce}" not in allowed_output:
                    raise RuntimeError("The relayed sys_os_read result is not in a tool output")
                denied, denied_output = ask(f"RELAY_DENY {uuid.uuid4().hex}", 2)
                if RELAY_DENIAL not in denied_output:
                    raise RuntimeError("The policy denial is not in a tool output")
                if write_path.exists():
                    raise RuntimeError("The denied sys_os_write still created its file")
                (evidence / "result.json").write_text(
                    json.dumps(
                        {
                            "scenario": "relay.mcp-tool-and-policy-denial",
                            "result": "PASS",
                            "session_id": session,
                            "allowed_reply": allowed,
                            "denied_reply": denied,
                        },
                        indent=2,
                    )
                )
    finally:
        cleanup_error = None
        if session is not None and process is not None and process.poll() is None:
            try:
                httpx.delete(f"{url}/v1/sessions/{session}", timeout=30).raise_for_status()
                wait_for(lambda: not owned_prime_processes(runtime), "owned Prime shutdown", 10)
            except (httpx.HTTPError, RuntimeError) as exc:
                cleanup_error = exc
        if terminal is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                attachment = psutil.Process(terminal.pid)
                for child in attachment.children(recursive=True):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
                attachment.kill()
            terminal.close(force=True)
        if terminal_log is not None:
            terminal_log.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        model.shutdown()
        model.server_close()
        if cleanup_error is not None:
            raise RuntimeError(f"Session cleanup failed: {cleanup_error}") from cleanup_error


def main() -> int:
    global REPO
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--prime-path")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument(
        "--scenario",
        choices=("messages", "relay"),
        default="messages",
        help="relay calls Omnigent tools from Prime 0.10 over MCP, with one policy denial",
    )
    parser.add_argument("--expected", default=PONG)
    parser.add_argument("--inspect-seconds", type=int, choices=(0, 60, 120), default=0)
    args = parser.parse_args()
    REPO = args.repo.resolve()
    if args.doctor:
        return run_doctor(args.prime_path or "prime-agent")
    prime_path = (
        args.prime_path
        if args.prime_path is not None
        else os.environ.get("OMNIGENT_PRIME_PATH", "prime-agent")
    )
    evidence = Path(tempfile.mkdtemp(prefix="prime-native-verify-", dir="/tmp"))
    runtime = Path(tempfile.mkdtemp(prefix="prime-native-runtime-", dir="/tmp"))
    env = dict(os.environ)
    env.update(
        {
            "OMNIGENT_CONFIG_HOME": str(runtime / "config"),
            "OMNIGENT_DATA_DIR": str(runtime / "data"),
            "OMNIGENT_SKIP_WEB_UI": "true",
            "BROWSER": "false",
            "TERM": "xterm-256color",
        }
    )
    for name in ("workspace", "data", "config"):
        (runtime / name).mkdir()
    status = 1
    try:
        prime = shutil.which(prime_path)
        if prime is None:
            raise RuntimeError("The requested Prime binary is unavailable")
        doctor(prime, evidence, env)
        if args.scenario == "relay":
            drive_relay(prime, evidence, runtime, env)
        else:
            drive(prime, evidence, runtime, env, args.expected, args.inspect_seconds)
        status = 0
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        pexpect.ExceptionPexpect,
    ) as exc:
        (evidence / "failure.txt").write_text(str(exc) + "\n")
        print(f"FAIL {exc}")
    finally:
        from omnigent.testing.process_reaper import reap_leaked_omnigent_processes

        _, survivors = reap_leaked_omnigent_processes(runtime / "data")
        owned = owned_prime_processes(runtime)
        for process in owned:
            with contextlib.suppress(psutil.Error):
                process.terminate()
        _, alive = psutil.wait_procs(owned, timeout=5)
        for process in alive:
            with contextlib.suppress(psutil.Error):
                process.kill()
        _, prime_survivors = psutil.wait_procs(alive, timeout=5)
        if survivors or prime_survivors:
            status = 1
        (evidence / "cleanup.json").write_text(
            json.dumps(
                {
                    "owned_survivors": len(survivors) + len(prime_survivors),
                    "prime_processes_reaped": len(owned),
                }
            )
        )
        logs = runtime / "data" / "logs"
        if logs.is_dir():
            shutil.copytree(logs, evidence / "process-logs")
        shutil.rmtree(runtime)
        (evidence / "manifest.json").write_text(
            json.dumps(
                {
                    "exit": status,
                    "evidence": str(evidence),
                    "runtime_removed": not runtime.exists(),
                },
                indent=2,
            )
        )
        print(f"Evidence {evidence}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

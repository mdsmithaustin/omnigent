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
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pexpect
import psutil

REPO = Path(__file__).resolve().parents[4]
PONG = "PRIME_NATIVE_PONG"


def doctor(prime: str, evidence: Path, env: dict[str, str]) -> None:
    results = {}
    for name, argv, required in (
        ("prime-version", [prime, "--version"], "0.9.6"),
        ("prime-help", [prime, "--help"], "model"),
        ("prime-model-help", [prime, "model", "list", "--help"], "List available models"),
        ("native-help", [str(REPO / ".venv/bin/omnigent"), "prime-native", "--help"], "--server"),
    ):
        result = subprocess.run(
            argv, env=env, cwd=REPO, capture_output=True, text=True, timeout=30
        )
        (evidence / f"{name}.txt").write_text(result.stdout + result.stderr)
        valid_output = (
            result.stdout.strip() == required
            if name == "prime-version"
            else required in result.stdout
        )
        if result.returncode or not valid_output:
            raise RuntimeError(f"Doctor failed for {name}, exit {result.returncode}")
        results[name] = {"argv": argv, "exit": result.returncode}
    results["checkout"] = str(REPO)
    results["revision"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
    ).strip()
    (evidence / "doctor.json").write_text(json.dumps(results, indent=2))


def wait_for(check, description: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}")


def make_model(evidence: Path) -> ThreadingHTTPServer:
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
            for delta, reason in (({"role": "assistant", "content": PONG}, None), ({}, "stop")):
                chunk = {
                    "id": "verify-completion",
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


def drive(
    prime: str,
    evidence: Path,
    runtime: Path,
    env: dict[str, str],
    expected: str,
    inspect_seconds: int = 0,
) -> None:
    model = make_model(evidence)
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
    env["PRIME_AGENT_CODING_AGENT_DIR"] = str(prime_config)
    env["OMNIGENT_PRIME_PATH"] = prime
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
                    "--no-tools",
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


def main() -> int:
    global REPO
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument(
        "--prime-path", default=os.environ.get("OMNIGENT_PRIME_PATH", "prime-agent")
    )
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--expected", default=PONG)
    parser.add_argument("--inspect-seconds", type=int, choices=(0, 60, 120), default=0)
    args = parser.parse_args()
    REPO = args.repo.resolve()
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
        prime = shutil.which(args.prime_path)
        if prime is None:
            raise RuntimeError("The requested Prime binary is unavailable")
        doctor(prime, evidence, env)
        if not args.doctor:
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
                    "doctor_only": args.doctor,
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

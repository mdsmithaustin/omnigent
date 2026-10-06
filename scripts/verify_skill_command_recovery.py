from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path.cwd()))


def deny_marked_request(event):
    if event.get("type") != "request":
        return {"result": "ALLOW"}
    text = json.dumps(event.get("data") or {})
    decision = "DENY" if "DENY_EFFECT_" in text else "ALLOW"
    with (Path(os.environ["RECOVERY_PROOF_DIR"]) / "policy.jsonl").open("a") as out:
        out.write(
            json.dumps(
                {"decision": decision, "markers": re.findall(r"(?:DENY_)?EFFECT_[0-9a-f]+", text)}
            )
            + "\n"
        )
    return (
        {"result": decision, "reason": "Recovery REQUEST deny"}
        if decision == "DENY"
        else {"result": decision}
    )


def requested_effect_marker(body):
    last_user = next(
        (
            message
            for message in reversed(body.get("messages", []))
            if message.get("role") == "user"
        ),
        {},
    )
    content = last_user.get("content", [])
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    ):
        return None
    markers = re.findall(r"(?:DENY_)?EFFECT_[0-9a-f]+", json.dumps(content))
    return markers[-1] if body.get("tools") and markers else None


def start_proxy(server_url, root):
    from aiohttp import ClientSession, WSMsgType, web

    from omnigent.runner.transports.ws_tunnel.frames import decode_body

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    stopped = None
    public_url = None

    def select_fault(marker_text, mode):
        path = root / "fault.json"
        if not path.exists():
            return None
        fault = json.loads(path.read_text())
        if fault["mode"] != mode or fault["marker"] not in marker_text:
            return None
        path.unlink()
        return fault

    def record_fault(fault, **evidence):
        with (root / "fault-observed.jsonl").open("a") as out:
            out.write(json.dumps({**fault, **evidence}) + "\n")

    async def serve():
        nonlocal stopped, public_url
        stopped = asyncio.Event()
        async with ClientSession(auto_decompress=False) as upstream:

            async def relay(request):
                headers = {
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() not in {"host", "connection", "upgrade", "content-length"}
                    and not key.lower().startswith("sec-websocket-")
                }
                target = server_url + str(request.rel_url)
                if request.headers.get("Upgrade", "").lower() == "websocket":
                    incoming = web.WebSocketResponse(autoping=False, max_msg_size=0)
                    await incoming.prepare(request)
                    async with upstream.ws_connect(
                        target, headers=headers, autoping=False, max_msg_size=0
                    ) as outgoing:
                        lost_runner_replies = {}

                        async def from_server():
                            async for message in outgoing:
                                if message.type == WSMsgType.TEXT:
                                    frame = json.loads(message.data)
                                    if frame.get("kind") == "request" and frame.get(
                                        "path", ""
                                    ).endswith("/events"):
                                        body = decode_body(
                                            frame.get("body") or "", frame.get("encoding", "utf-8")
                                        )
                                        command_body = json.loads(body)
                                        claim = command_body.get("command_admission") or {}
                                        fault = select_fault(body.decode(), "reject")
                                        if fault:
                                            record_fault(
                                                fault,
                                                request_id=frame["id"],
                                                forwarded_to_runner=False,
                                                claim=claim,
                                            )
                                            body = json.dumps(
                                                {
                                                    "error": "invalid_input",
                                                    "detail": (
                                                        "External relay refused delivery "
                                                        "before runner admission"
                                                    ),
                                                    "delivery": {**claim, "status": "rejected"},
                                                }
                                            )
                                            for reply in [
                                                {
                                                    "kind": "response.head",
                                                    "id": frame["id"],
                                                    "status": 400,
                                                    "headers": [
                                                        ["content-type", "application/json"]
                                                    ],
                                                },
                                                {
                                                    "kind": "response.body",
                                                    "id": frame["id"],
                                                    "body": body,
                                                    "encoding": "utf-8",
                                                },
                                                {"kind": "response.end", "id": frame["id"]},
                                            ]:
                                                await outgoing.send_str(json.dumps(reply))
                                            continue
                                        lost_reply = select_fault(
                                            body.decode(), "lose_runner_reply"
                                        )
                                        if lost_reply:
                                            lost_runner_replies[frame["id"]] = {
                                                "fault": lost_reply,
                                                "claim": claim,
                                            }
                                        if command_body.get("type") == "message":
                                            with (root / "forwards.jsonl").open("a") as out:
                                                out.write(
                                                    json.dumps(
                                                        {
                                                            "request_id": frame["id"],
                                                            "claim": claim,
                                                            "markers": re.findall(
                                                                r"(?:DENY_)?EFFECT_[0-9a-f]+",
                                                                body.decode(),
                                                            ),
                                                        }
                                                    )
                                                    + "\n"
                                                )
                                    await incoming.send_str(message.data)
                                elif message.type == WSMsgType.BINARY:
                                    await incoming.send_bytes(message.data)
                                elif message.type == WSMsgType.PING:
                                    await incoming.ping(message.data)
                                elif message.type == WSMsgType.PONG:
                                    await incoming.pong(message.data)

                        async def from_runner():
                            async for message in incoming:
                                if message.type == WSMsgType.TEXT:
                                    frame = json.loads(message.data)
                                    lost = lost_runner_replies.get(frame.get("id"))
                                    if lost is not None:
                                        if frame.get("kind") == "response.head":
                                            lost["upstream_http_status"] = frame["status"]
                                        if frame.get("kind") == "response.end":
                                            record_fault(
                                                lost["fault"],
                                                request_id=frame["id"],
                                                claim=lost["claim"],
                                                forwarded_to_runner=True,
                                                upstream_response_received=True,
                                                upstream_http_status=lost.get(
                                                    "upstream_http_status"
                                                ),
                                            )
                                            for reply in [
                                                {
                                                    "kind": "response.head",
                                                    "id": frame["id"],
                                                    "status": 502,
                                                    "headers": [
                                                        ["content-type", "application/json"]
                                                    ],
                                                },
                                                {
                                                    "kind": "response.body",
                                                    "id": frame["id"],
                                                    "body": '{"error":"ambiguous_gateway"}',
                                                    "encoding": "utf-8",
                                                },
                                                {"kind": "response.end", "id": frame["id"]},
                                            ]:
                                                await outgoing.send_str(json.dumps(reply))
                                            del lost_runner_replies[frame["id"]]
                                        continue
                                    await outgoing.send_str(message.data)
                                elif message.type == WSMsgType.BINARY:
                                    await outgoing.send_bytes(message.data)
                                elif message.type == WSMsgType.PING:
                                    await outgoing.ping(message.data)
                                elif message.type == WSMsgType.PONG:
                                    await outgoing.pong(message.data)

                        tasks = [
                            asyncio.create_task(from_server()),
                            asyncio.create_task(from_runner()),
                        ]
                        try:
                            done, _ = await asyncio.wait(
                                tasks, return_when=asyncio.FIRST_COMPLETED
                            )
                            for task in done:
                                task.result()
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            await incoming.close()
                    return incoming
                body = await request.read()
                async with upstream.request(
                    request.method, target, headers=headers, data=body
                ) as response:
                    data = await response.read()
                    if (
                        request.path.endswith("/events")
                        and json.loads(body or b"{}").get("type") == "slash_command"
                    ):
                        fault = select_fault(body.decode(), "lose_reply")
                        if fault:
                            record_fault(
                                fault,
                                upstream_http_status=response.status,
                                upstream_response_received=True,
                                invocation_id=json.loads(body)["data"]["stable_id"],
                                original_response=json.loads(data),
                            )
                            await asyncio.sleep(2)
                    response_headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower() not in {"transfer-encoding", "content-length", "connection"}
                    }
                    return web.Response(
                        status=response.status, body=data, headers=response_headers
                    )

            app = web.Application(client_max_size=64 * 1024 * 1024)
            app.router.add_route("*", "/{path:.*}", relay)
            runner = web.AppRunner(app, shutdown_timeout=3)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            public_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            ready.set()
            try:
                await stopped.wait()
            finally:
                await runner.cleanup()

    thread = threading.Thread(target=lambda: loop.run_until_complete(serve()), daemon=True)
    thread.start()
    if not ready.wait(10):
        raise RuntimeError("External transport proxy did not start")

    def close():
        loop.call_soon_threadsafe(stopped.set)
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("External transport proxy did not stop")
        loop.close()

    return public_url, close


def wait_for(check, description, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}")


def read_lines(path):
    return path.read_text().splitlines() if path.exists() else []


class NativeOwnerCensus:
    def __init__(self, attribution, started_at):
        self.attribution = attribution
        self.started_at = started_at
        self.owners = {}
        self.gaps = []
        self.tmux = None
        self.server_identity = None

    def capture_process(self, process, role, socket_path=None):
        environment = process.environ()
        identity = {
            "pid": process.pid,
            "created_at": process.create_time(),
            "roles": [role],
            "private_socket": socket_path,
            "command": process.cmdline(),
            "run_attribution": self.attribution,
            "observed_environment": {key: environment.get(key) for key in self.attribution},
        }
        key = (identity["pid"], identity["created_at"])
        if key in self.owners:
            identity["roles"] = sorted(set(self.owners[key]["roles"]) | {role})
        self.owners[key] = identity
        return identity

    def observe_attributed_processes(self):
        import psutil

        try:
            for process in psutil.process_iter(["pid", "create_time", "uids"]):
                try:
                    if process.info["uids"].real != os.getuid():
                        continue
                    if process.info["create_time"] < self.started_at:
                        continue
                    environment = process.environ()
                    if any(
                        environment.get(key) == value for key, value in self.attribution.items()
                    ):
                        self.capture_process(
                            process,
                            "attributed native owner",
                            self.tmux[-1] if self.tmux else None,
                        )
                except psutil.NoSuchProcess:
                    continue
                except (psutil.Error, OSError, TypeError, AttributeError) as exc:
                    self.gaps.append(
                        f"Process attribution observation failed for {process.pid}: {exc}"
                    )
        except (psutil.Error, OSError) as exc:
            self.gaps.append(f"Process enumeration failed: {exc}")

    def capture_tmux(self, tmux, target):
        import psutil

        self.tmux = tmux
        try:
            output = subprocess.check_output(
                [
                    *tmux,
                    "display-message",
                    "-p",
                    "-t",
                    target,
                    "#{pid}\t#{pane_pid}\t#{socket_path}",
                ],
                text=True,
                timeout=5,
            ).strip()
            server_pid, pane_pid, socket_path = output.split("\t")
            if Path(socket_path).resolve() != Path(tmux[-1]).resolve():
                raise RuntimeError("tmux reported a different private socket")
            server = psutil.Process(int(server_pid))
            pane = psutil.Process(int(pane_pid))
            self.server_identity = self.capture_process(server, "tmux server", socket_path)
            self.capture_process(pane, "tmux pane", socket_path)
            native = []
            for process in [pane, *pane.children(recursive=True)]:
                environment = process.environ()
                if all(environment.get(key) == value for key, value in self.attribution.items()):
                    native.append(self.capture_process(process, "native process", socket_path))
                else:
                    self.capture_process(process, "pane descendant", socket_path)
            if not native:
                raise RuntimeError("No native process has the private config and run attribution")
        except (
            psutil.Error,
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            self.gaps.append(f"Native ownership capture failed: {exc}")
        self.observe_attributed_processes()

    def close_tmux(self):
        import psutil

        if self.tmux is None or self.server_identity is None:
            self.gaps.append("No qualified tmux server identity for close")
            return
        try:
            server = psutil.Process(self.server_identity["pid"])
            if server.create_time() != self.server_identity["created_at"]:
                raise RuntimeError("tmux server identity changed before close")
            observed = (
                subprocess.check_output(
                    [*self.tmux, "display-message", "-p", "#{pid}\t#{socket_path}"],
                    text=True,
                    timeout=5,
                )
                .strip()
                .split("\t")
            )
            if observed != [str(server.pid), self.server_identity["private_socket"]]:
                raise RuntimeError("tmux socket ownership changed before close")
            result = subprocess.run(
                [*self.tmux, "kill-server"], capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"tmux kill-server exited {result.returncode}: {result.stderr.strip()}"
                )
        except (psutil.Error, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.gaps.append(f"Qualified tmux close failed: {exc}")

    def finish(self, summary, owned):
        import psutil

        self.observe_attributed_processes()
        identities = {(pid, created) for pid, created in owned.items()} | set(self.owners)
        survivors = []
        for pid, created in sorted(identities):
            try:
                process = psutil.Process(pid)
                if (
                    process.create_time() == created
                    and process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                ):
                    survivors.append(pid)
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:
                self.gaps.append(f"Final process census failed for {pid}: {exc}")
        if self.tmux is None or self.server_identity is None:
            self.gaps.append("Native owner identity was never captured")
        summary["native_owners"] = list(self.owners.values())
        summary["cleanup_gaps"] = self.gaps
        summary["owned_survivors"] = survivors
        summary["cleanup_qualified"] = not self.gaps and not survivors
        summary["qualified"] = summary["qualified"] and summary["cleanup_qualified"]
        return summary["cleanup_qualified"]


def run(args):
    import httpx
    import psutil

    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )
    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
    from tests.server.integration.mock_llm_server import (
        anthropic_sse_text_response,
        anthropic_sse_tool_call_response,
    )

    root = args.output or Path(tempfile.mkdtemp(prefix="command-native-proof-"))
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir()
    config = root / "claude-config"
    config.mkdir()
    (config / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    (config / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
    skill = workspace / ".claude/skills/recovery-nonce/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: recovery-nonce\n"
        "description: Execute the requested recovery nonce once.\n---\n\n"
        "Execute the requested nonce with the Bash tool once.\n"
    )
    (workspace / ".git").mkdir()
    effects = root / "native-effects.txt"
    (root / "server.yaml").write_text(
        "policies:\n  recovery-request:\n    type: function\n"
        "    handler: verify_skill_command_recovery.deny_marked_request\n"
    )
    print(f"Evidence directory {root}", flush=True)

    gateway_lock = threading.Lock()
    gateway_requests = 0

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            nonlocal gateway_requests
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = body.get("model", "recovery-fixture")
            with gateway_lock:
                gateway_requests += 1
                if gateway_requests <= 128:
                    messages = body.get("messages") or []
                    evidence = {
                        "request": gateway_requests,
                        "path": self.path.split("?", 1)[0],
                        "stream": bool(body.get("stream")),
                        "model": model,
                        "tool_names": [tool.get("name") for tool in body.get("tools", [])][:64],
                        "message_count": len(messages),
                        "messages": [
                            {
                                "role": message.get("role"),
                                "markers": re.findall(
                                    r"(?:DENY_)?EFFECT_[0-9a-f]+",
                                    json.dumps(message.get("content")),
                                )[:16],
                                "block_types": [
                                    block.get("type")
                                    for block in message.get("content", [])
                                    if isinstance(block, dict)
                                ][:64],
                                "content_kind": type(message.get("content")).__name__,
                            }
                            for message in messages[-12:]
                        ],
                    }
                    with (root / "gateway-requests.jsonl").open("a") as out:
                        out.write(json.dumps(evidence) + "\n")
            if self.path.split("?", 1)[0] == "/v1/messages":
                if body.get("stream"):
                    marker = requested_effect_marker(body)
                    if marker is not None:
                        command = (
                            f"printf '%s\\n' {shlex.quote(marker)} >> {shlex.quote(str(effects))}"
                        )
                        payload = anthropic_sse_tool_call_response(
                            [
                                {
                                    "call_id": "toolu_" + secrets.token_hex(8),
                                    "name": "Bash",
                                    "arguments": json.dumps(
                                        {
                                            "command": command,
                                            "description": "Write independent recovery nonce",
                                        }
                                    ),
                                }
                            ],
                            model=model,
                        ).encode()
                        with (root / "gateway.jsonl").open("a") as out:
                            out.write(json.dumps({"marker": marker, "tool": "Bash"}) + "\n")
                    else:
                        completion_marker = None
                        messages = body.get("messages", [])
                        last_user = next(
                            (m for m in reversed(messages) if m.get("role") == "user"), {}
                        )
                        content = last_user.get("content", [])
                        results = (
                            {
                                block.get("tool_use_id")
                                for block in content
                                if isinstance(block, dict) and block.get("type") == "tool_result"
                            }
                            if isinstance(content, list)
                            else set()
                        )
                        for message in reversed(messages):
                            blocks = message.get("content", [])
                            if message.get("role") == "assistant" and isinstance(blocks, list):
                                for block in blocks:
                                    if (
                                        isinstance(block, dict)
                                        and block.get("type") == "tool_use"
                                        and block.get("id") in results
                                    ):
                                        markers = re.findall(
                                            r"(?:DENY_)?EFFECT_[0-9a-f]+",
                                            json.dumps(block.get("input")),
                                        )
                                        if markers:
                                            completion_marker = markers[-1]
                        payload = anthropic_sse_text_response(
                            "RECOVERY_NATIVE_DONE"
                            + (" " + completion_marker if completion_marker else ""),
                            model=model,
                        ).encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(
                        {
                            "id": "msg_aux",
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [{"type": "text", "text": "Recovery"}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    ).encode()
                    content_type = "application/json"
            else:
                payload, content_type = b'{"input_tokens":10}', "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    server_url = f"http://127.0.0.1:{port}"
    public_url, close_proxy = start_proxy(server_url, root)
    token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(token)
    repo = Path.cwd()
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "USER", "LANG", "TMPDIR")
        if key in os.environ
    }
    env.update(
        PYTHONPATH=os.pathsep.join(
            str(p) for p in (repo, HERE, repo / "sdks/python-client", repo / "sdks/ui")
        ),
        OMNIGENT_CONFIG_HOME=str(root / "omnigent-config"),
        OMNIGENT_DATA_DIR=str(root / "omnigent-data"),
        OMNIGENT_AUTH_PROVIDER="header",
        OMNIGENT_LOCAL_SINGLE_USER="1",
        OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
        OMNIGENT_FEATURES="native_skill_routing",
        OPENAI_API_KEY="local-recovery-fixture",
        RECOVERY_PROOF_DIR=str(root),
        OMNIGENT_RUNNER_ID=runner_id,
        OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN=token,
        OMNIGENT_RUNNER_PARENT_PID=str(os.getpid()),
        OMNIGENT_RUNNER_WORKSPACE=str(workspace),
        RUNNER_SERVER_URL=public_url,
        CLAUDE_CONFIG_DIR=str(config),
        ANTHROPIC_AUTH_TOKEN="local-recovery-fixture",
        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{gateway.server_port}",
        CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
        DISABLE_AUTOUPDATER="1",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
        NO_PROXY="127.0.0.1,localhost",
        TERM="xterm-256color",
    )
    processes = []
    owned = {}
    native_owners = NativeOwnerCensus(
        {"RECOVERY_PROOF_DIR": str(root), "CLAUDE_CONFIG_DIR": str(config)}, time.time()
    )
    tmux = None
    target = None
    session_id = None
    summary = {
        "qualified": False,
        "surface": "real Claude CLI, native UserPromptSubmit, server HTTP, runner tunnel",
        "remote_inference": "local fixture",
        "claude_permission_mode": "manual",
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "source_changes": subprocess.check_output(
            ["git", "status", "--short"], cwd=repo, text=True
        ).splitlines(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "claude_version": subprocess.check_output(
            ["claude", "--version"], text=True, timeout=10
        ).strip(),
    }
    with contextlib.ExitStack() as stack:
        client = stack.enter_context(
            httpx.Client(
                base_url=public_url,
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
                timeout=90,
                trust_env=False,
            )
        )
        try:
            commands = {
                "server": [
                    sys.executable,
                    "-m",
                    "omnigent.cli",
                    "server",
                    "--port",
                    str(port),
                    "--database-uri",
                    f"sqlite:///{root / 'proof.db'}",
                    "--artifact-location",
                    str(root / "artifacts"),
                    "--config",
                    str(root / "server.yaml"),
                ],
                "runner": [sys.executable, "-m", "omnigent.runner._entry"],
            }
            current_processes = {}

            def start_process(role):
                command = commands[role]
                handle = stack.enter_context((root / f"{role}.log").open("a"))
                proc = subprocess.Popen(
                    command, cwd=repo, env=env, stdout=handle, stderr=subprocess.STDOUT
                )
                processes.append(proc)
                owned[proc.pid] = psutil.Process(proc.pid).create_time()
                current_processes[role] = proc
                if role == "server":

                    def healthy():
                        if proc.poll() is not None:
                            raise RuntimeError(
                                f"Server exited {proc.returncode}; inspect server.log"
                            )
                        try:
                            return client.get("/health", timeout=2).status_code == 200
                        except httpx.HTTPError:
                            return False

                    wait_for(healthy, "server health")

            for role in commands:
                start_process(role)
            wait_for(
                lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online"),
                "runner tunnel",
            )
            spec = (
                b"name: recovery-command-proof\nprompt: Use the requested tool once.\n"
                b"skills: all\nexecutor:\n  harness: claude-native\nos_env:\n"
                b"  type: caller_process\n  cwd: .\n  sandbox:\n    type: none\n"
            )
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
                info = tarfile.TarInfo("recovery.yaml")
                info.size = len(spec)
                archive.addfile(info, io.BytesIO(spec))
            response = client.post(
                "/v1/sessions",
                data={
                    "metadata": json.dumps(
                        {
                            "workspace": str(workspace),
                            "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                            "terminal_launch_args": ["--permission-mode", "manual"],
                        }
                    )
                },
                files={"bundle": ("agent.tar.gz", bundle.getvalue(), "application/gzip")},
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
            summary["session_id"] = session_id
            client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
            ).raise_for_status()
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            ).raise_for_status()
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            bridge = bridge_dir_for_bridge_id(
                snapshot.get("labels", {}).get(BRIDGE_ID_LABEL_KEY) or session_id
            )
            wait_for(lambda: (bridge / "tmux.json").exists(), "Claude tmux target")
            pane = json.loads((bridge / "tmux.json").read_text())
            tmux = ["tmux", "-S", pane["socket_path"]]
            target = pane["tmux_target"]
            summary["tmux_socket"] = pane["socket_path"]

            def capture():
                return subprocess.check_output(
                    [*tmux, "capture-pane", "-p", "-t", target], text=True, timeout=5
                )

            def direct(text):
                subprocess.run(
                    [*tmux, "load-buffer", "-"], input=text.encode(), check=True, timeout=5
                )
                subprocess.run(
                    [*tmux, "paste-buffer", "-p", "-d", "-t", target], check=True, timeout=5
                )
                time.sleep(0.3)
                subprocess.run([*tmux, "send-keys", "-t", target, "Enter"], check=True, timeout=5)

            def items():
                response = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 1000})
                response.raise_for_status()
                return response.json()["data"]

            def completed(marker):
                def completion():
                    return next(
                        (
                            item
                            for item in items()
                            if item.get("role") == "assistant"
                            and any(
                                block.get("text") == "RECOVERY_NATIVE_DONE " + marker
                                for block in item.get("content", [])
                            )
                        ),
                        None,
                    )

                item = wait_for(completion, "native assistant completion for " + marker)
                wait_for(
                    lambda: (
                        client.get(f"/v1/sessions/{session_id}").json().get("status") == "idle"
                    ),
                    "native idle after completion",
                )
                return item["id"]

            def forwards(marker):
                return [
                    json.loads(line)
                    for line in read_lines(root / "forwards.jsonl")
                    if marker in json.loads(line)["markers"]
                ]

            def command_item(response, expected):
                body = response.json()
                assert response.status_code == 202, body
                assert body["delivery"]["status"] == expected, body
                saved = next(item for item in items() if item["id"] == body["item_id"])
                assert saved["delivery"] == body["delivery"], (saved, body)
                assert body["queued"] is (expected == "accepted"), body
                return saved

            def command(marker, identity):
                return {
                    "type": "slash_command",
                    "data": {
                        "kind": "skill",
                        "name": "recovery-nonce",
                        "arguments": marker,
                        "stable_id": identity,
                    },
                }

            wait_for(lambda: "❯" in capture(), "Claude composer")
            baseline = "EFFECT_" + secrets.token_hex(8)
            first = client.post(
                f"/v1/sessions/{session_id}/events", json=command(baseline, secrets.token_hex(16))
            )
            first.raise_for_status()
            wait_for(
                lambda: read_lines(effects).count(baseline) == 1, "native Bash baseline nonce"
            )
            baseline_completion = completed(baseline)
            baseline_card = command_item(first, "accepted")
            if not baseline_card.get("native_invocation"):
                raise RuntimeError("Skill used paste fallback; wrong-surface gap")
            summary["baseline"] = {
                "count": 1,
                "native_invocation": baseline_card["native_invocation"],
                "completion_item_id": baseline_completion,
            }
            summary["baseline_verified"] = True
            native_owners.capture_tmux(tmux, target)
            (root / "native-owners.json").write_text(
                json.dumps(list(native_owners.owners.values()), indent=2)
            )
            print("Nominal native skill wrote exactly one independent nonce", flush=True)
            if args.baseline_only:
                summary["baseline_verified"] = True
                return 0

            denied = "DENY_EFFECT_" + secrets.token_hex(8)
            control = client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json={"event": {"type": "PHASE_REQUEST", "data": {"text": denied}}},
            )
            if control.json().get("result") != "POLICY_ACTION_DENY":
                raise RuntimeError(f"Deny-policy control failed: {control.text}")
            denied_before = len(read_lines(root / "policy.jsonl"))
            rejected = "EFFECT_" + secrets.token_hex(8)
            (root / "fault.json").write_text(json.dumps({"mode": "reject", "marker": rejected}))
            rejected_response = client.post(
                f"/v1/sessions/{session_id}/events", json=command(rejected, secrets.token_hex(16))
            )
            rejected_card = command_item(rejected_response, "rejected")
            direct(denied)
            wait_for(
                lambda: (
                    read_lines(effects).count(denied) > 0
                    or any(
                        '"DENY"' in line and denied in line
                        for line in read_lines(root / "policy.jsonl")[denied_before:]
                    )
                ),
                "native deny result or forbidden nonce",
            )
            summary["rejection"] = {
                "http": rejected_response.status_code,
                "body": rejected_response.json(),
                "rejected_effects": read_lines(effects).count(rejected),
                "forbidden_effects": read_lines(effects).count(denied),
                "native_policy_denied": any(
                    '"DENY"' in line and denied in line
                    for line in read_lines(root / "policy.jsonl")[denied_before:]
                ),
            }
            print(json.dumps({"rejection": summary["rejection"]}), flush=True)

            ambiguous = "EFFECT_" + secrets.token_hex(8)
            ambiguous_request = command(ambiguous, secrets.token_hex(16))
            (root / "fault.json").write_text(
                json.dumps({"mode": "lose_runner_reply", "marker": ambiguous})
            )
            ambiguous_response = client.post(
                f"/v1/sessions/{session_id}/events", json=ambiguous_request
            )
            ambiguous_card = command_item(ambiguous_response, "unknown")
            ambiguous_completion = completed(ambiguous)
            ambiguous_retry = client.post(
                f"/v1/sessions/{session_id}/events", json=ambiguous_request
            )
            assert command_item(ambiguous_retry, "unknown")["id"] == ambiguous_card["id"]
            assert len(forwards(ambiguous)) == 1
            summary["ambiguous"] = {
                "body": ambiguous_retry.json(),
                "completion_item_id": ambiguous_completion,
            }

            marker = "EFFECT_" + secrets.token_hex(8)
            identity = secrets.token_hex(16)
            request_body = command(marker, identity)
            (root / "fault.json").write_text(json.dumps({"mode": "lose_reply", "marker": marker}))
            try:
                client.post(f"/v1/sessions/{session_id}/events", json=request_body, timeout=0.2)
                raise RuntimeError("Lost-reply trigger did not time out")
            except httpx.ReadTimeout:
                pass
            wait_for(
                lambda: read_lines(effects).count(marker) == 1,
                "first accepted nonce after lost reply",
            )
            retry_completion = completed(marker)
            retry = client.post(f"/v1/sessions/{session_id}/events", json=request_body)
            retry_card = command_item(retry, "accepted")
            fault_records = [
                json.loads(line) for line in read_lines(root / "fault-observed.jsonl")
            ]
            refusal, ambiguity, lost = fault_records
            assert (
                refusal["mode"] == "reject"
                and refusal["marker"] == rejected
                and refusal["forwarded_to_runner"] is False
            )
            assert refusal["claim"] == {
                "item_id": rejected_card["id"],
                "fingerprint": rejected_card["delivery"]["fingerprint"],
            }
            assert ambiguity["mode"] == "lose_runner_reply" and ambiguity["marker"] == ambiguous
            assert (
                ambiguity["forwarded_to_runner"] is True
                and ambiguity["upstream_http_status"] == 202
                and ambiguity["upstream_response_received"] is True
            )
            assert (
                lost["mode"] == "lose_reply"
                and lost["marker"] == marker
                and lost["invocation_id"] == identity
            )
            assert (
                lost["upstream_http_status"] == 202 and lost["upstream_response_received"] is True
            )
            assert lost["original_response"] == retry.json()
            assert len(forwards(marker)) == 1
            summary["retry"] = {"body": retry.json(), "completion_item_id": retry_completion}

            old_processes = {
                role: {"pid": proc.pid, "created_at": owned[proc.pid]}
                for role, proc in current_processes.items()
            }
            for role in ("runner", "server"):
                proc = current_processes[role]
                for child in psutil.Process(proc.pid).children(recursive=True):
                    owned[child.pid] = child.create_time()
                proc.terminate()
                proc.wait(timeout=20)
            for role in ("server", "runner"):
                start_process(role)
            wait_for(
                lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online"),
                "restarted runner tunnel",
            )
            for original, expected, claim in [
                (request_body, "accepted", retry_card),
                (ambiguous_request, "unknown", ambiguous_card),
            ]:
                restored = client.post(f"/v1/sessions/{session_id}/events", json=original)
                assert command_item(restored, expected)["id"] == claim["id"]
                assert restored.json()["delivery"] == claim["delivery"]
            assert len(forwards(marker)) == len(forwards(ambiguous)) == 1
            summary["restart"] = {
                "old_processes": old_processes,
                "new_processes": {
                    role: {"pid": proc.pid, "created_at": owned[proc.pid]}
                    for role, proc in current_processes.items()
                },
                "original_claims_preserved": True,
                "additional_forwards": 0,
            }
            recovery = "EFFECT_" + secrets.token_hex(8)
            recovered = client.post(
                f"/v1/sessions/{session_id}/events", json=command(recovery, secrets.token_hex(16))
            )
            recovered_card = command_item(recovered, "accepted")
            summary["recovered"] = {
                "item_id": recovered_card["id"],
                "completion_item_id": completed(recovery),
            }
            pane = json.loads((bridge / "tmux.json").read_text())
            tmux = ["tmux", "-S", pane["socket_path"]]
            target = pane["tmux_target"]
            summary["restarted_tmux_socket"] = pane["socket_path"]
            native_owners.capture_tmux(tmux, target)

            final_items = items()
            for invocation_id in (identity, ambiguous_request["data"]["stable_id"]):
                claims = [
                    item
                    for item in final_items
                    if item.get("delivery", {}).get("invocation_id") == invocation_id
                ]
                assert len(claims) == 1, claims
            census = {
                name: read_lines(effects).count(value)
                for name, value in {
                    "baseline": baseline,
                    "rejected": rejected,
                    "forbidden": denied,
                    "ambiguous": ambiguous,
                    "retry": marker,
                    "recovered": recovery,
                }.items()
            }
            assert census == {
                "baseline": 1,
                "rejected": 0,
                "forbidden": 0,
                "ambiguous": 1,
                "retry": 1,
                "recovered": 1,
            }, census
            assert len(forwards(marker)) == len(forwards(ambiguous)) == 1
            assert forwards(rejected) == []
            assert summary["rejection"]["native_policy_denied"] is True
            summary["final_effect_census"] = census
            summary["final_forward_census"] = {
                "retry": len(forwards(marker)),
                "ambiguous": len(forwards(ambiguous)),
                "rejected": len(forwards(rejected)),
            }
            (root / "items.json").write_text(json.dumps(final_items, indent=2))
            summary["qualified"] = True
            return 0
        except AssertionError:
            summary["failed_assertion"] = traceback.format_exc()
            print(summary["failed_assertion"], flush=True)
            return 1
        except (
            RuntimeError,
            OSError,
            ValueError,
            KeyError,
            httpx.HTTPError,
            subprocess.SubprocessError,
            psutil.Error,
        ) as exc:
            summary["gap"] = f"{type(exc).__name__}: {exc}"
            print(summary["gap"], flush=True)
            return 2
        finally:
            if session_id is not None:
                with contextlib.suppress(httpx.HTTPError):
                    saved = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 1000})
                    (root / "items.json").write_text(json.dumps(saved.json(), indent=2))
            for proc in processes:
                try:
                    for child in psutil.Process(proc.pid).children(recursive=True):
                        owned[child.pid] = child.create_time()
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error as exc:
                    native_owners.gaps.append(f"Descendant capture failed for {proc.pid}: {exc}")
            if tmux is not None:
                native_owners.capture_tmux(tmux, target)
                with contextlib.suppress(Exception):
                    (root / "pane.txt").write_text(
                        subprocess.check_output(
                            [*tmux, "capture-pane", "-p", "-S", "-200", "-t", target],
                            text=True,
                            timeout=5,
                        )
                    )
                native_owners.close_tmux()
            for proc in reversed(processes):
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5)
                except (OSError, subprocess.SubprocessError) as exc:
                    native_owners.gaps.append(f"Process close failed for {proc.pid}: {exc}")
            try:
                close_proxy()
                gateway.shutdown()
                gateway.server_close()
                thread.join(timeout=5)
                if thread.is_alive():
                    native_owners.gaps.append("Gateway thread did not stop")
            except Exception as exc:
                logging.getLogger(__name__).exception("Fixture cleanup failed")
                native_owners.gaps.append(f"Fixture cleanup failed: {exc}")
            cleanup_qualified = native_owners.finish(summary, owned)
            (root / "summary.json").write_text(json.dumps(summary, indent=2))
            print(f"Evidence saved to {root / 'summary.json'}", flush=True)
            if not cleanup_qualified:
                raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run:
        import omnigent

        assert Path("pyproject.toml").is_file(), "Run from the assigned Omnigent worktree"
        for binary in ("claude", "tmux"):
            assert shutil.which(binary), f"Missing {binary}"
        print(
            json.dumps(
                {
                    "prerequisites": "ready",
                    "python": sys.executable,
                    "omnigent": omnigent.__file__,
                    "live_started": False,
                }
            )
        )
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

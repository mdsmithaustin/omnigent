from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class Actor:
    name: str
    python: Path
    directory: str
    preferred_port: int


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-python", type=Path, required=True)
    parser.add_argument("--fork-python", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--require-ui", action="store_true")
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=False)
    home = Path(tempfile.mkdtemp(prefix="omnigent-coexistence-"))
    workspace = home / "workspace"
    workspace.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_", "RUNNER_")) and key != "PYTHONPATH"
    }
    env["HOME"] = str(home)
    actors = (
        Actor("upstream", args.upstream_python, ".omnigent", 6767),
        Actor("fork", args.fork_python, ".omnigent-mdsmithaustin", 6768),
    )
    owned: list[tuple[int, float]] = []
    sequence = 0
    receipt: dict[str, object] = {"home": str(home), "qualified": False}

    def command(actor: Actor, *arguments: str) -> str:
        nonlocal sequence
        sequence += 1
        result = subprocess.run(
            [str(actor.python.parent / "omnigent"), *arguments],
            env=env,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
        )
        name = f"{sequence:02d}-" + actor.name + "-" + "-".join(arguments).replace("/", "_")
        (args.evidence / (name + ".stdout")).write_text(result.stdout)
        (args.evidence / (name + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, (name, result.returncode, result.stderr)
        return result.stdout

    def health(port: int) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            body = json.load(response)
        assert body["status"] == "ok", body
        return body

    def running(identity: tuple[int, float]) -> bool:
        try:
            process = psutil.Process(identity[0])
            return (
                process.create_time() == identity[1] and process.status() != psutil.STATUS_ZOMBIE
            )
        except psutil.NoSuchProcess:
            return False

    try:
        for actor in actors:
            assert actor.python.is_file(), actor.python
            probe = (
                "import json; from omnigent.process_logging import data_dir; "
                "from omnigent.config import global_config_path; "
                "from omnigent.host.local_server import _local_data_dir,_DEFAULT_LOCAL_PORT; "
                "print(json.dumps({'runtime':str(data_dir()),'config':str(global_config_path()),"
                "'local_runtime':str(_local_data_dir()),'port':_DEFAULT_LOCAL_PORT}))"
            )
            observed = subprocess.run(
                [str(actor.python), "-I", "-c", probe],
                env=env,
                cwd=workspace,
                capture_output=True,
                text=True,
                check=True,
            )
            values = json.loads(observed.stdout)
            receipt[actor.name + "_defaults"] = values
            assert values == {
                "runtime": str(home / actor.directory),
                "config": str(home / actor.directory / "config.yaml"),
                "local_runtime": str(home / actor.directory),
                "port": actor.preferred_port,
            }, (actor.name, values)
            command(actor, "server", "--background")
            pid, port = map(
                int, (home / actor.directory / "local_server.pid").read_text().splitlines()
            )
            process = psutil.Process(pid)
            identity = (pid, process.create_time())
            owned.append(identity)
            assert str(actor.python) in process.cmdline(), process.cmdline()
            receipt[actor.name + "_server"] = {"pid": pid, "created": identity[1], "port": port}
            health(port)
        assert owned[0][0] != owned[1][0]
        assert running(owned[0]) and running(owned[1])
        upstream_port = receipt["upstream_server"]["port"]
        fork_port = receipt["fork_server"]["port"]
        assert upstream_port != fork_port
        receipt["concurrent_health"] = [health(upstream_port), health(fork_port)]
        if args.require_ui:
            for actor, port in zip(actors, (upstream_port, fork_port), strict=True):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                    html = response.read().decode()
                    assert response.status == 200 and "<html" in html.lower()
                (args.evidence / (actor.name + "-index.html")).write_text(html)
            receipt["simultaneous_ui_verified"] = True
        command(actors[1], "server", "stop")
        deadline = time.monotonic() + 20
        while running(owned[1]) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not running(owned[1]), "Fork stop left its recorded server alive"
        assert running(owned[0]), "Fork stop terminated upstream"
        receipt["upstream_health_after_fork_stop"] = health(upstream_port)
        assert (home / ".omnigent/local_server.pid").is_file()
        with socket.socket() as probe_socket:
            probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe_socket.bind(("127.0.0.1", 6768))
        fixture_code = """
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args):
        pass
class Server(HTTPServer):
    allow_reuse_address = True
Server(('127.0.0.1', 6768), Handler).serve_forever()
"""
        fixture = subprocess.Popen(
            [str(args.upstream_python), "-I", "-c", fixture_code],
            env=env,
            cwd=workspace,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        fixture_identity = (fixture.pid, psutil.Process(fixture.pid).create_time())
        owned.append(fixture_identity)
        deadline = time.monotonic() + 10
        while True:
            assert running(fixture_identity), "Healthy fixture exited before acceptance check"
            try:
                health(6768)
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.1)
        for stop_arguments in (("server", "stop"), ("stop",)):
            command(actors[1], *stop_arguments)
            assert running(fixture_identity), (stop_arguments, "Killed an untracked listener")
            assert running(owned[0]), (stop_arguments, "Terminated upstream")
            health(upstream_port)
            health(6768)
        receipt["untracked_listener_survived"] = health(6768)
        receipt["stop_commands_verified"] = ["server stop", "stop"]
        receipt["qualified"] = True
    except BaseException as error:
        receipt["error"] = repr(error)
        raise
    finally:
        for pid, created in reversed(owned):
            if not running((pid, created)):
                continue
            process = psutil.Process(pid)
            if process.create_time() != created:
                continue
            process.terminate()
            try:
                process.wait(timeout=20)
            except psutil.TimeoutExpired:
                assert process.create_time() == created
                process.kill()
                process.wait(timeout=5)
        survivors = [identity for identity in owned if running(identity)]
        receipt["owned_survivors"] = survivors
        (args.evidence / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        assert not survivors, survivors


if __name__ == "__main__":
    main()

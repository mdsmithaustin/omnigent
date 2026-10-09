from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e._harness_probes import cli_unavailable_reason

pexpect = pytest.importorskip("pexpect")

pytestmark = pytest.mark.skipif(
    (_reason := cli_unavailable_reason("pi")) is not None,
    reason=f"pi-native misroute e2e requires a runnable 'pi' CLI; {_reason}.",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
# A Claude-family model the gateway serves only on its Anthropic surface.
_CLAUDE_MODEL = "claude-fable-5-1"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[>=]")
# Launch budget mirrors the CLI's internal host/runner cold-start ceiling.
_LAUNCH_TIMEOUT = 180


class _GatewayHandler(BaseHTTPRequestHandler):
    """Mock gateway OpenAI surface: 404 with an empty body, like the report."""

    requests: list[dict[str, Any]] = []

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        model = None
        with contextlib.suppress(Exception):
            model = json.loads(body.decode() or "{}").get("model")
        _GatewayHandler.requests.append(
            {"method": self.command, "path": self.path, "model": model}
        )
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = _handle  # type: ignore[assignment]

    def log_message(self, *args: object) -> None:  # keep pytest output quiet
        return


@pytest.fixture
def openai_only_gateway() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Start a mock gateway whose OpenAI surface 404s; yield its base URL."""
    _GatewayHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/openai/v1", _GatewayHandler.requests
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def pi_home(tmp_path: Path, openai_only_gateway: tuple[str, list[Any]]) -> Path:
    """A fake ``HOME`` seeded with the openai-only gateway config."""
    base_url, _ = openai_only_gateway
    config_home = tmp_path / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\n"
        "providers:\n"
        "  rpw-fable:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    openai:\n"
        f"      base_url: {base_url}\n"
        "      api_key: test-gateway-key\n"
        "      wire_api: chat\n"
        "      models:\n"
        f"        default: {_CLAUDE_MODEL}\n"
    )
    return tmp_path


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _routing_warning_present(items: list[dict[str, Any]]) -> bool:
    for item in items:
        code = str(item.get("code") or "")
        message = str(item.get("message") or "")
        # The raw misroute failure is NOT the routing warning under test.
        if "404 status code" in message and code != "pi_credentials_unresolved":
            continue
        low = message.lower()
        if _CLAUDE_MODEL in low and any(
            token in low
            for token in (
                "rout",
                "family",
                "anthropic",
                "can't be served",
                "won't reply",
            )
        ):
            return True
    return False


def _fetch_items(server: str, conv: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{server}/v1/sessions/{conv}/items", timeout=30)
    resp.raise_for_status()
    return list(resp.json().get("data", []))


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("", "", False),
        ("RuntimeError", "Pi model error: 404 status code (no body)", False),
        ("pi_credentials_unresolved", "Gateway credentials missing.", False),
        (
            "pi_credentials_unresolved",
            "Model 'gpt-other' requires another routing family.",
            False,
        ),
        (
            "pi_credentials_unresolved",
            "Model 'claude-fable-5-1' needs gateway credentials.",
            False,
        ),
        (
            "pi_credentials_unresolved",
            "Model 'claude-fable-5-1' requires the Anthropic family instead of OpenAI routing.",
            True,
        ),
        (
            "RuntimeError",
            "Model 'claude-fable-5-1' can't be served by this provider.",
            True,
        ),
    ],
)
def test_routing_warning_requires_model_family_explanation(
    code: str, message: str, expected: bool
) -> None:
    assert _routing_warning_present([{"code": code, "message": message}]) is expected


def test_pi_native_openai_only_gateway_claude_model_fails_loud(
    pi_home: Path,
    openai_only_gateway: tuple[str, list[dict[str, Any]]],
) -> None:
    _, gateway_requests = openai_only_gateway

    omnigent_bin = Path(sys.executable).parent / "omnigent"
    assert omnigent_bin.exists(), f"omnigent CLI not found at {omnigent_bin}"

    env = {
        **os.environ,
        "HOME": str(pi_home),
        "OMNIGENT_CONFIG_HOME": str(pi_home / ".omnigent"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "PYTHONPATH": os.pathsep.join(
            str(p)
            for p in (
                _REPO_ROOT,
                _REPO_ROOT / "sdks" / "python-client",
                _REPO_ROOT / "sdks" / "ui",
            )
        ),
        # A real TERM so the runner-owned Pi tmux pane attaches under the pty.
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
    }
    env.pop("OMNIGENT_CONFIG", None)

    child = pexpect.spawn(
        str(omnigent_bin),
        ["pi", "--server", ""],  # auto-spawn a local server + runner
        cwd=str(_REPO_ROOT),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 140),
        timeout=_LAUNCH_TIMEOUT,
    )

    try:
        child.expect(r"Web UI:\s*(\S+)", timeout=_LAUNCH_TIMEOUT)
        web_url = child.match.group(1)
        match = re.match(r"(https?://[^/]+)/c/(\S+)", web_url)
        assert match, f"could not parse Web UI url: {web_url!r}"
        server, conv = match.group(1), match.group(2)

        # Poll the session for the routing warning the fix must surface.
        deadline = time.monotonic() + 60
        items: list[dict[str, Any]] = []
        warned = False
        turn_sent = False
        while time.monotonic() < deadline:
            items = _fetch_items(server, conv)
            if _routing_warning_present(items):
                warned = True
                break
            if not turn_sent:
                with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
                    child.expect(_CLAUDE_MODEL, timeout=3)
                    time.sleep(8)  # let prompt_toolkit's input loop go live before typing
                    child.send("Reply with exactly the single word: PONG")
                    time.sleep(1)
                    child.send("\r")
                    turn_sent = True
            time.sleep(3)

        # Diagnostic context for a failure: what the gateway actually saw and
        # what items the session recorded.
        posted_models = [r.get("model") for r in gateway_requests if r.get("method") == "POST"]
        item_summ = [
            {"type": it.get("type"), "code": it.get("code"), "message": it.get("message")}
            for it in items
            if it.get("type") in ("error", "notice", "message")
        ]
        assert warned, (
            "pi-native silently misrouted the Claude-family model "
            f"{_CLAUDE_MODEL!r} to the gateway's OpenAI /chat/completions surface "
            "with no routing warning surfaced to the user. "
            f"gateway POST models={posted_models!r}; session items={item_summ!r}. "
            "Expected a fail-loud routing warning (e.g. a "
            "'pi_credentials_unresolved' notice) instead of a silent "
            "'404 status code (no body)'."
        )
        assert all(model == _CLAUDE_MODEL for model in posted_models), (
            f"gateway POST models={posted_models!r}; expected {_CLAUDE_MODEL!r}"
        )
    finally:
        _teardown(child, env)


def _teardown(child: Any, env: dict[str, str]) -> None:
    """Detach the CLI and stop the auto-spawned server + local daemon.

    ``omnigent pi`` reaps its runner-owned Pi/tmux via the daemon, so
    ``omni server stop`` (which stops the managed server and the local daemon)
    is the safe teardown. We deliberately avoid a broad ``pkill -f`` on tokens
    like ``pi_native``/``tmux`` here - the pytest command line itself contains
    this test's ``pi_native`` filename, so a broad pattern would kill the test
    runner.
    """
    with contextlib.suppress(Exception):
        child.kill(signal.SIGTERM)
    time.sleep(2)
    with contextlib.suppress(Exception):
        child.kill(signal.SIGKILL)
    omni_bin = Path(sys.executable).parent / "omni"
    if omni_bin.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                [str(omni_bin), "server", "stop"],
                env=env,
                capture_output=True,
                timeout=60,
            )

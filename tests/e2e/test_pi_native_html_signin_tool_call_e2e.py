"""Exercise the shipped Pi extension's registered MCP callback under Node.

A synthetic HTTP 200 HTML sign-in response must make the callback throw a
bounded authentication classification without response body text. A valid
MCP JSON response must return the successful tool result.

The runner generates the extension and config through ``write_extension_files``.
The fixture stubs HTTP and the Pi registration API, then calls ``execute``
directly. It observes callback return or throw, not an SDK tool-result journal.
Pi and Prime SDK error conversion has separate selected-source tests.

Usage::

    python -m pytest tests/e2e/test_pi_native_html_signin_tool_call_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Literal, TypedDict

import pytest

from omnigent.harnesses.pi_native import bridge as pi_native_bridge

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to execute the pi-native extension",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_NAME = "sys_os_shell"
_CSRF_SECRET = "CSRF-SECRET-abc123"
_STATE_SECRET = "STATE-SECRET-tenant-acme-xyz"

_SIGNIN_HTML = (
    "<!DOCTYPE html><html><head><title>Sign in</title>"
    f'<meta name="csrf-token" content="{_CSRF_SECRET}">'
    '</head><body><form action="/login" method="post">'
    f'<input type="hidden" name="state" value="{_STATE_SECRET}">'
    "<button>Sign in</button></form>Please sign in to continue.</body></html>"
)

_BODY_MARKERS = ("<!DOCTYPE", "<html", "<head", "<form", "<input", _CSRF_SECRET, _STATE_SECRET)


class _Returned(TypedDict):
    kind: Literal["returned"]
    result: dict


class _Thrown(TypedDict):
    kind: Literal["thrown"]
    name: str
    message: str
    isErrorInstance: bool


_CallbackOutcome = _Returned | _Thrown


def _prepare_bridge(tmp_path: Path) -> tuple[Path, Path]:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    extension_path, config_path = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_html_signin_e2e",
        server_url="https://omnigent.example.internal",
        conversation_url="https://omnigent.example.internal/c/conv_html_signin_e2e",
        auth_headers={"authorization": "Bearer test-token"},
        tools=[
            {
                "name": _TOOL_NAME,
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            }
        ],
    )
    return extension_path, config_path


def _drive_tool_call(
    tmp_path: Path,
    *,
    extension_path: Path,
    config_path: Path,
    response_body: str = _SIGNIN_HTML,
    content_type: str = "text/html; charset=utf-8",
) -> _CallbackOutcome:
    script = tmp_path / "drive_html_signin.mjs"
    script.write_text(
        textwrap.dedent(
            r"""
            import { createRequire } from "module";

            const require = createRequire(import.meta.url);
            const extensionPath = process.env.PI_NATIVE_EXTENSION_PATH;
            const toolName = process.env.PI_NATIVE_TOOL_NAME;
            const responseBody = process.env.PI_NATIVE_RESPONSE_BODY;
            const contentType = process.env.PI_NATIVE_CONTENT_TYPE;

            globalThis.fetch = async (url) => {
              if (String(url).endsWith("/mcp")) {
                return new Response(responseBody, {
                  status: 200,
                  headers: { "content-type": contentType },
                });
              }
              return new Response("{}", {
                status: 200,
                headers: { "content-type": "application/json" },
              });
            };

            const registered = new Map();
            const pi = {
              on() {},
              registerCommand() {},
              sendUserMessage() {},
              registerTool(spec) {
                if (spec && spec.name) registered.set(spec.name, spec);
              },
            };

            require(extensionPath)(pi);

            const tool = registered.get(toolName);
            if (!tool || typeof tool.execute !== "function") {
              console.error(`tool ${toolName} was not registered`);
              process.exit(3);
            }

            let outcome;
            try {
              const result = await tool.execute("call-1", { command: "echo hi" });
              outcome = { kind: "returned", result };
            } catch (error) {
              outcome = { kind: "thrown", name: error?.name,
                message: error?.message, isErrorInstance: error instanceof Error };
            }
            process.stdout.write(JSON.stringify(outcome));
            """
        ),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PI_NATIVE_EXTENSION_PATH": str(extension_path),
        "OMNIGENT_PI_NATIVE_CONFIG": str(config_path),
        "PI_NATIVE_TOOL_NAME": _TOOL_NAME,
        "PI_NATIVE_RESPONSE_BODY": response_body,
        "PI_NATIVE_CONTENT_TYPE": content_type,
    }
    proc = subprocess.run(
        ["node", str(script)],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"node scenario failed: rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_html_signin_at_mcp_boundary_is_a_bounded_auth_error(tmp_path: Path) -> None:
    """A synthetic HTML response throws the bounded classification directly."""
    extension_path, config_path = _prepare_bridge(tmp_path)
    outcome = _drive_tool_call(tmp_path, extension_path=extension_path, config_path=config_path)

    assert outcome == {
        "kind": "thrown",
        "name": "Error",
        "message": (
            "Omnigent tool call failed: the server answered 2xx with a non-JSON "
            "body, which usually means an authenticating proxy or sign-in page "
            "answered instead of Omnigent. Re-authenticate and retry."
        ),
        "isErrorInstance": True,
    }
    assert not any(marker in json.dumps(outcome) for marker in _BODY_MARKERS)


def test_json_mcp_response_returns_success(tmp_path: Path) -> None:
    """A successful callback remains distinguishable from a thrown failure."""
    extension_path, config_path = _prepare_bridge(tmp_path)
    outcome = _drive_tool_call(
        tmp_path,
        extension_path=extension_path,
        config_path=config_path,
        response_body='{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"hi"}],"isError":false}}',
        content_type="application/json",
    )

    assert outcome == {
        "kind": "returned",
        "result": {"content": [{"type": "text", "text": "hi"}], "isError": False},
    }

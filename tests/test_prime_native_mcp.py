from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.prime_native import bridge, main, process
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.runner.native.orchestration import NativeLaunchContext
from omnigent.spec.types import AgentSpec, MCPServerConfig


@pytest.fixture(autouse=True)
def private_prime_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact")
    monkeypatch.setattr(process, "_ACTIVE_RUNTIMES", set())
    monkeypatch.setattr(main, "resolve_prime_executable", lambda: "/qualified/prime-agent")
    monkeypatch.setattr(main, "complete_prime_launch", lambda *_args: None)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://omnigent.test")


def _mcp_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="prime-mcp-fixture",
        instructions="Use the declared tools when they help.",
        mcp_servers=[MCPServerConfig(name="adapter_fixture", transport="http", url="http://mcp")],
    )


async def _launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: AgentSpec,
    mcp_response: dict[str, object] | httpx.Response | None = None,
) -> tuple[dict[str, Any], list[httpx.Request], dict[str, Any]]:
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    monkeypatch.setattr(main, "runtime_paths", lambda _: paths)
    requests: list[httpx.Request] = []

    async def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/mcp"):
            assert mcp_response is not None
            if isinstance(mcp_response, httpx.Response):
                return mcp_response
            return httpx.Response(200, json=mcp_response)
        return httpx.Response(200, json={"workspace": str(tmp_path)})

    view = SessionResourceView(
        id="terminal_prime-native_main",
        type="terminal",
        session_id="conv_prime",
        name="prime-native",
    )
    registry = AsyncMock()
    registry.launch_required_terminal.return_value = view
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://omnigent.test"
    ) as client:
        await main.launch_prime_terminal(
            NativeLaunchContext(
                session_id="conv_prime",
                resource_registry=registry,
                publish_event=lambda _session_id, _event: None,
                server_client=client,
                agent_spec=spec,
            )
        )
    terminal = registry.launch_required_terminal.call_args.kwargs["spec"]
    return json.loads((paths.root / "config.json").read_text()), requests, {"args": terminal.args}


@pytest.mark.asyncio
async def test_launch_advertises_declared_mcp_schema_to_prime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = {
        "type": "function",
        "name": "adapter_fixture__echo",
        "description": "Echo the provided message.",
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    }
    config, requests, terminal = await _launch(
        tmp_path,
        monkeypatch,
        _mcp_spec(),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": schema["name"],
                        "description": schema["description"],
                        "inputSchema": schema["parameters"],
                    }
                ],
            },
        },
    )

    advertised = next(tool for tool in config["tools"] if tool["name"] == "adapter_fixture__echo")
    assert advertised == schema
    mcp_request = next(request for request in requests if request.url.path.endswith("/mcp"))
    assert json.loads(mcp_request.content) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    prompt = terminal["args"][terminal["args"].index("--append-system-prompt") + 1]
    assert "Use the declared tools when they help." in prompt


@pytest.mark.asyncio
async def test_launch_without_declared_mcp_skips_proxy_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, requests, _terminal = await _launch(
        tmp_path,
        monkeypatch,
        AgentSpec(spec_version=1, name="prime-no-mcp"),
    )

    assert not [request for request in requests if request.url.path.endswith("/mcp")]
    assert any(tool["name"] == "sys_os_read" for tool in config["tools"])


@pytest.mark.asyncio
async def test_launch_remains_usable_when_mcp_proxy_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config, _requests, _terminal = await _launch(
        tmp_path,
        monkeypatch,
        _mcp_spec(),
        httpx.Response(503, text="MCP proxy unavailable"),
    )

    names = {tool["name"] for tool in config["tools"]}
    assert "adapter_fixture__echo" not in names
    assert "sys_os_read" in names
    assert "Prime Native MCP 'proxy' unavailable at launch: HTTPStatusError" in caplog.text

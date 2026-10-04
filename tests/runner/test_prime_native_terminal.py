from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import psutil
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.prime_native import bridge, main, process
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.native.native_dispatch import resolve_hook_for_key
from omnigent.runner.native.interrupt import NativeInterruptRunner
from omnigent.runner.native.orchestration import NativeLaunchContext
from omnigent.runtime.prompt import EMBEDDED_BROWSER_PRIORITY_INSTRUCTION
from omnigent.spec.types import AgentSpec, ExecutorSpec


@dataclass(frozen=True)
class _DeliveryAck:
    delivered: bool = True
    entry: object | None = None
    reason: str = ""


class _EmptyTerminalRegistry:
    def list_for_conversation(self, _session_id: str) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_stop_during_prime_launch_preparation_reports_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner.native import orchestration

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    published: list[tuple[str, dict[str, object]]] = []

    async def blocked_config(**_kwargs: object) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("configuration failed before terminal dispatch")

    def acknowledge(child_session_id: str, *, status: str, output: str | None) -> _DeliveryAck:
        return _DeliveryAck()

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", blocked_config)
    registry = AsyncMock()
    registry.terminal_registry = _EmptyTerminalRegistry()
    async with httpx.AsyncClient(base_url="http://omnigent.test") as client:
        ctx = NativeLaunchContext(
            session_id="conv_prime_overlap",
            resource_registry=registry,
            publish_event=lambda session_id, event: published.append((session_id, event)),
            server_client=client,
        )
        runner = NativeInterruptRunner(
            server_client=client,
            resource_registry=registry,
            publish_event=lambda session_id, event: published.append((session_id, event)),
            mark_subagent_terminal_and_wake=acknowledge,
            session_sub_agent_names={},
            codex_bridge_state_for_session=AsyncMock(return_value=None),
            client_safe_error_detail=lambda exc, *, context: f"{context}: {exc}",
            logger=logging.getLogger(__name__),
        )
        launched = asyncio.create_task(main.launch_prime_terminal(ctx))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            response = await runner.stop("prime-native", ctx.session_id)
            assert response is not None and response.status_code == 503
            assert published == []
        finally:
            release.set()
            with pytest.raises(
                RuntimeError, match="configuration failed before terminal dispatch"
            ):
                await launched
        assert not (bridge.runtime_paths(ctx.session_id).root / "launch.pending.json").exists()
        await asyncio.to_thread(process.stop_session, ctx.session_id)


@pytest.mark.asyncio
async def test_prime_launch_reservation_survives_terminal_dispatch_until_identity_is_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(process, "_ACTIVE_RUNTIMES", set())
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    monkeypatch.setattr(main, "runtime_paths", lambda _: paths)
    monkeypatch.setattr(process, "runtime_paths", lambda _: paths)
    monkeypatch.setattr(main, "resolve_prime_executable", lambda: "/qualified/prime-agent")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    entered = asyncio.Event()
    release = asyncio.Event()
    view = SessionResourceView(
        id="terminal_prime-native_main",
        type="terminal",
        session_id="conv_prime_dispatch",
        name="prime-native",
    )
    registry = AsyncMock()

    async def delayed_terminal(**_kwargs: object) -> SessionResourceView:
        entered.set()
        await release.wait()
        return view

    registry.launch_required_terminal.side_effect = delayed_terminal

    def snapshot(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workspace": str(tmp_path)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(snapshot), base_url="http://omnigent.test"
    ) as client:
        context = NativeLaunchContext(
            session_id="conv_prime_dispatch",
            resource_registry=registry,
            publish_event=lambda *_event: None,
            server_client=client,
        )
        launched = asyncio.create_task(main.launch_prime_terminal(context))
        child: subprocess.Popen[str] | None = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            with pytest.raises(RuntimeError, match="Prime terminal launch is in progress"):
                await asyncio.to_thread(process.stop_session, context.session_id)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env={**os.environ, **paths.env},
                text=True,
            )
            (paths.root / "terminal.json").write_text(
                json.dumps(
                    {"pid": child.pid, "created_at": psutil.Process(child.pid).create_time()}
                ),
                encoding="utf-8",
            )
            release.set()
            assert await asyncio.wait_for(launched, timeout=5) == view
            assert not (paths.root / "launch.pending.json").exists()
            assert child.poll() is None
        finally:
            release.set()
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            if not launched.done():
                await launched


@pytest.mark.asyncio
async def test_provider_launch_owns_terminal_and_reuses_only_native_saved_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(process, "_ACTIVE_RUNTIMES", set())
    paths = PrimeRuntimePaths(tmp_path / "prime-native" / "runtime")
    monkeypatch.setattr(main, "runtime_paths", lambda _: paths)
    monkeypatch.setattr(main, "complete_prime_launch", lambda *_args: None)
    monkeypatch.setattr(main, "resolve_prime_executable", lambda: "/qualified/prime-agent")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://omnigent.test")
    published = []
    view = SessionResourceView(
        id="terminal_prime-native_main",
        type="terminal",
        session_id="conv_prime",
        name="prime-native",
    )
    registry = AsyncMock()
    registry.launch_required_terminal.return_value = view

    async def snapshot(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workspace": str(tmp_path),
                "terminal_launch_args": ["--offline"],
                "external_session_id": "prime-saved",
                "model_override": "local/fixture",
                "reasoning_effort": "high",
            },
        )

    spec = AgentSpec(
        spec_version=1,
        name="custom-prime",
        instructions="Keep author instructions.",
        executor=ExecutorSpec(model="ignored-default"),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(snapshot), base_url="http://omnigent.test"
    ) as client:
        context = NativeLaunchContext(
            session_id="conv_prime",
            resource_registry=registry,
            publish_event=lambda *event: published.append(event),
            server_client=client,
            agent_spec=spec,
        )
        launch = resolve_hook_for_key("prime-native", "auto_create_terminal")
        assert launch is not None
        assert await launch(context) == view
    call = registry.launch_required_terminal.call_args.kwargs
    assert call["terminal_name"] == "prime-native"
    assert call["resource_role"] == "prime-native"
    terminal = call["spec"]
    assert terminal.command == sys.executable
    assert terminal.args[:4] == [
        "-m",
        "omnigent.harnesses.prime_native.process",
        str(paths.root),
        "/qualified/prime-agent",
    ]
    assert terminal.args[terminal.args.index("--resume") + 1] == "prime-saved"
    assert terminal.args[terminal.args.index("--model") + 1] == "local/fixture"
    assert terminal.args[terminal.args.index("--thinking") + 1] == "high"
    instructions = terminal.args[terminal.args.index("--append-system-prompt") + 1]
    assert instructions.startswith("Keep author instructions.")
    assert instructions.endswith(EMBEDDED_BROWSER_PRIORITY_INSTRUCTION)
    assert "--session" not in terminal.args and "--approve" not in terminal.args
    assert terminal.env["TMPDIR"] == str(paths.temp_dir)
    assert terminal.env["PRIME_AGENT_CODING_AGENT_DIR"] == str(paths.agent_dir)
    assert list(paths.session_dir.iterdir()) == []
    config = json.loads((paths.root / "config.json").read_text())
    assert config["agentLabel"] == "Prime Native"
    assert config["sessionId"] == "conv_prime"
    assert any(tool["name"] == "sys_os_read" for tool in config["tools"])
    assert published[0][1]["type"] == "session.resource.created"

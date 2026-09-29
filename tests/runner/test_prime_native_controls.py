from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.prime_native.controls import (
    Compact,
    ControlOutcome,
    ControlStatus,
    Interrupt,
    PrimeExtensionBinding,
    SetEffort,
    SetModel,
)
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient
from tests.runner.test_native_interrupt_runner import _make_runner


@pytest.fixture(autouse=True)
def no_real_terminal(monkeypatch):
    monkeypatch.setattr("omnigent.harnesses.prime_native.main.launch_prime_terminal", AsyncMock())


async def prime_app(frames=None, terminal_registry=None):
    spec = AgentSpec(
        spec_version=1,
        name="prime-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "prime-native"}),
    )

    async def resolver(agent_id, session_id=None):
        return spec

    harness = _ScriptedHarnessClient(frames or [])
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),
        spec_resolver=resolver,
        server_client=NullServerClient(),
        terminal_registry=terminal_registry,
    )
    app.state.test_harness = harness
    return app


def write_live_binding(root: Path, *, heartbeat: float | None = None) -> None:
    controls = root / "controls"
    controls.mkdir(parents=True, exist_ok=True)
    (controls / "binding.json").write_text(
        json.dumps(
            {
                "incarnation": "root",
                "pid": os.getpid(),
                "heartbeat": time.time() * 1000 if heartbeat is None else heartbeat,
            }
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    [
        "clear",
        "reset",
        "plan_mode_change",
        "permission_mode_change",
        "codex_approval_mode_change",
        "btw_dismiss",
        "cost_approval_popup",
        "policy_blocked_notice",
    ],
)
async def test_prime_reachable_unsupported_controls_have_stable_errors(control: str) -> None:
    app = await prime_app()
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": "prime-control", "agent_id": "agent"}
        )
        assert created.status_code == 201
        response = await client.post("/v1/sessions/prime-control/events", json={"type": control})
    assert response.status_code == 409
    assert response.json()["error"] == "prime_control_unsupported"


@pytest.mark.asyncio
async def test_runner_prime_uses_private_binding_and_effective_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.prime_native import bridge

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    calls = []

    async def execute(self, control, *, timeout_s=18):
        calls.append((self._root, control))
        return ControlOutcome(ControlStatus.APPLIED, effort="low")

    monkeypatch.setattr(PrimeExtensionBinding, "execute", execute)
    app = await prime_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions", json={"session_id": "prime-control", "agent_id": "agent"}
        )
        for payload in [
            {"type": "model_change", "model": "verify/fixture"},
            {"type": "effort_change", "effort": "high"},
        ]:
            response = await client.post("/v1/sessions/prime-control/events", json=payload)
            assert response.status_code == 200
            assert response.json()["effort"] == "low"
    assert calls[0][0] == bridge.runtime_paths("prime-control").root / "controls"
    assert isinstance(calls[0][1], SetModel)
    assert calls[0][1].model.selector == "verify/fixture"
    assert calls[1][1] == SetEffort("high")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["none", "holder", "waiter"])
async def test_prime_settings_fifo_survives_cancellation_and_controls_bypass(
    monkeypatch, cancel
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    native = {"effort": "medium"}
    order = []

    async def execute(self, control, *, timeout_s=18):
        if isinstance(control, Compact):
            return ControlOutcome(ControlStatus.APPLIED)
        if isinstance(control, Interrupt):
            return ControlOutcome(ControlStatus.INTERRUPT_ACCEPTED)
        assert isinstance(control, SetEffort)
        order.append(control.effort)
        if control.effort == "high":
            started.set()
            await release.wait()
        native["effort"] = control.effort
        return ControlOutcome(ControlStatus.APPLIED, effort=control.effort)

    monkeypatch.setattr(PrimeExtensionBinding, "execute", execute)
    app = await prime_app()
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": "fifo", "agent_id": "agent"})

        async def setting(effort):
            return await client.post(
                "/v1/sessions/fifo/events", json={"type": "effort_change", "effort": effort}
            )

        first = asyncio.create_task(setting("high"))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(setting("low"))
        await asyncio.sleep(0.02)
        compact = await client.post("/v1/sessions/fifo/events", json={"type": "compact"})
        assert compact.status_code == 200
        interrupt = await client.post("/v1/sessions/fifo/events", json={"type": "interrupt"})
        assert interrupt.status_code == 202
        assert interrupt.json()["status"] == "interrupt_accepted"
        assert native == {"effort": "medium"}
        if cancel != "none":
            cancelled = first if cancel == "holder" else second
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
        third = asyncio.create_task(setting("minimal"))
        await asyncio.sleep(0.02)
        release.set()
        responses = await asyncio.gather(
            *(task for task in (first, second, third) if not task.cancelled())
        )
        assert all(response.status_code == 200 for response in responses)
        assert native == {"effort": "minimal"}
        assert order == (["high", "minimal"] if cancel == "waiter" else ["high", "low", "minimal"])
        fourth = await setting("medium")
        assert fourth.json()["effort"] == "medium"
        assert native == {"effort": "medium"}


@pytest.mark.asyncio
async def test_prime_settings_waiting_and_execution_share_one_budget(monkeypatch) -> None:
    monkeypatch.setattr("omnigent.runner.app._PRIME_SETTINGS_TIMEOUT_S", 0.2)
    started = asyncio.Event()
    release = asyncio.Event()
    native = {"effort": "medium"}
    budgets = []

    async def execute(self, control, *, timeout_s=18):
        budgets.append(timeout_s)
        if control.effort == "high":
            started.set()
            await release.wait()
            return ControlOutcome(ControlStatus.UNKNOWN)
        native["effort"] = control.effort
        return ControlOutcome(ControlStatus.APPLIED, effort=control.effort)

    monkeypatch.setattr(PrimeExtensionBinding, "execute", execute)
    app = await prime_app()
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": "budget", "agent_id": "agent"})

        async def setting(effort):
            return await client.post(
                "/v1/sessions/budget/events", json={"type": "effort_change", "effort": effort}
            )

        first = asyncio.create_task(setting("high"))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(setting("low"))
        await asyncio.sleep(0.08)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.status_code == 504
        assert second_result.json()["effort"] == "low"
        assert native == {"effort": "low"}
        assert 0 < budgets[1] < budgets[0] - 0.04


@pytest.mark.asyncio
async def test_prime_waiter_expiry_does_not_create_a_second_gate(monkeypatch) -> None:
    monkeypatch.setattr("omnigent.runner.app._PRIME_SETTINGS_TIMEOUT_S", 0.04)
    started = asyncio.Event()
    release = asyncio.Event()
    native = {"effort": "medium"}

    async def execute(self, control, *, timeout_s=18):
        if control.effort == "high":
            started.set()
            await release.wait()
        native["effort"] = control.effort
        return ControlOutcome(ControlStatus.APPLIED, effort=control.effort)

    monkeypatch.setattr(PrimeExtensionBinding, "execute", execute)
    app = await prime_app()
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": "expired", "agent_id": "agent"})

        async def setting(effort):
            return await client.post(
                "/v1/sessions/expired/events", json={"type": "effort_change", "effort": effort}
            )

        first = asyncio.create_task(setting("high"))
        await asyncio.wait_for(started.wait(), 1)
        try:
            second = await setting("low")
            assert second.status_code == 503
            third = asyncio.create_task(setting("minimal"))
            await asyncio.sleep(0.01)
            assert native == {"effort": "medium"}
        finally:
            release.set()
            await first
        assert (await third).json()["effort"] == "minimal"
        assert native == {"effort": "minimal"}


@pytest.mark.asyncio
async def test_prime_reverse_uuid_files_follow_runner_admission(monkeypatch, tmp_path) -> None:
    from omnigent.harnesses.prime_native import bridge, controls

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    root = bridge.runtime_paths("uuid-order").root
    write_live_binding(root)
    request_dir = root / "controls" / "requests"
    result_dir = root / "controls" / "results"
    request_dir.mkdir()
    result_dir.mkdir()
    native = {"effort": "medium"}
    applied = []
    app = await prime_app()
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": "uuid-order", "agent_id": "agent"})
        ids = iter(["f".zfill(32), "1".zfill(32)])
        monkeypatch.setattr(
            controls, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(ids)))
        )

        async def setting(effort):
            return await client.post(
                "/v1/sessions/uuid-order/events", json={"type": "effort_change", "effort": effort}
            )

        async def consume():
            while len(applied) < 2:
                for file in sorted(request_dir.glob("*.json")):
                    request = json.loads(file.read_text())
                    file.unlink()
                    native["effort"] = request["level"]
                    applied.append(native["effort"])
                    (result_dir / file.name).write_text(
                        json.dumps(
                            {
                                "id": request["id"],
                                "incarnation": "root",
                                "status": "applied",
                                "effort": native["effort"],
                            }
                        )
                    )
                await asyncio.sleep(0.005)

        first = asyncio.create_task(setting("high"))
        for _ in range(100):
            if list(request_dir.glob("*.json")):
                break
            await asyncio.sleep(0.005)
        second = asyncio.create_task(setting("low"))
        await asyncio.sleep(0.03)
        consumer = asyncio.create_task(consume())
        first_result, second_result, _ = await asyncio.wait_for(
            asyncio.gather(first, second, consumer), 2
        )
        assert first_result.json()["effort"] == "high"
        assert second_result.json()["effort"] == "low"
        assert applied == ["high", "low"]
        assert native == {"effort": "low"}
        assert list(request_dir.iterdir()) == []
        assert list(result_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_prime_interrupt_acceptance_does_not_publish_idle_or_wake_parent(
    monkeypatch,
) -> None:
    async def execute(self, control, *, timeout_s):
        assert timeout_s < 5
        return ControlOutcome(ControlStatus.INTERRUPT_ACCEPTED)

    monkeypatch.setattr(PrimeExtensionBinding, "execute", execute)
    runner, captured = _make_runner()
    response = await runner.interrupt("prime-native", "s")
    assert response.status_code == 202
    assert json.loads(response.body)["status"] == "interrupt_accepted"
    assert captured == {"published": [], "wakes": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_prime_delivery_does_not_publish_completed_or_idle(
    stream: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.prime_native import bridge

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    write_live_binding(bridge.runtime_paths("prime-delivery").root)
    app = await prime_app(
        [
            _sse({"type": "response.created", "response": {"id": "delivery"}}),
            _sse({"type": "response.completed", "response": {"id": "delivery"}}),
        ]
    )
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions", json={"session_id": "prime-delivery", "agent_id": "agent"}
        )
        from omnigent.runner.app import _session_event_queues_ref

        queue = _session_event_queues_ref.setdefault("prime-delivery", asyncio.Queue())
        _drain_session_event_queue(queue)
        response = await client.post(
            f"/v1/sessions/prime-delivery/events?stream={str(stream).lower()}",
            json={
                "type": "message",
                "role": "user",
                "model_override": "verify/fixture",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )
        assert response.status_code == 202
        await asyncio.sleep(0.1)
        assert app.state.test_harness.posted_bodies[0]["content"][-1]["content"] == [
            {"type": "input_text", "text": "hello"}
        ]
        events = _drain_session_event_queue(queue)
        assert not [
            event
            for event in events
            if event.get("type") == "response.completed" or event.get("status") == "idle"
        ], events
        observed = await client.post(
            "/v1/sessions/prime-delivery/events",
            json={"type": "external_session_status", "data": {"status": "waiting"}},
        )
        assert observed.status_code == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_prime_message_rejects_unavailable_binding_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    from omnigent.harnesses.prime_native import bridge

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    root = bridge.runtime_paths("prime-unavailable").root
    if stale:
        write_live_binding(root, heartbeat=0)
    app = await prime_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions", json={"session_id": "prime-unavailable", "agent_id": "agent"}
        )
        response = await client.post(
            "/v1/sessions/prime-unavailable/events",
            json={"type": "message", "content": [{"type": "input_text", "text": "hello"}]},
        )
        assert response.status_code == 503
        assert response.json()["error"] == "prime_control_unavailable"
        assert app.state.test_harness.posted_bodies == []
        assert app.state.has_active_work() is False
        write_live_binding(root)
        accepted = await client.post(
            "/v1/sessions/prime-unavailable/events",
            json={"type": "message", "content": [{"type": "input_text", "text": "ready"}]},
        )
        assert accepted.status_code == 202
        await asyncio.sleep(0.1)
        assert app.state.test_harness.posted_bodies[0]["content"][-1]["content"] == [
            {"type": "input_text", "text": "ready"}
        ]


@pytest.mark.asyncio
async def test_prime_message_observes_fresh_root_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.prime_native import bridge

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    root = bridge.runtime_paths("prime-starting").root
    app = await prime_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions", json={"session_id": "prime-starting", "agent_id": "agent"}
        )

        async def start_root() -> None:
            await asyncio.sleep(0.05)
            write_live_binding(root)

        startup = asyncio.create_task(start_root())
        response = await client.post(
            "/v1/sessions/prime-starting/events?stream=true",
            json={"type": "message", "content": [{"type": "input_text", "text": "first"}]},
        )
        assert response.status_code == 202
        assert startup.done()
        await startup
        await asyncio.sleep(0.1)
        assert app.state.test_harness.posted_bodies[0]["content"][-1]["content"] == [
            {"type": "input_text", "text": "first"}
        ]


@pytest.mark.asyncio
async def test_quiet_native_waiting_protects_the_pane_reaper(tmp_path):
    from omnigent.terminals import TerminalRegistry
    from omnigent.terminals.pane_reaper import PaneRef

    app = await prime_app(terminal_registry=TerminalRegistry())
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": "quiet-prime", "agent_id": "agent"})
        response = await client.post(
            "/v1/sessions/quiet-prime/events",
            json={
                "type": "external_session_status",
                "data": {"status": "waiting"},
            },
        )
        assert response.status_code == 204
        pane = PaneRef(
            "quiet-prime", "prime-native:main", "prime-native", tmp_path / "missing.sock"
        )
        assert await app.state.native_pane_reaper._is_busy(pane) is True
        assert app.state.has_active_work() is True


@pytest.mark.asyncio
async def test_delivery_stream_end_preserves_native_ownership_and_accepts_steering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.prime_native import bridge
    from omnigent.terminals import TerminalRegistry
    from omnigent.terminals.pane_reaper import PaneRef

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    session_id = "prime-native-ownership"
    write_live_binding(bridge.runtime_paths(session_id).root)
    app = await prime_app(
        [_sse({"type": "response.completed", "response": {"id": "delivery"}})],
        terminal_registry=TerminalRegistry(),
    )
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": session_id, "agent_id": "agent"})
        observed = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_status", "data": {"status": "running"}},
        )
        assert observed.status_code == 204
        registry = app.state.session_resource_registry
        resident = SimpleNamespace(is_alive=AsyncMock(return_value=True))
        monkeypatch.setattr(registry.terminal_registry, "get", lambda *args: resident)
        pane = PaneRef(session_id, "prime-native:main", "prime-native", tmp_path / "missing.sock")
        delivered = []
        for text in ("first input", "steer the active native loop"):
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "message", "content": [{"type": "input_text", "text": text}]},
            )
            assert response.status_code == 202
            async with asyncio.timeout(2):
                while session_id in app.state.active_turns:
                    await asyncio.sleep(0.01)
            assert app.state.native_pane_status[session_id] == "running"
            assert registry.session_turn_is_active(session_id) is True
            assert app.state.has_active_work() is True
            assert await app.state.native_pane_reaper._is_busy(pane) is True
            delivered.append(app.state.test_harness.posted_bodies[-1]["content"][-1]["content"])
        assert delivered == [
            [{"type": "input_text", "text": "first input"}],
            [{"type": "input_text", "text": "steer the active native loop"}],
        ]
        observed = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_status", "data": {"status": "idle"}},
        )
        assert observed.status_code == 204
        assert registry.session_turn_is_active(session_id) is False
        assert app.state.has_active_work() is False

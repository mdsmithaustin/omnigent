from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.native.source_owner import NativeStopOutcome
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals.registry import TerminalRegistry
from tests.native_source_helpers import native_source_server as native_source_server
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient


@pytest.mark.asyncio
async def test_delayed_native_launch_cannot_start_after_original_admission_is_stopped(
    native_source_server,
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/validate"):
            entered.set()
            await release.wait()
        return native_source_server.respond(request)

    terminals = TerminalRegistry()
    launch = AsyncMock(side_effect=AssertionError("revoked native process was started"))
    terminals.launch = launch
    registry = SessionResourceRegistry(terminal_registry=terminals)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://source"
    ) as client:
        registry.native_server_client = client
        task = asyncio.create_task(
            registry.launch_required_terminal(
                "delayed-source",
                "codex",
                "main",
                TerminalEnvSpec(command="codex"),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        source_id = native_source_server.source("delayed-source")
        stop = native_source_server.store.seal_native_stop(source_id)
        native_source_server.store.finish_native_stop(stop, NativeStopOutcome.VERIFIED)
        release.set()
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await task
        assert failure.value.response.status_code == 409
    assert launch.await_count == 0


@pytest.mark.asyncio
async def test_generic_prime_ensure_uses_registered_prime_launch_and_agent_context(
    native_source_server, monkeypatch
):
    source_id = "prime-ensure"
    spec = AgentSpec(
        spec_version=1,
        name="prime-ensure-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "prime-native"}),
    )
    view = SessionResourceView(
        id=terminal_resource_id("prime-native", "main"),
        type="terminal",
        session_id=source_id,
        name="Prime",
    )
    launched = []

    async def resolve_spec(agent_id, session_id=None):
        return spec

    async def launch(ctx):
        assert ctx.agent_spec == spec
        launched.append(ctx.session_id)
        return view

    async def existing(self, session_id, terminal_id):
        return view if launched else None

    monkeypatch.setattr("omnigent.harnesses.prime_native.main.launch_prime_terminal", launch)
    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", existing)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: native_source_server.respond(request, {"agent_id": "agent"})
        ),
        base_url="http://source",
    ) as server:
        app = create_runner_app(
            server_client=server,
            spec_resolver=resolve_spec,
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            for _ in range(2):
                response = await client.post(
                    f"/v1/sessions/{source_id}/resources/terminals",
                    json={
                        "terminal": "prime-native",
                        "session_key": "main",
                        "ensure_native_terminal": True,
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["id"] == "terminal_prime-native_main"
    assert launched == [source_id]


@pytest.mark.asyncio
async def test_queued_prime_setting_cannot_reach_successor_after_stop(
    native_source_server, monkeypatch, tmp_path
):
    import json
    import os
    import time

    from omnigent.harnesses.prime_native import bridge
    from omnigent.native.source_owner import NativeStopReceipt

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact")
    queued = asyncio.Event()
    first_written = asyncio.Event()
    spec = AgentSpec(
        spec_version=1,
        name="prime-controls",
        executor=ExecutorSpec(type="omnigent", config={"harness": "prime-native"}),
    )

    async def resolve_spec(agent_id, session_id=None):
        return spec

    def respond(request):
        response = native_source_server.respond(request, {"agent_id": "agent"})
        if first_written.is_set() and request.url.path.endswith("/native-admission"):
            queued.set()
        return response

    monkeypatch.setattr("omnigent.harnesses.prime_native.main.launch_prime_terminal", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://source"
    ) as server:
        app = create_runner_app(
            server_client=server,
            spec_resolver=resolve_spec,
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            initialized = await client.post(
                "/v1/sessions", json={"session_id": "queued-setting", "agent_id": "agent"}
            )
            assert initialized.status_code == 201, initialized.text
            root = bridge.runtime_paths("queued-setting").root / "controls"
            root.mkdir(parents=True, exist_ok=True)
            (root / "requests").mkdir(exist_ok=True)
            (root / "results").mkdir(exist_ok=True)
            binding = {
                "incarnation": "original",
                "pid": os.getpid(),
                "heartbeat": time.time() * 1000,
            }
            (root / "binding.json").write_text(json.dumps(binding))
            first = asyncio.create_task(
                client.post(
                    "/v1/sessions/queued-setting/events",
                    json={"type": "effort_change", "effort": "high"},
                )
            )

            async def wait_for_request():
                while not list((root / "requests").glob("*.json")):
                    if first.done():
                        response = await first
                        raise AssertionError(response.text)
                    await asyncio.sleep(0.005)
                return next((root / "requests").glob("*.json"))

            request_path = await asyncio.wait_for(wait_for_request(), 2)
            original_request = json.loads(request_path.read_text())
            assert original_request["incarnation"] == "original"
            assert original_request["type"] == "effort"
            first_written.set()
            second = asyncio.create_task(
                client.post(
                    "/v1/sessions/queued-setting/events",
                    json={"type": "effort_change", "effort": "low"},
                )
            )
            await asyncio.wait_for(queued.wait(), 2)
            source_id = native_source_server.source("queued-setting")
            stop = native_source_server.store.seal_native_stop(source_id)
            local_stop = stop.model_copy(
                update={
                    "admission": stop.admission.model_copy(update={"source_id": "queued-setting"})
                }
            )
            stopped = await asyncio.wait_for(
                client.post(
                    "/v1/sessions/queued-setting/events",
                    json={"type": "stop_session", "native_stop": local_stop.model_dump()},
                ),
                2,
            )
            assert stopped.status_code == 200, stopped.text
            receipt = NativeStopReceipt.model_validate(stopped.json())
            assert receipt.result.outcome == NativeStopOutcome.UNKNOWN
            assert not first.done()
            native_source_server.store.finish_native_stop(
                stop, receipt.result.outcome, detail=receipt.result.detail
            )
            native_source_server.store.admit_native(source_id, stop.admission.owner)
            binding["incarnation"] = "successor"
            (root / "binding.json").write_text(json.dumps(binding))
            result_dir = root / "results"
            result_dir.mkdir(exist_ok=True)
            (result_dir / request_path.name).write_text(
                json.dumps(
                    {
                        "id": original_request["id"],
                        "incarnation": "original",
                        "status": "applied",
                        "effort": "high",
                    }
                )
            )
            assert (await first).status_code == 200
            with pytest.raises(httpx.HTTPStatusError) as failure:
                await second
            assert failure.value.response.status_code == 409
            assert list((root / "requests").iterdir()) == []


def test_prime_successor_extension_rejects_predecessor_control_request(tmp_path):
    from tests.test_prime_native_controls import run_extension

    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
const before = JSON.parse(fs.readFileSync(path.join(primeControlsDir, "binding.json")));
const old = enqueue("model", { provider: "verify", modelId: "fixture" });
await handlers.session_shutdown({}, ctx);
await handlers.session_start({}, ctx);
const after = JSON.parse(fs.readFileSync(path.join(primeControlsDir, "binding.json")));
assert.notEqual(after.incarnation, before.incarnation);
await tick();
assert.equal(result(old).status, "rejected");
assert.deepEqual(calls, []);
assert.equal((await request("model", {
  provider: "verify", modelId: "fixture"
})).status, "applied");
assert.deepEqual(calls, ["verify/fixture"]);
""",
    )


@pytest.mark.asyncio
async def test_competing_control_writers_and_sync_stop_share_owner_lock_without_blocking_loop(
    native_source_server, monkeypatch, tmp_path
):
    import json
    import os
    import threading
    import time

    from omnigent.harnesses.prime_native import bridge
    from omnigent.harnesses.prime_native.controls import PrimeExtensionBinding, SetEffort
    from omnigent.native.admission import admit_native
    from omnigent.native.native_bridge_common import bridge_dir_preparation_lock

    monkeypatch.setattr(bridge, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact")
    entered = asyncio.Event()
    release = asyncio.Event()
    close_waiting = threading.Event()
    closed = threading.Event()

    async def respond(request):
        if request.url.path.endswith("/validate") and not entered.is_set():
            entered.set()
            await release.wait()
        return native_source_server.respond(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://source"
    ) as server:
        admission = await admit_native(server, "contending-writers", "prime-native")
        root = bridge.runtime_paths("contending-writers").root
        for directory in (root / "controls" / "requests", root / "controls" / "results"):
            directory.mkdir(parents=True, exist_ok=True)
        (root / "controls" / "binding.json").write_text(
            json.dumps(
                {"incarnation": "current", "pid": os.getpid(), "heartbeat": time.time() * 1000}
            )
        )
        first = asyncio.create_task(
            PrimeExtensionBinding(root, native_admission=(server, admission)).execute(
                SetEffort("high")
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(
            PrimeExtensionBinding(root, native_admission=(server, admission)).execute(
                SetEffort("low")
            )
        )
        await asyncio.sleep(0.03)
        assert not second.done(), "A competing native control did not wait for current ownership."
        source_id = native_source_server.source("contending-writers")
        stop = native_source_server.store.seal_native_stop(source_id)

        def close():
            close_waiting.set()
            with bridge_dir_preparation_lock(root):
                native_source_server.store.finish_native_stop(stop, NativeStopOutcome.UNKNOWN)
                closed.set()

        actor = asyncio.create_task(asyncio.to_thread(close))
        assert await asyncio.to_thread(close_waiting.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0.03), 1)
        assert not closed.is_set()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 2)
        await asyncio.wait_for(actor, 2)
        assert closed.is_set()
        assert [result.response.status_code for result in results] == [409, 409]
        assert list((root / "controls" / "requests").iterdir()) == []


@pytest.mark.asyncio
async def test_unsupported_current_mode_finishes_unknown_without_reviving_original_work(
    native_source_server,
):
    from omnigent.native.admission import native_owner
    from omnigent.native.source_owner import NativeStopReceipt

    alias = "unsupported-mode"
    source_id = native_source_server.source(alias)
    owner = native_owner(alias, "prime-native")
    original = native_source_server.store.admit_native(source_id, owner)
    spec = AgentSpec(
        spec_version=1,
        name="sdk-current-mode",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def resolver(agent_id, session_id=None):
        return spec

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(native_source_server.respond), base_url="http://source"
    ) as server:
        app = create_runner_app(
            server_client=server,
            spec_resolver=resolver,
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            initialized = await client.post(
                "/v1/sessions", json={"session_id": alias, "agent_id": "agent"}
            )
            assert initialized.status_code == 201, initialized.text
            stop = native_source_server.store.seal_native_stop(source_id)
            local_stop = stop.model_copy(
                update={"admission": stop.admission.model_copy(update={"source_id": alias})}
            )
            response = await client.post(
                f"/v1/sessions/{alias}/events",
                json={"type": "stop_session", "native_stop": local_stop.model_dump()},
            )
            assert response.status_code == 200, response.text
            receipt = NativeStopReceipt.model_validate(response.json())
            assert receipt.stop == local_stop
            assert receipt.result.outcome == NativeStopOutcome.UNKNOWN
            native_source_server.store.finish_native_stop(stop, receipt.result.outcome)
            fresh = native_source_server.store.admit_native(source_id, owner)
            assert fresh.epoch != original.epoch
            delayed = await server.post(
                f"/v1/sessions/{alias}/native-admission",
                json={"owner": owner.model_dump(), "expected_epoch": original.epoch},
            )
            assert delayed.status_code == 409, delayed.text
            with pytest.raises(httpx.HTTPStatusError) as stale_stop:
                await client.post(
                    f"/v1/sessions/{alias}/events",
                    json={"type": "stop_session", "native_stop": local_stop.model_dump()},
                )
            assert stale_stop.value.response.status_code == 409

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime import pending_inputs
from omnigent.server.feature_flags import resolve_feature_flags
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import _create_session, _route_to_runner
from tests.server.integration.test_sessions_policy_evaluate import _patch_default_policies

pytestmark = pytest.mark.asyncio


def _admission_response(request: httpx.Request, status: str) -> httpx.Response:
    claim = json.loads(request.content)["command_admission"]
    return httpx.Response(
        202 if status == "accepted" else 400, json={"delivery": {**claim, "status": status}}
    )


def _deny_unrelated_request(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    if event.get("type") == "request" and data.get("user_content") == "unrelated denied prompt":
        return {"result": "DENY", "reason": "Unrelated request denied"}
    return {"result": "ALLOW"}


def _command(arguments: str = "rollout") -> dict[str, Any]:
    return {
        "type": "slash_command",
        "data": {
            "kind": "skill",
            "name": "review",
            "arguments": arguments,
            "stable_id": "0f" * 16,
        },
    }


@pytest.mark.parametrize("closure", ["pending", "verified", "unknown"])
@pytest.mark.parametrize("runner_origin", [False, True])
@pytest.mark.parametrize("native_routing", [False, True])
async def test_stop_during_first_skill_resolution_revokes_original_ingress(
    client, app, db_uri, monkeypatch, closure, runner_origin, native_routing
):
    from omnigent.errors import OmnigentError
    from omnigent.native.source_owner import (
        NativeAdmission,
        NativeOwner,
        NativeStop,
        NativeStopOutcome,
    )
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.routes._sessions import native_stop
    from omnigent.server.routes.sessions import routes_events
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests.server.integration.test_native_source_stop import source_and_target

    app.state.feature_flags = resolve_feature_flags(
        {"OMNIGENT_FEATURES": "native_skill_routing" if native_routing else ""}
    )
    source_id, target_id = await source_and_target(client)
    store = SqlAlchemyConversationStore(db_uri)
    headers = {}
    if runner_origin:
        store.replace_runner_id(source_id, token_bound_runner_id("skill-runner-secret"))
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = "skill-runner-secret"
    owner = NativeOwner(provider="prime-native", environment="fixture", runtime="private-owner")
    original = store.admit_native(source_id, owner)
    resolving = asyncio.Event()
    release = asyncio.Event()
    written = []
    resolutions = []

    async def relay_ready(*args, **kwargs):
        return None

    monkeypatch.setattr(routes_events, "_ensure_runner_relay_ready", relay_ready)

    async def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"terminal_ready": True, "session_init_protocol_version": 2}
            )
        payload = json.loads(request.content)
        if request.url.path.endswith("/skills/resolve"):
            resolutions.append(payload["arguments"])
            if payload["arguments"] == "rollout":
                resolving.set()
                await asyncio.wait_for(release.wait(), 5)
            return httpx.Response(
                200,
                json={
                    "native_invocation": f"/review {payload['arguments']}",
                    "meta_text": f"Expanded review {payload['arguments']}",
                },
            )
        if payload.get("type") == "stop_session":
            stop = NativeStop.model_validate(payload["native_stop"])
            if closure == "pending":
                return httpx.Response(204)
            return httpx.Response(
                200, json={"stop": stop.model_dump(), "result": {"outcome": closure, "detail": ""}}
            )
        if request.url.path.endswith("/events"):
            ticket = NativeAdmission.model_validate(payload["native_admission"])
            try:
                store.validate_native_admission(ticket)
            except OmnigentError:
                return _admission_response(request, "rejected")
            written.append(payload["content"])
            return _admission_response(request, "accepted")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)

        async def get_runner(*args):
            return runner

        monkeypatch.setattr(native_stop, "_get_runner_client", get_runner)
        submitted = _command()
        submitted["data"]["native_admission"] = original.model_copy(
            update={"epoch": "caller-forged-epoch"}
        ).model_dump()
        task = asyncio.create_task(
            client.post(f"/v1/sessions/{source_id}/events", json=submitted, headers=headers)
        )
        try:
            await asyncio.wait_for(resolving.wait(), 5)
            stopped = await client.post(
                f"/v1/sessions/{source_id}/events", json={"type": "stop_session", "data": {}}
            )
            assert stopped.status_code == 202, stopped.text
            assert stopped.json()["native_stop"]["outcome"] == (
                "verified" if closure == "verified" else "unknown"
            )
            release.set()
            result = await asyncio.wait_for(task, 5)
            assert result.status_code == 202, result.text
            assert result.json()["delivery"]["status"] == "rejected"
            assert store.get_native_source(source_id).admission.epoch == original.epoch
            retry = await client.post(f"/v1/sessions/{source_id}/events", json=_command())
            assert retry.json()["delivery"]["status"] == "rejected"
            assert resolutions == ["rollout"]
            assert written == []
            fork = await client.post(
                f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id}
            )
            assert fork.status_code == (201 if closure == "verified" else 409), fork.text
            if closure == "pending":
                store.finish_native_stop(
                    store.get_native_source(source_id).stop, NativeStopOutcome.UNKNOWN
                )
            fresh = _command("new invocation")
            fresh["data"]["stable_id"] = "ab" * 16
            resumed = await client.post(
                f"/v1/sessions/{source_id}/events", json=fresh, headers=headers
            )
            assert resumed.status_code == 202, resumed.text
            assert resumed.json()["delivery"]["status"] == "accepted"
            assert written == [
                [
                    {
                        "type": "input_text",
                        "text": (
                            "/review new invocation"
                            if native_routing
                            else "Expanded review new invocation"
                        ),
                    }
                ]
            ]
            assert store.get_native_source(source_id).admission.epoch != original.epoch
            blocked = await client.post(
                f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id}
            )
            assert blocked.status_code == 409, blocked.text
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            while pending_inputs.resolve_oldest(source_id) is not None:
                pass


@pytest.mark.parametrize("native_routing", [False, True])
async def test_unadmitted_native_skill_cannot_start_after_legacy_stop(
    client, app, db_uri, monkeypatch, native_routing
):
    from omnigent.native.admission import native_operation
    from omnigent.native.source_owner import NativeAdmission
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests.server.integration.test_native_source_stop import source_and_target

    app.state.feature_flags = resolve_feature_flags(
        {"OMNIGENT_FEATURES": "native_skill_routing" if native_routing else ""}
    )
    source_id, target_id = await source_and_target(client)
    store = SqlAlchemyConversationStore(db_uri)
    assert store.get_native_source(source_id) is None
    resolving = asyncio.Event()
    release = asyncio.Event()
    written = []

    async def respond(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/skills/resolve"):
            if payload["arguments"] == "rollout":
                resolving.set()
                await asyncio.wait_for(release.wait(), 5)
            return httpx.Response(
                200,
                json={
                    "native_invocation": f"/review {payload['arguments']}",
                    "meta_text": f"Expanded review {payload['arguments']}",
                },
            )
        if payload.get("type") == "stop_session":
            return httpx.Response(204)
        if request.url.path.endswith("/events"):
            if "native_admission" in payload:
                store.validate_native_admission(
                    NativeAdmission.model_validate(payload["native_admission"])
                )
            else:
                async with native_operation(client, source_id, "prime-native"):
                    pass
            written.append(payload["content"])
            return _admission_response(request, "accepted")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        task = asyncio.create_task(
            client.post(f"/v1/sessions/{source_id}/events", json=_command())
        )
        try:
            await asyncio.wait_for(resolving.wait(), 5)
            stopped = await client.post(
                f"/v1/sessions/{source_id}/events", json={"type": "stop_session", "data": {}}
            )
            assert stopped.status_code == 202, stopped.text
            assert stopped.json()["native_stop"]["outcome"] == "unknown"
            release.set()
            result = await asyncio.wait_for(task, 5)
            assert result.status_code == 202, result.text
            assert result.json()["delivery"]["status"] == "rejected"
            assert result.json()["queued"] is False
            assert store.get_native_source(source_id) is None
            retry = await client.post(f"/v1/sessions/{source_id}/events", json=_command())
            assert retry.json()["delivery"]["status"] == "rejected"
            assert written == []
            blocked = await client.post(
                f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id}
            )
            assert blocked.status_code == 409, blocked.text
            async with native_operation(client, source_id, "prime-native") as ready:
                store.validate_native_admission(ready)
            fresh = _command("ready owner")
            fresh["data"]["stable_id"] = "cd" * 16
            resumed = await client.post(f"/v1/sessions/{source_id}/events", json=fresh)
            assert resumed.status_code == 202, resumed.text
            assert resumed.json()["delivery"]["status"] == "accepted"
            assert written == [
                [
                    {
                        "type": "input_text",
                        "text": (
                            "/review ready owner"
                            if native_routing
                            else "Expanded review ready owner"
                        ),
                    }
                ]
            ]
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            while pending_inputs.resolve_oldest(source_id) is not None:
                pass


@pytest.mark.parametrize("failure", ["rejected", "proxy_error", "timeout"])
async def test_failed_skill_does_not_authorize_unrelated_native_request(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    _patch_default_policies(monkeypatch, f"{__name__}._deny_unrelated_request")

    def runner_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"native_invocation": "/bundle:review rollout"})
        if request.url.path.endswith("/events"):
            if failure == "timeout":
                raise httpx.ReadTimeout("reply lost", request=request)
            status = 400 if failure == "rejected" else 502
            return httpx.Response(
                status, json={"error": "invalid_input", "detail": "not admitted"}
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        try:
            await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            hook = await client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json={
                    "event": {
                        "type": "PHASE_REQUEST",
                        "data": {"text": "unrelated denied prompt"},
                    }
                },
            )
            assert hook.status_code == 200, hook.text
            assert hook.json()["result"] == "POLICY_ACTION_DENY"
            assert hook.json()["reason"] == "Unrelated request denied"
        finally:
            pending_inputs.resolve_oldest(session_id)


@pytest.mark.parametrize(
    "failure,expected",
    [("rejected", "rejected"), ("proxy_error", "unknown"), ("timeout", "unknown")],
)
async def test_skill_history_reports_runner_admission_failure(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: str,
) -> None:
    from omnigent.server.routes._sessions import helpers

    statuses = []
    monkeypatch.setattr(helpers, "_publish_status", lambda *args: statuses.append(args))
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})

    def runner_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"native_invocation": "/bundle:review rollout"})
        if request.url.path.endswith("/events"):
            if failure == "timeout":
                raise httpx.ReadTimeout("reply lost", request=request)
            if failure == "rejected":
                return _admission_response(request, "rejected")
            return httpx.Response(502, json={"error": "proxy_error"})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        try:
            response = await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
            assert [item["type"] for item in items] == ["slash_command"]
            assert items[0].get("delivery", {}).get("status") == expected
            assert response.json().get("queued") is not True
            assert statuses == []
            assert not pending_inputs.has_pending(session_id)
        finally:
            pending_inputs.resolve_oldest(session_id)


async def test_cancelled_skill_clears_only_its_own_pending_input(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    forwarding = asyncio.Event()

    async def runner_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"native_invocation": "/bundle:review rollout"})
        if request.url.path.endswith("/events"):
            forwarding.set()
            await asyncio.Event().wait()
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        unrelated_id = pending_inputs.record(
            session_id, [{"type": "input_text", "text": "previously queued prompt"}]
        )
        task = asyncio.create_task(
            client.post(f"/v1/sessions/{session_id}/events", json=_command())
        )
        try:
            await asyncio.wait_for(forwarding.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            preserved = pending_inputs.resolve(session_id, unrelated_id)
            assert preserved is not None
            assert preserved.content == [
                {"type": "input_text", "text": "previously queued prompt"}
            ]
            assert not pending_inputs.has_pending(session_id)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pending_inputs.resolve_oldest(session_id)


async def test_same_skill_invocation_id_forwards_once(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    forwarded: list[dict[str, Any]] = []

    def runner_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"native_invocation": "/bundle:review rollout"})
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
            return _admission_response(request, "accepted")
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        try:
            first = await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            second = await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            assert first.status_code == 202, first.text
            assert second.status_code == 202, second.text
            assert first.json()["item_id"] == second.json()["item_id"]
            assert [body["content"] for body in forwarded] == [
                [{"type": "input_text", "text": "/bundle:review rollout"}]
            ]
        finally:
            while pending_inputs.resolve_oldest(session_id) is not None:
                pass


async def test_skill_retry_reads_original_claim_before_resolution(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    resolved = False
    forwarded: list[dict[str, Any]] = []

    def runner_response(request: httpx.Request) -> httpx.Response:
        nonlocal resolved
        if request.url.path.endswith("/skills/resolve"):
            if resolved:
                return httpx.Response(404, json={"available": []})
            resolved = True
            return httpx.Response(200, json={"native_invocation": "/bundle:review rollout"})
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
            return _admission_response(request, "accepted")
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        try:
            first = await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            second = await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            assert first.status_code == second.status_code == 202
            assert second.json()["item_id"] == first.json()["item_id"]
            assert [body["content"] for body in forwarded] == [
                [{"type": "input_text", "text": "/bundle:review rollout"}]
            ]
        finally:
            while pending_inputs.resolve_oldest(session_id) is not None:
                pass


@pytest.mark.parametrize("changed_field", ["arguments", "name", "model_override"])
async def test_skill_invocation_id_rejects_changed_submission(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    forwarded: list[dict[str, Any]] = []

    def runner_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"native_invocation": "/bundle:review rollout"})
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
            return _admission_response(request, "accepted")
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        changed = _command()
        if changed_field == "model_override":
            changed[changed_field] = "fixture-model-changed"
        else:
            changed["data"][changed_field] = "changed"
        try:
            first = await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            second = await client.post(f"/v1/sessions/{session_id}/events", json=changed)
            assert first.status_code == 202, first.text
            assert second.status_code == 409, second.text
            assert [body["content"] for body in forwarded] == [
                [{"type": "input_text", "text": "/bundle:review rollout"}]
            ]
        finally:
            while pending_inputs.resolve_oldest(session_id) is not None:
                pass


@pytest.mark.parametrize("conflicting", [False, True])
async def test_concurrent_skill_claims_forward_one_original_context(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    conflicting: bool,
) -> None:
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    resolving = 0
    both_resolving = asyncio.Event()
    forwarded: list[dict[str, Any]] = []

    async def runner_response(request: httpx.Request) -> httpx.Response:
        nonlocal resolving
        if request.url.path.endswith("/skills/resolve"):
            resolving += 1
            if resolving == 2:
                both_resolving.set()
            await asyncio.wait_for(both_resolving.wait(), timeout=5)
            arguments = json.loads(request.content)["arguments"]
            if arguments == "changed":
                return httpx.Response(200, json={"meta_text": "Expanded changed context"})
            return httpx.Response(200, json={"native_invocation": "/bundle:review rollout"})
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
            return _admission_response(request, "accepted")
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        try:
            responses = await asyncio.gather(
                client.post(f"/v1/sessions/{session_id}/events", json=_command()),
                client.post(
                    f"/v1/sessions/{session_id}/events",
                    json=_command("changed" if conflicting else "rollout"),
                ),
            )
            assert sorted(response.status_code for response in responses) == (
                [202, 409] if conflicting else [202, 202]
            )
            items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
            commands = [item for item in items if item["type"] == "slash_command"]
            assert len(commands) == 1
            winner = commands[0]["arguments"]
            expected = (
                "/bundle:review rollout" if winner == "rollout" else "Expanded changed context"
            )
            assert [body["content"] for body in forwarded] == [
                [{"type": "input_text", "text": expected}]
            ]
            saved_context = [item["content"] for item in items if item["type"] == "message"]
            assert saved_context == (
                [[{"type": "input_text", "text": expected}]] if winner == "changed" else []
            )
        finally:
            while pending_inputs.resolve_oldest(session_id) is not None:
                pass


@pytest.mark.parametrize("reply", ["accepted", "timeout"])
async def test_skill_claim_survives_app_recreation_and_compaction(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
    tmp_path: Path,
    reply: str,
) -> None:
    from omnigent.entities import CompactionData, NewConversationItem
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    forwarded: list[dict[str, Any]] = []

    def runner_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(
                200,
                json={
                    "native_invocation": "/bundle:review rollout",
                    "meta_text": "Changed default paste context",
                },
            )
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
            if reply == "timeout":
                raise httpx.ReadTimeout("reply lost", request=request)
            return _admission_response(request, "accepted")
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]
        try:
            await client.post(f"/v1/sessions/{session_id}/events", json=_command())
            items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
            command_id = items[0]["id"]
            while pending_inputs.resolve_oldest(session_id) is not None:
                pass
            reopened_store = SqlAlchemyConversationStore(db_uri)
            reopened_store.append(
                session_id,
                [
                    NewConversationItem(
                        type="compaction",
                        response_id="turn_compact",
                        data=CompactionData(
                            summary="Earlier command summarized",
                            last_item_id=command_id,
                            token_count=4,
                        ),
                    )
                ],
            )
            artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
            restarted_app = create_app(
                agent_store=SqlAlchemyAgentStore(db_uri),
                file_store=SqlAlchemyFileStore(db_uri),
                conversation_store=reopened_store,
                artifact_store=artifact_store,
                agent_cache=AgentCache(
                    artifact_store=artifact_store, cache_dir=tmp_path / "new-cache"
                ),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted_app), base_url="http://test"
            ) as restarted_client:
                retry = await restarted_client.post(
                    f"/v1/sessions/{session_id}/events", json=_command()
                )
            assert retry.status_code == 202, retry.text
            assert retry.json()["item_id"] == command_id
            assert retry.json()["delivery"]["status"] == (
                "accepted" if reply == "accepted" else "unknown"
            )
            assert [body["content"] for body in forwarded] == [
                [{"type": "input_text", "text": "/bundle:review rollout"}]
            ]
        finally:
            while pending_inputs.resolve_oldest(session_id) is not None:
                pass


@pytest.mark.parametrize(
    "receipt",
    [
        "missing",
        "malformed",
        "wrong_item",
        "wrong_fingerprint",
        "rejected_202",
        "accepted_502",
        "unattributed_400",
    ],
)
async def test_unattributed_skill_reply_stays_unknown(client, app, monkeypatch, receipt):
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "native_skill_routing"})
    forwarded = []

    def runner_response(request):
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"native_invocation": "/review rollout"})
        if request.url.path.endswith("/events"):
            body = json.loads(request.content)
            forwarded.append(body)
            delivery = {**body["command_admission"], "status": "accepted"}
            if receipt == "wrong_item":
                delivery["item_id"] = "aa" * 16
            if receipt == "wrong_fingerprint":
                delivery["fingerprint"] = "bb" * 32
            if receipt == "rejected_202":
                delivery["status"] = "rejected"
            if receipt == "malformed":
                return httpx.Response(202, text="not json")
            return httpx.Response(
                502
                if receipt == "accepted_502"
                else 400
                if receipt == "unattributed_400"
                else 202,
                json={} if receipt in {"missing", "unattributed_400"} else {"delivery": delivery},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        responses = [
            await client.post(f"/v1/sessions/{session['id']}/events", json=_command())
            for _ in range(2)
        ]
        assert [r.json()["delivery"]["status"] for r in responses] == ["unknown", "unknown"]
        assert responses[0].json()["item_id"] == responses[1].json()["item_id"]
        assert len(forwarded) == 1
        assert not pending_inputs.has_pending(session["id"])


async def test_skill_claim_before_send_survives_cancellation(client, app, monkeypatch):
    from omnigent.runtime import get_conversation_store
    from omnigent.runtime.prompt import history_to_input_items
    from omnigent.server.routes._sessions import helpers as routes

    entered = asyncio.Event()
    forwarded = []

    async def stop_before_send(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    def runner_response(request):
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"meta_text": "Unsent skill context"})
        if request.url.path.endswith("/events"):
            forwarded.append(request)
            return _admission_response(request, "accepted")
        return httpx.Response(404)

    monkeypatch.setattr(routes, "_seed_missing_title", stop_before_send)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        task = asyncio.create_task(
            client.post(f"/v1/sessions/{session['id']}/events", json=_command())
        )
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        retry = await client.post(f"/v1/sessions/{session['id']}/events", json=_command())
        assert retry.json()["delivery"]["status"] == "unknown"
        assert forwarded == []
        store = get_conversation_store()
        assert history_to_input_items(store.list_items(session["id"]).data) == []


@pytest.mark.parametrize("occupied_by", ["different_creator", "message"])
async def test_public_skill_identity_conflict_precedes_resolution(
    client, app, monkeypatch, occupied_by
):
    from omnigent.entities import MessageData, NewConversationItem, SlashCommandData
    from omnigent.entities.conversation import SkillCommandDelivery
    from omnigent.runtime import get_conversation_store
    from omnigent.server.routes._sessions.helpers import _skill_command_identity
    from omnigent.server.schemas import SessionEventInput

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    invocation_id, item_id, fingerprint = _skill_command_identity(
        session["id"], SessionEventInput(**_command())
    )
    data = (
        MessageData(role="user", content=[])
        if occupied_by == "message"
        else SlashCommandData(
            agent="agent",
            name="review",
            arguments="rollout",
            delivery=SkillCommandDelivery(invocation_id=invocation_id, fingerprint=fingerprint),
        )
    )
    store = get_conversation_store()
    store.append(
        session["id"],
        [
            NewConversationItem(
                type="message" if occupied_by == "message" else "slash_command",
                stable_id=item_id,
                response_id="turn",
                data=data,
                created_by="another-creator",
            )
        ],
    )
    response = await client.post(f"/v1/sessions/{session['id']}/events", json=_command())
    assert response.status_code == 409
    assert len(store.list_items(session["id"]).data) == 1

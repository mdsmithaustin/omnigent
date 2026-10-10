from __future__ import annotations

import httpx
import pytest

from omnigent.native.source_owner import (
    NativeOwner,
    NativeStop,
    NativeStopOutcome,
    NativeStopReceipt,
    NativeStopResult,
)
from omnigent.server.routes._sessions import native_stop
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("level", [2, 4])
async def test_native_owner_admission_requires_current_runner_proof(auth_client, db_uri, level):
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id

    source_id, _ = await source_and_target(auth_client, user="owner@example.com")
    caller = "editor@example.com" if level == 2 else "owner@example.com"
    headers = {"X-Forwarded-Email": caller}
    await auth_client.get("/v1/sessions", headers=headers)
    if level == 2:
        grant = await auth_client.put(
            f"/v1/sessions/{source_id}/permissions",
            json={"user_id": caller, "level": level},
            headers={"X-Forwarded-Email": "owner@example.com"},
        )
        assert grant.status_code == 200, grant.text
    store = SqlAlchemyConversationStore(db_uri)
    store.replace_runner_id(source_id, token_bound_runner_id("current-runner-secret"))
    owner = NativeOwner(provider="prime-native", environment="fixture", runtime="private-owner")
    url = f"/v1/sessions/{source_id}/native-admission"
    for proof in ({}, {RUNNER_TUNNEL_TOKEN_HEADER: "different-runner-secret"}):
        rejected = await auth_client.post(
            url, json={"owner": owner.model_dump()}, headers={**headers, **proof}
        )
        assert rejected.status_code == 403, rejected.text
        assert store.get_native_source(source_id) is None
    admitted = await auth_client.post(
        url,
        json={"owner": owner.model_dump()},
        headers={**headers, RUNNER_TUNNEL_TOKEN_HEADER: "current-runner-secret"},
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["owner"] == {
        "provider": "prime-native",
        "environment": "fixture",
        "runtime": "private-owner",
        "coordinator": "",
    }
    rejected_validation = await auth_client.post(
        f"{url}/validate", json=admitted.json(), headers=headers
    )
    assert rejected_validation.status_code == 403, rejected_validation.text
    validated = await auth_client.post(
        f"{url}/validate",
        json=admitted.json(),
        headers={**headers, RUNNER_TUNNEL_TOKEN_HEADER: "current-runner-secret"},
    )
    assert validated.status_code == 200, validated.text
    assert validated.json() == {"current": True}


@pytest.mark.parametrize("host_bound", [False, True])
async def test_legacy_owner_stop_attempts_shutdown_without_qualifying_fork(
    client, db_uri, monkeypatch, host_bound
):
    import json

    from tests.server.integration.test_sessions_endpoints import _route_to_runner

    source_id, target_id = await source_and_target(client)
    store = SqlAlchemyConversationStore(db_uri)
    if host_bound:
        store.replace_runner_id(source_id, "legacy-runner")
        store.set_host_id(source_id, "cd" * 16, workspace="/legacy")
    running = True
    host_running = host_bound
    forwarded = []

    def respond(request):
        nonlocal running
        forwarded.append(json.loads(request.content))
        running = False
        return httpx.Response(204)

    async def stop_host(session_id, host_id, runner_id, registry, conversation_store):
        nonlocal host_running
        assert (session_id, host_id, runner_id) == (source_id, "cd" * 16, "legacy-runner")
        host_running = False
        return True

    monkeypatch.setattr(native_stop, "_stop_host_runner_intentionally", stop_host)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as runner:
        _route_to_runner(monkeypatch, runner)
        response = await client.post(
            f"/v1/sessions/{source_id}/events", json={"type": "stop_session", "data": {}}
        )
    assert response.status_code == 202, response.text
    assert response.json()["native_stop"]["outcome"] == "unknown"
    assert running is False
    assert host_running is False
    assert forwarded == [{"type": "stop_session"}]
    blocked = await client.post(f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id})
    assert blocked.status_code == 409, blocked.text


async def source_and_target(client, *, user=None, wrapper=False):
    source_agent = await create_test_agent(
        client,
        name="legacy-custom-prime",
        user=user,
        executor={"type": "omnigent", "config": {"harness": "prime-native"}},
    )
    target = await create_test_agent(
        client,
        name="different-sdk",
        user=user,
        executor={"type": "omnigent", "config": {"harness": "claude-sdk"}},
    )
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": source_agent["id"],
            "initial_items": [],
            "labels": {"omnigent.wrapper": "prime-native-ui"} if wrapper else {},
        },
        headers={"X-Forwarded-Email": user} if user else {},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], target["id"]


@pytest.mark.parametrize("wrapper", [False, True])
async def test_idle_legacy_native_source_requires_explicit_owner_stop_before_different_agent(
    client, wrapper
):
    source_id, target_id = await source_and_target(client, wrapper=wrapper)
    blocked = await client.post(f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id})
    assert blocked.status_code == 409, blocked.text
    assert "explicit Stop" in blocked.text
    same = await client.post(f"/v1/sessions/{source_id}/fork", json={})
    assert same.status_code == 201, same.text
    assert same.json()["id"] != source_id
    gone = await client.post(
        f"/v1/sessions/{source_id}/switch-agent", json={"agent_id": target_id}
    )
    assert gone.status_code == 410, gone.text


@pytest.mark.parametrize(
    "ack", ["empty", "http-error", "wrong-operation", "unsupported", "verified"]
)
async def test_stop_qualification_controls_actual_fork_and_resume(
    client, db_uri, monkeypatch, ack
):
    source_id, target_id = await source_and_target(client)
    owner = NativeOwner(provider="prime-native", environment="fixture", runtime="private-owner")
    admitted = await client.post(
        f"/v1/sessions/{source_id}/native-admission", json={"owner": owner.model_dump()}
    )
    assert admitted.status_code == 200, admitted.text
    ticket = admitted.json()

    def respond(request: httpx.Request) -> httpx.Response:
        import json

        stop = NativeStop.model_validate(json.loads(request.content)["native_stop"])
        if ack == "empty":
            return httpx.Response(204)
        if ack == "http-error":
            return httpx.Response(500, json={"error": "closure_failed"})
        receipt = NativeStopReceipt(
            stop=stop,
            result=NativeStopResult(
                outcome=NativeStopOutcome.UNKNOWN
                if ack == "unsupported"
                else NativeStopOutcome.VERIFIED,
                detail="Provider closure is unqualified." if ack == "unsupported" else "",
            ),
        )
        if ack == "wrong-operation":
            receipt.stop = stop.model_copy(update={"operation_id": "different-operation"})
        return httpx.Response(200, json=receipt.model_dump(mode="json"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as runner:

        async def get_runner(*args):
            return runner

        monkeypatch.setattr(native_stop, "_get_runner_client", get_runner)
        response = await client.post(
            f"/v1/sessions/{source_id}/events", json={"type": "stop_session", "data": {}}
        )
    assert response.status_code == 202, response.text
    expected = "verified" if ack == "verified" else "unknown"
    assert response.json()["native_stop"]["outcome"] == expected
    snapshot = await client.get(f"/v1/sessions/{source_id}")
    assert snapshot.json()["native_stop"]["outcome"] == expected
    fork = await client.post(f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id})
    assert fork.status_code == (201 if ack == "verified" else 409), fork.text
    store = SqlAlchemyConversationStore(db_uri)
    source = store.get_native_source(source_id)
    assert source.phase == (
        "closed" if ack == "verified" else "open" if ack == "unsupported" else "stopping"
    )
    resumed = await client.post(
        f"/v1/sessions/{source_id}/native-admission", json={"owner": owner.model_dump()}
    )
    assert resumed.status_code == (
        409 if ack in {"empty", "http-error", "wrong-operation"} else 200
    ), resumed.text
    if resumed.status_code == 200:
        assert resumed.json()["epoch"] != ticket["epoch"]
        after_resume = await client.post(
            f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id}
        )
        assert after_resume.status_code == 409, after_resume.text


async def test_reader_cannot_stop_but_can_fork_after_owner_stop(auth_client, db_uri):
    source_id, target_id = await source_and_target(auth_client, user="owner@example.com")
    reader = {"X-Forwarded-Email": "reader@example.com"}
    await auth_client.get("/v1/sessions", headers=reader)
    grant = await auth_client.put(
        f"/v1/sessions/{source_id}/permissions",
        json={
            "user_id": "reader@example.com",
            "level": 1,
        },
        headers={"X-Forwarded-Email": "owner@example.com"},
    )
    assert grant.status_code == 200, grant.text
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    target_owner = SqlAlchemyAgentStore(db_uri).get(target_id).session_id
    grant_target = await auth_client.put(
        f"/v1/sessions/{target_owner}/permissions",
        json={"user_id": "reader@example.com", "level": 1},
        headers={"X-Forwarded-Email": "owner@example.com"},
    )
    assert grant_target.status_code == 200, grant_target.text
    stop = await auth_client.post(
        f"/v1/sessions/{source_id}/events",
        json={"type": "stop_session", "data": {}},
        headers=reader,
    )
    assert stop.status_code == 403, stop.text
    blocked = await auth_client.post(
        f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id}, headers=reader
    )
    assert blocked.status_code == 409, blocked.text
    store = SqlAlchemyConversationStore(db_uri)
    store.admit_native(
        source_id, NativeOwner(provider="prime-native", environment="fixture", runtime="private")
    )
    store.finish_native_stop(store.seal_native_stop(source_id), NativeStopOutcome.VERIFIED)
    allowed = await auth_client.post(
        f"/v1/sessions/{source_id}/fork", json={"agent_id": target_id}, headers=reader
    )
    assert allowed.status_code == 201, allowed.text


@pytest.fixture
def header_auth_required(monkeypatch):
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")


async def test_native_admission_status_requires_a_matching_deleted_tombstone(
    header_auth_required, auth_client, db_uri
):
    import uuid

    owner_headers = {"X-Forwarded-Email": "owner@example.com"}
    deleted_id, _ = await source_and_target(auth_client, user="owner@example.com")
    live_id, _ = await source_and_target(auth_client, user="owner@example.com")
    store = SqlAlchemyConversationStore(db_uri)
    owner = NativeOwner(provider="prime-native", environment="fixture", runtime="private-owner")
    deleted_epoch = store.admit_native(deleted_id, owner).epoch
    live_epoch = store.admit_native(live_id, owner).epoch
    deleted = await auth_client.delete(f"/v1/sessions/{deleted_id}", headers=owner_headers)
    assert deleted.status_code == 200, deleted.text

    async def status(session_id, epoch, headers):
        response = await auth_client.post(
            f"/v1/sessions/{session_id}/native-admission/status",
            json={"epoch": epoch},
            headers=headers,
        )
        return response.status_code, response.json()

    stranger = {"X-Forwarded-Email": "stranger@example.com"}
    assert await status(deleted_id, deleted_epoch, owner_headers) == (200, {"deleted": True})
    assert await status(deleted_id, live_epoch, owner_headers) == (200, {"deleted": False})
    assert await status(uuid.uuid4().hex, deleted_epoch, owner_headers) == (
        200,
        {"deleted": False},
    )
    assert await status(live_id, live_epoch, owner_headers) == (200, {"deleted": False})
    assert await status(live_id, live_epoch, stranger) == (200, {"deleted": False})
    assert (await status(deleted_id, deleted_epoch, {}))[0] == 401

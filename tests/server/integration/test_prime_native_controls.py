from __future__ import annotations

import json

import httpx
import pytest

from omnigent.server.routes._sessions.helpers import _RunnerForwardResult
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import _create_session

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["wrapper", "custom", "override-to-prime"])
async def prime_session(client: httpx.AsyncClient, request) -> str:
    agent = await create_test_agent(
        client,
        name="custom-prime-controls",
        executor={
            "type": "omnigent",
            "config": {"harness": "prime-native" if request.param == "custom" else "claude-sdk"},
        },
    )
    payload = {"agent_id": agent["id"], "initial_items": [], "title": "Original"}
    if request.param == "wrapper":
        payload["labels"] = {"omnigent.wrapper": "prime-native-ui"}
    elif request.param == "override-to-prime":
        payload["labels"] = {"omnigent.wrapper": "codex-native-ui"}
        payload["harness_override"] = "prime-native"
    response = await client.post("/v1/sessions", json=payload)
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    if request.param == "custom":
        assert snapshot["harness"] == "prime-native"
        assert "omnigent.wrapper" not in snapshot["labels"]
    elif request.param == "override-to-prime":
        assert snapshot["harness"] == "prime-native"
        assert snapshot["labels"]["omnigent.wrapper"] == "codex-native-ui"
    return sid


@pytest.mark.parametrize(
    "result",
    [
        None,
        _RunnerForwardResult(204, ""),
        _RunnerForwardResult(200, "{}"),
        _RunnerForwardResult(200, '{"status":"applied"}'),
        _RunnerForwardResult(200, '{"status":"applied","model":"verify/wrong"}'),
        _RunnerForwardResult(200, "[]"),
        _RunnerForwardResult(200, "not json"),
        _RunnerForwardResult(202, '{"status":"accepted"}'),
    ],
)
async def test_prime_patch_requires_applied_receipt_without_optimistic_persistence(
    client, monkeypatch, prime_session, result
) -> None:
    sid = prime_session

    async def forward(*args, **kwargs):
        snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
        assert snapshot["reasoning_effort"] is None
        assert snapshot["model_override"] is None
        return result

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    response = await client.patch(
        f"/v1/sessions/{sid}",
        json={
            "model_override": "verify/fixture",
            "reasoning_effort": "high",
            "title": "Updated",
            "labels": {"review": "ready"},
        },
    )
    assert response.status_code == 504, response.text
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_override"] is None
    assert snapshot["reasoning_effort"] is None
    assert snapshot["title"] == "Original"
    assert "review" not in snapshot["labels"]


async def test_prime_patch_model_then_effort_preserves_newer_native_observation(
    client, monkeypatch, prime_session
) -> None:
    sid = prime_session
    events = []
    writes = []
    update = SqlAlchemyConversationStore.update_conversation

    def record_update(store, conversation_id, **kwargs):
        if conversation_id == sid and "title" in kwargs:
            writes.append(kwargs)
        return update(store, conversation_id, **kwargs)

    monkeypatch.setattr(SqlAlchemyConversationStore, "update_conversation", record_update)

    async def forward(session_id, router, event, **kwargs):
        events.append(event["type"])
        if event["type"] == "model_change":
            response = await client.post(
                f"/v1/sessions/{sid}/events",
                json={
                    "type": "external_model_change",
                    "data": {"model": "verify/newer-tui-model"},
                },
            )
            assert response.status_code == 202
            return _RunnerForwardResult(200, '{"status":"applied","model":"verify/fixture"}')
        response = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "external_reasoning_effort_change", "data": {"reasoning_effort": "low"}},
        )
        assert response.status_code == 202
        return _RunnerForwardResult(200, '{"status":"applied","effort":"high"}')

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    response = await client.patch(
        f"/v1/sessions/{sid}",
        json={
            "model_override": "verify/fixture",
            "reasoning_effort": "high",
            "title": "Updated",
            "labels": {"review": "ready"},
        },
    )
    assert response.status_code == 200, response.text
    assert events == ["model_change", "effort_change"]
    assert response.json()["llm_model"] == "verify/newer-tui-model"
    assert response.json()["reasoning_effort"] == "low"
    assert response.json()["title"] == "Updated"
    assert writes == [
        {
            "title": "Updated",
            "cost_control_mode_override": None,
            "_unset_cost_control_mode_override": False,
            "subagent_routing_override": None,
            "_unset_subagent_routing_override": False,
            "share_workspace_files": None,
            "terminal_launch_args": None,
            "archived": None,
        }
    ]
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_override"] is None
    assert snapshot["llm_model"] == "verify/newer-tui-model"
    assert snapshot["reasoning_effort"] == "low"
    assert snapshot["title"] == "Updated"
    assert snapshot["labels"]["review"] == "ready"


async def test_prime_patch_reports_partial_result_and_does_not_rollback_tui(
    client, monkeypatch, prime_session
) -> None:
    sid = prime_session
    events = []

    async def forward(session_id, router, event, **kwargs):
        events.append(event["type"])
        if event["type"] == "model_change":
            await client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "external_model_change", "data": {"model": "verify/fixture"}},
            )
            return _RunnerForwardResult(200, '{"status":"applied","model":"verify/fixture"}')
        await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "external_reasoning_effort_change",
                "data": {"reasoning_effort": "medium"},
            },
        )
        return _RunnerForwardResult(409, '{"status":"rejected","error":"prime_control_rejected"}')

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    response = await client.patch(
        f"/v1/sessions/{sid}",
        json={
            "model_override": "verify/fixture",
            "reasoning_effort": "high",
            "title": "Updated",
            "labels": {"review": "ready"},
        },
    )
    assert response.status_code == 409
    assert events == ["model_change", "effort_change"]
    assert response.json()["detail"]["outcomes"] == {
        "model": {"status": "applied", "detail": "", "model": "verify/fixture"},
        "effort": {"status": "rejected", "detail": "", "error": "prime_control_rejected"},
    }
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["llm_model"] == "verify/fixture"
    assert snapshot["reasoning_effort"] == "medium"
    assert snapshot["title"] == "Original"
    assert "review" not in snapshot["labels"]


@pytest.mark.parametrize(
    "body",
    [
        {"model_override": None},
        {"model_override": "reset"},
        {"reasoning_effort": None},
        {"reasoning_effort": "off"},
        {"model_override": "fixture"},
        {"model_override": "verify/fixture", "silent": True},
        {"reasoning_effort": "high", "silent": True},
    ],
)
async def test_prime_patch_rejects_unsupported_clear_and_bare_model(
    client, prime_session, body
) -> None:
    sid = prime_session
    response = await client.patch(f"/v1/sessions/{sid}", json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"].startswith("prime_control_")


async def test_prime_abort_acceptance_waits_for_actual_interruption_event(
    client, monkeypatch, prime_session
) -> None:
    sid = prime_session
    published = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda session_id, event: published.append(event),
    )

    async def forward(*args, **kwargs):
        return _RunnerForwardResult(202, json.dumps({"status": "interrupt_accepted"}))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    response = await client.post(f"/v1/sessions/{sid}/events", json={"type": "interrupt"})
    assert response.status_code == 202
    assert response.json() == {"queued": True, "status": "interrupt_accepted"}
    assert published == []
    response = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_session_interrupted", "data": {"response_id": "native-turn"}},
    )
    assert response.status_code == 202
    assert [event["type"] for event in published] == ["session.interrupted"]
    assert published[0]["data"]["response_id"] == "native-turn"


@pytest.mark.parametrize(
    "reply, expected",
    [
        (None, 504),
        (_RunnerForwardResult(200, "{}"), 504),
        (_RunnerForwardResult(504, '{"status":"unknown"}'), 504),
        (_RunnerForwardResult(200, '{"status":"applied"}'), 202),
    ],
)
async def test_prime_compact_requires_completion_and_never_replays_unknown(
    client, monkeypatch, prime_session, reply, expected
):
    sid = prime_session
    attempts = []

    async def forward(session_id, router, event, **kwargs):
        attempts.append(event)
        return reply

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    response = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
    assert response.status_code == expected, response.text
    assert attempts == [{"type": "compact"}]


async def test_prime_reset_model_rejects_without_erasing_observation(client, prime_session):
    sid = prime_session
    observed = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_model_change", "data": {"model": "verify/fixture"}},
    )
    assert observed.status_code == 202
    response = await client.post(
        f"/v1/sessions/{sid}/model-override/reset",
        json={"expected_model_override": "verify/fixture"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "prime_control_unsupported"
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["llm_model"] == "verify/fixture"


async def test_prime_catalog_push_and_snapshot(client, prime_session, monkeypatch):
    sid = prime_session
    published = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda session_id, event: published.append(event),
    )
    response = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "external_model_options",
            "data": {
                "models": [{"id": "verify/fixture", "displayName": "Fixture", "isDefault": True}]
            },
        },
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"queued": False}
    assert [event["type"] for event in published] == ["session.model_options"]
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_options"] == [
        {
            "id": "verify/fixture",
            "displayName": "Fixture",
            "isDefault": True,
            "model": None,
            "defaultReasoningEffort": None,
            "supportedReasoningEfforts": [],
        }
    ]


@pytest.mark.parametrize(
    "reply",
    [
        None,
        _RunnerForwardResult(202, "{}"),
        _RunnerForwardResult(200, '{"status":"interrupt_accepted"}'),
    ],
)
async def test_prime_interrupt_rejects_missing_or_invalid_receipt(
    client, prime_session, monkeypatch, reply
):
    sid = prime_session
    published = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda session_id, event: published.append(event),
    )

    async def forward(*args, **kwargs):
        return reply

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    response = await client.post(f"/v1/sessions/{sid}/events", json={"type": "interrupt"})
    assert response.status_code == 504, response.text
    assert response.json()["detail"]["error"] == "prime_control_unknown"
    assert published == []


@pytest.mark.parametrize("receipt_effort", [None, "invalid", 3])
async def test_prime_effort_requires_valid_receipt(
    client, prime_session, monkeypatch, receipt_effort
):
    sid = prime_session

    async def forward(*args, **kwargs):
        return _RunnerForwardResult(
            200, json.dumps({"status": "applied", "effort": receipt_effort})
        )

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    response = await client.patch(f"/v1/sessions/{sid}", json={"reasoning_effort": "high"})
    assert response.status_code == 504, response.text
    assert response.json()["detail"]["outcomes"]["effort"]["status"] == "unknown"
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["reasoning_effort"] is None


@pytest.mark.parametrize("operation", ["model", "effort", "compact", "interrupt"])
@pytest.mark.parametrize(
    "status, body, expected_status, expected_error",
    [
        (
            200,
            '{"status":"applied","model":"verify/fixture","effort":"low","error":"injected"}',
            504,
            "prime_control_unknown",
        ),
        (401, '{"status":"applied","error":{"untrusted":true}}', 401, "prime_control_unknown"),
        (500, "[]", 500, "prime_control_unknown"),
        (409, '{"status":"rejected","error":"injected"}', 409, "prime_control_rejected"),
    ],
)
async def test_prime_receipt_normalization_across_public_routes(
    client, monkeypatch, prime_session, operation, status, body, expected_status, expected_error
):
    async def forward(*args, **kwargs):
        return _RunnerForwardResult(status, body)

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    url = f"/v1/sessions/{prime_session}"
    if operation in {"model", "effort"}:
        payload = (
            {"model_override": "verify/fixture"}
            if operation == "model"
            else {"reasoning_effort": "high"}
        )
        response = await client.patch(url, json=payload)
    else:
        response = await client.post(f"{url}/events", json={"type": operation})
    assert response.status_code == expected_status, response.text
    assert response.json()["detail"]["error"] == expected_error
    if operation in {"model", "effort"}:
        assert response.json()["detail"]["outcomes"][operation]["status"] == (
            "rejected" if expected_status == 409 else "unknown"
        )
    snapshot = (await client.get(url)).json()
    assert snapshot["reasoning_effort"] is None
    assert snapshot["model_override"] is None
    assert snapshot["title"] == "Original"


@pytest.mark.parametrize("override", ["claude-sdk", "pi-native"])
async def test_explicit_override_away_from_prime_preserves_generic_controls(
    client, monkeypatch, override
):
    agent = await create_test_agent(client, executor={"config": {"harness": "prime-native"}})
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "labels": {"omnigent.wrapper": "prime-native-ui"},
            "harness_override": override,
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    events = []

    async def forward(session_id, router, event, **kwargs):
        events.append(event)
        if event["type"] == "compact":
            return _RunnerForwardResult(200, "{}")
        return _RunnerForwardResult(204, "")

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    changed = await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": "verify/generic", "reasoning_effort": "high"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["model_override"] == "verify/generic"
    assert changed.json()["reasoning_effort"] == "high"
    assert events == [
        {"type": "effort_change", "effort": "high"},
        {"type": "model_change", "model": "verify/generic"},
    ]
    cleared = await client.patch(
        f"/v1/sessions/{sid}", json={"model_override": "reset", "reasoning_effort": "off"}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["model_override"] is None
    assert cleared.json()["reasoning_effort"] is None
    assert events[2:] == [
        {"type": "effort_change", "effort": None},
        {"type": "model_change", "model": None},
    ]
    silent = await client.patch(
        f"/v1/sessions/{sid}", json={"model_override": "verify/reset-me", "silent": True}
    )
    assert silent.status_code == 200, silent.text
    assert silent.json()["model_override"] == "verify/reset-me"
    assert len(events) == 4
    reset = await client.post(
        f"/v1/sessions/{sid}/model-override/reset",
        json={"expected_model_override": "verify/reset-me"},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"reset": True}
    compact = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
    assert compact.status_code == 202, compact.text
    assert compact.json() == {"queued": False}
    interrupt = await client.post(f"/v1/sessions/{sid}/events", json={"type": "interrupt"})
    assert interrupt.status_code == 202, interrupt.text
    assert interrupt.json() == {"queued": False}
    catalog = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_model_options", "data": {"models": [{"id": "verify/fixture"}]}},
    )
    assert catalog.status_code == 400, catalog.text
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_override"] is None
    assert snapshot["model_options"] == []


async def test_catalog_classification_uses_facade_resolver(client, monkeypatch):
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._resolve_harness", lambda conv, **kwargs: "prime-native"
    )
    pushed = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_model_options", "data": {"models": [{"id": "verify/facade"}]}},
    )
    assert pushed.status_code == 202, pushed.text
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_options"] == [
        {
            "id": "verify/facade",
            "displayName": "verify/facade",
            "isDefault": False,
            "model": None,
            "defaultReasoningEffort": None,
            "supportedReasoningEfforts": [],
        }
    ]
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._resolve_harness", lambda conv, **kwargs: "claude-sdk"
    )
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_options"] == []


async def test_non_prime_wrapper_conflict_keeps_native_precedence(client, db_uri):
    from omnigent.server.routes import sessions

    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "labels": {"omnigent.wrapper": "pi-native-ui"},
            "harness_override": "claude-sdk",
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(sid)
    assert sessions._native_coding_agent_for_session(conv).harness == "pi-native"
    pushed = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_model_options", "data": {"models": [{"id": "pi/preserved"}]}},
    )
    assert pushed.status_code == 202, pushed.text
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_options"] == [
        {
            "id": "pi/preserved",
            "displayName": "pi/preserved",
            "isDefault": False,
            "model": None,
            "defaultReasoningEffort": None,
            "supportedReasoningEfforts": [],
        }
    ]


@pytest.mark.parametrize("override", ["claude-sdk", "pi-native", "auto"])
async def test_prime_catalog_cache_is_hidden_after_override(client, db_uri, override):
    agent = await create_test_agent(client, executor={"config": {"harness": "prime-native"}})
    session = await _create_session(
        client, agent["id"], labels={"omnigent.wrapper": "prime-native-ui"}
    )
    sid = session["id"]
    pushed = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_model_options", "data": {"models": [{"id": "verify/before"}]}},
    )
    assert pushed.status_code == 202, pushed.text
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert [entry["id"] for entry in snapshot["model_options"]] == ["verify/before"]
    SqlAlchemyConversationStore(db_uri).update_conversation(sid, harness_override=override)
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["model_options"] == []
    refused = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_model_options", "data": {"models": [{"id": "verify/after"}]}},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "invalid_input"

import asyncio
import logging

import pytest

from tests.runner.conftest import _runner_client
from tests.runner.test_suppress_recovery_turn import (
    _build_sdk_app,
    _HistoryServerClient,
    _init_rows,
    _session_init_payload,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,historical,turns",
    [("accepted", False, 0), ("unknown", False, 0), ("rejected", False, 0), ("accepted", True, 0)],
)
async def test_trailing_skill_context_recovery_uses_existing_admission_state(
    monkeypatch, caplog, status, historical, turns
):
    import tests.runner.test_suppress_recovery_turn as recovery

    monkeypatch.setattr(
        recovery,
        "_ITEMS_PAGE",
        {
            "data": [
                {
                    "id": "skill_context",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Saved skill instructions"}],
                    "is_meta": True,
                    "skill_context": {
                        "command_item_id": "original_command",
                        "status": status,
                        "historical": historical,
                    },
                }
            ],
            "has_more": False,
        },
    )
    app, _, harness = _build_sdk_app(_HistoryServerClient())
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json=_session_init_payload(suppress_recovery_turn=False)
        )
        assert response.status_code == 201
        await asyncio.sleep(0.1)
        assert len(harness.posted_bodies) == turns
        assert _init_rows(caplog)[0]["recovery_turn"] == ("history_resume" if turns else "none")

        if status == "accepted" and not historical:
            forwarded = await client.post(
                "/v1/sessions/conv_recover_test/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_recover_test",
                    "content": [{"type": "input_text", "text": "Next deliberate prompt"}],
                },
            )
            assert forwarded.status_code == 202
            await asyncio.sleep(0.1)
            assert len(harness.posted_bodies) == 1
            assert harness.posted_bodies[0]["content"] == [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Saved skill instructions"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Next deliberate prompt"}],
                },
            ]


@pytest.mark.asyncio
async def test_catchup_retains_skill_context_without_starting_another_turn(monkeypatch):
    import tests.runner.test_suppress_recovery_turn as recovery

    monkeypatch.setattr(
        recovery,
        "_ITEMS_PAGE",
        {
            "data": [
                {
                    **recovery._PENDING_USER_MESSAGE,
                    "is_meta": True,
                    "skill_context": {
                        "command_item_id": "original_command",
                        "status": "accepted",
                        "historical": False,
                    },
                }
            ],
            "has_more": False,
        },
    )
    from omnigent.runner.app import _session_histories_ref

    server = recovery._CatchUpServerClient()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json=_session_init_payload(suppress_recovery_turn=False)
        )
        assert response.status_code == 201
        _session_histories_ref[recovery.SESSION_ID] = []
        server.expose_item = True
        await app.state.catch_up_scan()
        assert (
            _session_histories_ref[recovery.SESSION_ID][0]["skill_context"]["command_item_id"]
            == "original_command"
        )
        await asyncio.sleep(0.1)
        assert harness.posted_bodies == []

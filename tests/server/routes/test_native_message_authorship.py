"""Native provenance distinguishes internal deliveries from human-authored agent markup."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import read_transcript_items_since
from omnigent.harnesses.claude_native.forwarder import _external_conversation_item_event
from omnigent.harnesses.codex_native import forwarder as codex_forwarder
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions.orchestration import _persist_external_conversation_item
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_ENVELOPE = '<teammate-message teammate_id="reviewer">Review this</teammate-message>'
# Completion emitted by a named Claude Agent Teams worker.
_TEAM_COMPLETION = """<task-notification>
<task-id>ae6a7749a5dc6041c</task-id><tool-use-id>toolu_agent_team</tool-use-id>
<status>completed</status><summary>Agent "Message probe" finished</summary>
<result>TEAM_DONE</result></task-notification>"""


@pytest.mark.asyncio
async def test_codex_pasted_skill_followup_does_not_report_failed_delivery(db_uri: str) -> None:
    """Accepted user text must settle its delivery, even when it resembles a skill."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Pasted skill")
    store.set_labels(conv.id, {"omnigent.wrapper": "codex-native-ui"})
    conv = store.get_conversation(conv.id)
    assert conv is not None
    state = codex_forwarder._CodexForwarderState()

    async def persist(request: httpx.Request) -> httpx.Response:
        body = SessionEventInput.model_validate(json.loads(request.content))
        await _persist_external_conversation_item(conv.id, conv, body, store)
        return httpx.Response(202, json={"queued": False})

    texts = [
        "<skill>\n<name>uninstalled-quote</name>\n"
        "<path>/nonexistent/SKILL.md</path>\nUSER_QUOTE_731\n</skill>",
        "Ordinary follow-up",
    ]
    try:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(persist)
        ) as client:
            for index, text in enumerate(texts):
                pending_inputs.record(conv.id, [{"type": "input_text", "text": text}])
                for item in [
                    {
                        "type": "userMessage",
                        "id": f"user-{index}",
                        "content": [{"type": "text", "text": text}],
                    },
                    {"type": "agentMessage", "id": f"reply-{index}", "text": "Received"},
                ]:
                    await codex_forwarder._handle_completed_item(
                        client,
                        conv.id,
                        {"threadId": "cafe", "turnId": f"turn-{index}", "item": item},
                        forwarder_state=state,
                    )

        items = store.list_items(conv.id).data
        assert [item.data.code for item in items if item.type == "error"] == []
        assert [item.type for item in items] == ["message"] * 4
        users = [item for item in items if item.data.role == "user"]
        assert [item.data.content for item in users] == [
            [{"type": "input_text", "text": text}] for text in texts
        ]
        assert all(not item.data.is_meta for item in users)
        assert pending_inputs.snapshot_for(conv.id) == []
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.parametrize("author", [None, "alice@example.com"])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize(
    "text,origin,matching,internal",
    [
        (_ENVELOPE, None, True, False),
        ('<agent-message from="reviewer">Review this</agent-message>', None, True, False),
        (_ENVELOPE, None, False, False),
        (_ENVELOPE, {"kind": "peer", "handback": True, "senderTaskId": "agent-1"}, True, True),
        (_TEAM_COMPLETION, {"kind": "task-notification"}, True, True),
    ],
    ids=["web-teammate", "web-handback", "terminal", "internal-handback", "agent-team"],
)
@pytest.mark.asyncio
async def test_native_authorship_survives_delivery_and_retries(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    author: str | None,
    queued: bool,
    text: str,
    origin: dict[str, Any] | None,
    matching: bool,
    internal: bool,
) -> None:
    entry = (
        {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "commandMode": "prompt",
                "prompt": text,
                "origin": origin,
            },
        }
        if queued
        else {"type": "user", "origin": origin, "message": {"role": "user", "content": text}}
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"uuid": "native-record", **entry}) + "\n", encoding="utf-8")
    [parsed] = read_transcript_items_since(transcript, 0, agent_name="Claude")[2]
    body = SessionEventInput.model_validate(_external_conversation_item_event(parsed))
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Review")
    content = [{"type": "input_text", "text": text}]
    pending = [pending_inputs.record(conv.id, [{"type": "input_text", "text": "Still queued"}])]
    match = pending_inputs.record(conv.id, content, created_by=author) if matching else None
    if match and internal:
        pending.append(match)
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    await _persist_external_conversation_item(conv.id, conv, body, store, created_by=author)
    [item] = SqlAlchemyConversationStore(db_uri).list_items(conv.id, type="message").data
    assert item.data.content == content
    assert item.created_by == (None if internal else author)
    assert item.data.user_authored is (not internal)
    assert item.data.is_meta is internal
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == pending
    [receipt] = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    assert receipt.get("cleared_pending_id") == (match if not internal else None)
    assert receipt["data"].get("user_authored", False) is (not internal)
    # Replaying a transcript record must not acknowledge a newer identical submission.
    pending.append(pending_inputs.record(conv.id, content))
    await _persist_external_conversation_item(conv.id, conv, body, store, created_by=author)
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == pending
    assert len(store.list_items(conv.id, type="message").data) == 1

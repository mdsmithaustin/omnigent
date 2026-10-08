from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState, write_bridge_state
from tests.harnesses.codex_native.forwarder._support import _RecordingClient


def _skill(body: str) -> str:
    return f"<skill>\n<name>orchard</name>\n<path>/deleted/SKILL.md</path>\n{body}\n</skill>"


def _response(
    text: str,
    item_id: str | None,
    *,
    role: str = "user",
    turn_id: str | None = None,
    kind: str = "skills.selected_skill_instructions",
) -> dict:
    payload = {
        "type": "message",
        "id": item_id,
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }
    if turn_id is not None:
        payload["internal_chat_message_metadata_passthrough"] = {
            "turn_id": turn_id,
            "content_item_kinds": [kind],
        }
    return {"type": "response_item", "payload": payload}


def _start(turn: str, *, modern: bool = False) -> list[dict]:
    raw = "$orchard Reply READY."
    event = (
        {"type": "item_completed", "turn_id": turn, "item": _item("userMessage", "u", raw)}
        if modern
        else {"type": "user_message", "message": raw}
    )
    return [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        {"type": "turn_context", "payload": {"turn_id": turn}},
        _response(raw, f"raw-{turn}", turn_id=turn if modern else None, kind="user.text"),
        {"type": "event_msg", "payload": event},
    ]


def _item(kind: str, item_id: str, text: str) -> dict:
    if kind == "userMessage":
        return {"type": kind, "id": item_id, "content": [{"type": "text", "text": text}]}
    return {"type": kind, "id": item_id, "text": text}


def _rollout(tmp_path: Path, rows: list[dict]) -> Path:
    home = tmp_path / "native-home"
    path = home / "sessions" / "2026" / "10" / "02" / "rollout-2026-10-02T00-00-00-cafe.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv",
            thread_id="cafe",
            codex_home=str(home),
            socket_path="ws://127.0.0.1:9999",
            active_turn_id="turn-1",
        ),
    )
    return path


async def _complete(client, state, bridge_dir: Path, turn: str, item: dict) -> None:
    await fwd._handle_completed_item(
        client,
        "conv",
        {"threadId": "cafe", "turnId": turn, "item": item},
        forwarder_state=state,
        bridge_dir=bridge_dir,
    )


def _messages(client: _RecordingClient) -> list[dict]:
    return [
        body["data"]
        for _, body in client.posts
        if body["type"] == "external_conversation_item" and body["data"]["item_type"] == "message"
    ]


@pytest.mark.parametrize("modern", [False, True])
async def test_rollout_skills_precede_reply_without_user_message_notifications(
    tmp_path: Path, modern: bool
) -> None:
    original = _skill("Reply with the original orchard instructions.")
    _rollout(
        tmp_path,
        [
            _response(_skill("Before any turn"), "pre-turn"),
            *_start("earlier", modern=modern),
            _response(_skill("Earlier turn"), "earlier-skill"),
            *_start("turn-1", modern=modern),
            _response(_skill("Developer quote"), "developer", role="developer"),
            _response(original, "native-skill", turn_id="turn-1" if modern else None),
            _response("READY", "reply", role="assistant"),
        ],
    )
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    await _complete(client, state, tmp_path, "turn-1", _item("userMessage", "u", "$orchard"))
    await _complete(client, state, tmp_path, "turn-1", _item("agentMessage", "reply", "READY"))

    assert [message["item_data"]["content"][0]["text"] for message in _messages(client)] == [
        "$orchard",
        original,
        "READY",
    ]
    assert _messages(client)[1] == {
        "item_type": "message",
        "item_data": {
            "role": "user",
            "is_meta": True,
            "content": [{"type": "input_text", "text": original}],
        },
        "response_id": "codex_turn-1",
        "source_id": "cafe:turn-1:native-skill",
    }


async def test_rollout_skills_keep_distinct_invocations_and_native_order(tmp_path: Path) -> None:
    rows = []
    for turn, body in [("turn-1", "Original"), ("turn-2", "Original"), ("turn-3", "Changed")]:
        rows.extend(_start(turn, modern=True))
        rows.extend(
            [
                _response(_skill(body), "skill-a", turn_id=turn),
                _response("First reply", "reply-a", role="assistant"),
                _response(_skill("Later instructions"), "skill-b", turn_id=turn),
                _response("Final reply", "reply-b", role="assistant"),
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
            ]
        )
    _rollout(tmp_path, rows)
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    for turn in ["turn-1", "turn-2", "turn-3"]:
        for item in [
            _item("userMessage", "u", "$orchard"),
            _item("agentMessage", "reply-a", "First reply"),
            _item("agentMessage", "reply-b", "Final reply"),
        ]:
            await _complete(client, state, tmp_path, turn, item)
            await _complete(client, state, tmp_path, turn, item)

    messages = _messages(client)
    assert [message["item_data"]["content"][0]["text"] for message in messages] == [
        text
        for body in ["Original", "Original", "Changed"]
        for text in [
            "$orchard",
            _skill(body),
            "First reply",
            _skill("Later instructions"),
            "Final reply",
        ]
    ]
    assert len({message["source_id"] for message in messages}) == 15


async def test_live_reply_boundary_excludes_later_skills_and_legacy_user_quotes(
    tmp_path: Path,
) -> None:
    quoted = _skill("A user pasted this block.")
    original = _skill("The CLI loaded this block.")
    path = _rollout(
        tmp_path,
        [
            *_start("turn-1"),
            _response(quoted, "quote"),
            {"type": "event_msg", "payload": {"type": "user_message", "message": quoted}},
            _response(original, "skill"),
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn-1",
                    "item": {"type": "AgentMessage", "id": "reply"},
                },
            },
            _response(_skill("Instructions after the reply"), "later-skill"),
        ],
    )
    with path.open("ab") as stream:
        stream.write(b'{"type":"response_item","payload":{"text":"\xf0\x9f')
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    await _complete(client, state, tmp_path, "turn-1", _item("userMessage", "u", "$orchard"))
    await _complete(client, state, tmp_path, "turn-1", _item("agentMessage", "reply", "READY"))
    await _complete(client, state, tmp_path, "turn-1", _item("agentMessage", "final", "DONE"))

    assert [message["item_data"]["content"][0]["text"] for message in _messages(client)] == [
        "$orchard",
        original,
        "READY",
        _skill("Instructions after the reply"),
        "DONE",
    ]


async def test_transient_skill_post_retry_keeps_source_and_precedes_reply(tmp_path: Path) -> None:
    _rollout(tmp_path, [*_start("turn-1"), _response(_skill("Original"), "skill")])

    class RetryOnce(_RecordingClient):
        attempted_sources: list[str]

        def __init__(self):
            super().__init__()
            self.attempted_sources = []

        async def post(self, url, *, json, timeout=None):
            source = json["data"]["source_id"]
            self.attempted_sources.append(source)
            if source == "cafe:turn-1:skill" and self.attempted_sources.count(source) == 1:
                return httpx.Response(503, request=httpx.Request("POST", url))
            return await super().post(url, json=json, timeout=timeout)

    client = RetryOnce()
    state = fwd._CodexForwarderState()
    await _complete(client, state, tmp_path, "turn-1", _item("userMessage", "u", "$orchard"))
    await _complete(client, state, tmp_path, "turn-1", _item("agentMessage", "reply", "READY"))

    assert client.attempted_sources == [
        "cafe:turn-1:u",
        "cafe:turn-1:skill",
        "cafe:turn-1:skill",
        "cafe:turn-1:reply",
    ]
    assert [message["item_data"]["content"][0]["text"] for message in _messages(client)] == [
        "$orchard",
        _skill("Original"),
        "READY",
    ]


async def test_reconnect_recovers_skills_from_a_turn_without_assistant_output(
    tmp_path: Path,
) -> None:
    original = _skill("Loaded before the turn was interrupted.")
    _rollout(tmp_path, [*_start("turn-1"), _response(original, "skill")])
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    tracker = fwd._CodexElicitationTaskTracker()
    try:
        await fwd._replay_resume_response(
            client,
            session_id="conv",
            bridge_dir=tmp_path,
            response={
                "result": {
                    "thread": {
                        "id": "cafe",
                        "turns": [
                            {
                                "id": "turn-1",
                                "status": "interrupted",
                                "items": [_item("userMessage", "u", "$orchard")],
                            }
                        ],
                    }
                }
            },
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv"),
            elicitation_tracker=tracker,
            forwarder_state=state,
        )
    finally:
        await tracker.close()

    assert [message["item_data"]["content"][0]["text"] for message in _messages(client)] == [
        "$orchard",
        original,
    ]
    relevant = [
        body
        for _, body in client.posts
        if body["type"] in {"external_conversation_item", "external_session_status"}
    ]
    assert relevant[-2]["data"]["item_data"]["is_meta"] is True
    assert relevant[-1]["data"]["status"] == "idle"


async def test_rollout_skill_rejections_do_not_consume_identity_or_overtake_reply(
    tmp_path: Path,
) -> None:
    original = _skill("Keep me across a rejected delivery.")
    _rollout(tmp_path, [*_start("turn-1"), _response(original, "skill")])

    class RejectOnce(_RecordingClient):
        async def post(self, url, *, json, timeout=None):
            if json["data"].get("item_data", {}).get("is_meta") and not self.rejected:
                self.rejected = True
                return httpx.Response(400, request=httpx.Request("POST", url))
            return await super().post(url, json=json, timeout=timeout)

        rejected = False

    client = RejectOnce()
    state = fwd._CodexForwarderState()
    await _complete(client, state, tmp_path, "turn-1", _item("userMessage", "u", "$orchard"))
    reply = _item("agentMessage", "reply", "READY")
    await _complete(client, state, tmp_path, "turn-1", reply)
    assert [message["item_data"]["content"][0]["text"] for message in _messages(client)] == [
        "$orchard"
    ]
    await _complete(client, state, tmp_path, "turn-1", reply)
    assert [message["item_data"]["content"][0]["text"] for message in _messages(client)] == [
        "$orchard",
        original,
        "READY",
    ]


async def test_cancelled_skill_delivery_retries_same_source_id(tmp_path: Path) -> None:
    _rollout(tmp_path, [*_start("turn-1"), _response(_skill("Original"), "skill")])

    class CancelOnce(_RecordingClient):
        cancelled_source = None

        async def post(self, url, *, json, timeout=None):
            if json["data"].get("item_data", {}).get("is_meta") and self.cancelled_source is None:
                self.cancelled_source = json["data"]["source_id"]
                raise asyncio.CancelledError
            return await super().post(url, json=json, timeout=timeout)

    client = CancelOnce()
    state = fwd._CodexForwarderState()
    await _complete(client, state, tmp_path, "turn-1", _item("userMessage", "u", "$orchard"))
    reply = _item("agentMessage", "reply", "READY")
    with pytest.raises(asyncio.CancelledError):
        await _complete(client, state, tmp_path, "turn-1", reply)
    await _complete(client, state, tmp_path, "turn-1", reply)
    assert [message["source_id"] for message in _messages(client)] == [
        "cafe:turn-1:u",
        "cafe:turn-1:skill",
        "cafe:turn-1:reply",
    ]
    assert client.cancelled_source == "cafe:turn-1:skill"


async def test_turn_completion_recovers_truncated_tail_and_ignores_user_quotes(
    tmp_path: Path,
) -> None:
    original = _skill("Original instructions without a current skill file.")
    rows = [
        *_start("turn-1", modern=True),
        _response(_skill("User quotation"), "quote", turn_id="turn-1", kind="user.text"),
        _response("Quoted " + _skill("Not native"), "embedded", turn_id="turn-1"),
        _response("<skill>Not a native block</skill>", "invalid", turn_id="turn-1"),
        _response(_skill("Wrong turn"), "wrong", turn_id="other"),
    ]
    path = _rollout(tmp_path, rows)
    tail = json.dumps(_response(original, None, turn_id="turn-1"))
    with path.open("a") as stream:
        stream.write(tail[:80])
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    tracker = fwd._CodexElicitationTaskTracker()

    async def finish():
        await fwd._handle_event(
            client,
            session_id="conv",
            bridge_dir=tmp_path,
            event={
                "method": "turn/completed",
                "params": {"threadId": "cafe", "turn": {"id": "turn-1", "status": "completed"}},
            },
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv"),
            elicitation_tracker=tracker,
            expected_thread_id="cafe",
            forwarder_state=state,
        )

    try:
        await _complete(client, state, tmp_path, "turn-1", _item("userMessage", "u", "$orchard"))
        await finish()
        with path.open("a") as stream:
            stream.write(tail[80:] + "\n")
        await finish()
        await finish()
    finally:
        await tracker.close()

    assert [message["item_data"]["content"][0]["text"] for message in _messages(client)] == [
        "$orchard",
        original,
    ]
    source_id = _messages(client)[1]["source_id"]
    assert source_id.startswith("cafe:turn-1:rollout-skill-")
    fresh = fwd._CodexForwarderState()
    await _complete(client, fresh, tmp_path, "turn-1", _item("agentMessage", "reply", "READY"))
    assert _messages(client)[-2]["source_id"] == source_id

"""Prime 0.10 daemon events -> Omnigent /events bodies, over trimmed real recordings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.prime_native.transcript import TranscriptState, reconcile, reduce

FIXTURES = Path(__file__).parent / "fixtures"
CALL_ID = "call-1bb8aea2-0805-41cd-9874-9a12fd90deaa-0|fc_42e9b75b-2cf1-93e7-9246-8a17a6425cf0_0"

USER_TOOL_TEXT = "use your python tool to compute 6*7, then reply with just the number"


def item(response_id: str, item_type: str, item_data: dict[str, Any], source_id: str) -> dict:
    return {
        "type": "external_conversation_item",
        "data": {
            "response_id": response_id,
            "item_type": item_type,
            "item_data": item_data,
            "source_id": source_id,
        },
    }


def user_item(ts: int, text: str) -> dict:
    return item(
        f"pi-user-{ts}",
        "message",
        {"role": "user", "content": [{"type": "input_text", "text": text}]},
        f"prime:user:{ts}",
    )


def assistant_item(response_id: str, text: str, source_id: str) -> dict:
    data = {"role": "assistant", "agent": "Prime Native"}
    data["content"] = [{"type": "output_text", "text": text}]
    return item(response_id, "message", data, source_id)


def call_item(response_id: str) -> dict:
    data = {"agent": "Prime Native", "name": "ipython", "arguments": '{"code":"6*7"}'}
    data["call_id"] = CALL_ID
    return item(response_id, "function_call", data, f"prime:call:{CALL_ID}")


def output_item(response_id: str) -> dict:
    data = {"call_id": CALL_ID, "output": "42"}
    return item(response_id, "function_call_output", data, f"prime:result:{CALL_ID}")


def status(state: str, response_id: str) -> dict:
    return {
        "type": "external_session_status",
        "data": {"status": state, "response_id": response_id},
    }


def text_delta(message_id: str, index: int, delta: str, final: bool = False) -> dict:
    data = {"delta": delta, "message_id": message_id, "index": index, "final": final}
    return {"type": "external_output_text_delta", "data": data}


def usage(input_tokens: int, output_tokens: int, cache_read: int, model: str) -> dict:
    return {
        "type": "external_session_usage",
        "data": {
            "cumulative_input_tokens": input_tokens,
            "cumulative_output_tokens": output_tokens,
            "cumulative_cache_read_input_tokens": cache_read,
            "model": model,
        },
    }


def compaction(state: str) -> dict:
    return {"type": "external_compaction_status", "data": {"status": state}}


def envelopes(name: str) -> list[dict]:
    lines = (FIXTURES / f"{name}.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def snapshot_messages() -> list[dict]:
    return json.loads((FIXTURES / "attach_snapshot.json").read_text())["messages"]


def replay(
    envs: list[dict], state: TranscriptState | None = None
) -> tuple[TranscriptState, list[dict]]:
    state = state or TranscriptState()
    events: list[dict] = []
    for env in envs:
        state, out = reduce(state, env)
        events += out
    return state, events


def source_ids(events: list[dict]) -> list[str]:
    return [e["data"]["source_id"] for e in events if e["type"] == "external_conversation_item"]


TOOL_TURN = [
    status("running", "pi-g1-21"),
    user_item(1792255199218, USER_TOOL_TEXT),
    call_item("pi-turn-1-g1-22"),
    usage(22269, 34, 1152, "grok-4.7"),
    output_item("pi-turn-1-g1-22"),
    text_delta("pi-turn-2-g1-35:msg:1", 0, "42"),
    text_delta("pi-turn-2-g1-35:msg:1", 1, "", final=True),
    usage(44556, 35, 23296, "grok-4.7"),
    assistant_item(
        "pi-turn-2-g1-35", "42", "pi:assistant:response:f3028fe0-52b2-904c-a0f6-7cb3b46cece4"
    ),
    status("idle", "pi-g1-21"),
]

THINKING_TURN_MESSAGE = "pi-turn-1-g1-142:msg:0"
THINKING_REASONING = item(
    "pi-turn-1-g1-142",
    "reasoning",
    {
        "agent": "Prime Native",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "Count from one to eight.\n"}],
    },
    "prime:reasoning:response:msg_011Cg89T5BJRkk6e3qWu8g82:0",
)
THINKING_TURN = [
    status("running", "pi-g1-141"),
    user_item(1792255490522, "count to eight"),
    {
        "type": "external_output_reasoning_delta",
        "data": {"delta": "Count from one to eight.\n", "started": True},
    },
    text_delta(THINKING_TURN_MESSAGE, 0, "1 2 3"),
    text_delta(THINKING_TURN_MESSAGE, 1, " 4 5"),
    text_delta(THINKING_TURN_MESSAGE, 2, " 6 7 8"),
    text_delta(THINKING_TURN_MESSAGE, 3, "", final=True),
    THINKING_REASONING,
    usage(31192, 630, 0, "claude-opus-4-8"),
    assistant_item(
        "pi-turn-1-g1-142",
        "1 2 3 4 5 6 7 8",
        "pi:assistant:response:msg_011Cg89T5BJRkk6e3qWu8g82",
    ),
    status("idle", "pi-g1-141"),
]

ABORTED_MESSAGE = "pi-turn-1-g1-34:msg:0"
ABORTED_TURN = [
    status("running", "pi-g1-33"),
    user_item(1792255887519, "count slowly"),
    text_delta(ABORTED_MESSAGE, 0, "ok"),
    text_delta(ABORTED_MESSAGE, 1, "\n\n1 2 3"),
    text_delta(ABORTED_MESSAGE, 2, " 4 5"),
    text_delta(ABORTED_MESSAGE, 3, "", final=True),
    assistant_item("pi-turn-1-g1-34", "ok\n\n1 2 3 4 5", "pi:assistant:timestamp:1792255892561"),
    {"type": "external_session_interrupted", "data": {"response_id": "pi-g1-33"}},
    status("failed", "pi-g1-33"),
]


def provider_error(response_id: str, timestamp: int) -> dict:
    message = "Prime Native model error: Provider rejected the request (not_found_error, 404): "
    return item(
        response_id,
        "error",
        {
            "source": "execution",
            "code": "RuntimeError",
            "message": message + "model: fixture-anthropic-4",
        },
        f"prime:error:timestamp:{timestamp}",
    )


ERROR_RETRY_TURN = [
    status("running", "pi-g1-1"),
    user_item(1792255841608, "say ok"),
    provider_error("pi-turn-1-g1-2", 1792255841613),
    status("failed", "pi-g1-1"),
    status("running", "pi-g1-10"),
    provider_error("pi-turn-1-g1-11", 1792255844176),
    status("failed", "pi-g1-10"),
]


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        pytest.param("tool_call_turn", TOOL_TURN, id="tool-call-turn"),
        pytest.param("thinking_text_turn", THINKING_TURN, id="thinking-and-streamed-text"),
        pytest.param("aborted_turn", ABORTED_TURN, id="aborted-turn"),
        pytest.param("error_retry_turn", ERROR_RETRY_TURN, id="provider-error-with-retry"),
        pytest.param(
            "compaction_failed", [compaction("in_progress"), compaction("failed")], id="compaction"
        ),
    ],
)
def test_recorded_stream_maps_to_the_events_the_extension_posts(
    fixture: str, expected: list[dict]
) -> None:
    _, events = replay(envelopes(fixture))

    assert events == expected


def test_unstreamed_reasoning_gets_one_preview_delta_before_its_item() -> None:
    envs = [
        e
        for e in envelopes("thinking_text_turn")
        if e["event"].get("assistantMessageEvent", {}).get("type") != "thinking_delta"
    ]

    _, events = replay(envs)

    assert events[2:5] == [
        text_delta(THINKING_TURN_MESSAGE, 0, "1 2 3"),
        text_delta(THINKING_TURN_MESSAGE, 1, " 4 5"),
        text_delta(THINKING_TURN_MESSAGE, 2, " 6 7 8"),
    ]
    assert events[6:8] == [
        {
            "type": "external_output_reasoning_delta",
            "data": {"delta": "Count from one to eight.\n", "started": True},
        },
        THINKING_REASONING,
    ]


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            {
                "type": "compaction_end",
                "reason": "threshold",
                "aborted": False,
                "willRetry": False,
                "result": {"summary": "s"},
            },
            "completed",
            id="finished",
        ),
        pytest.param(
            {"type": "compaction_end", "reason": "manual", "aborted": True, "willRetry": False},
            "failed",
            id="aborted",
        ),
        pytest.param(
            {
                "type": "compaction_end",
                "reason": "manual",
                "aborted": False,
                "willRetry": False,
                "errorMessage": "too short",
            },
            "failed",
            id="error",
        ),
    ],
)
def test_compaction_end_reports_completed_or_failed(event: dict, expected: str) -> None:
    envelope = {"type": "session_event", "event": event, "meta": {"sequence": 1}}

    _, events = reduce(TranscriptState(), envelope)

    assert events == [compaction(expected)]


def test_turn_end_posts_the_tool_call_and_result_a_dropped_message_end_missed() -> None:
    turn_end = next(e for e in envelopes("tool_call_turn") if e["event"]["type"] == "turn_end")

    _, events = replay([turn_end])

    assert events == [
        call_item("pi-turn-0-g1-34"),
        usage(22269, 34, 1152, "grok-4.7"),
        output_item("pi-turn-0-g1-34"),
    ]


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({"type": "daemon_hello"}, id="not-a-session-event"),
        pytest.param(
            {"type": "session_event", "event": {"type": "auto_retry_start", "attempt": 1}},
            id="auto-retry",
        ),
        pytest.param(
            {
                "type": "session_event",
                "event": {
                    "type": "message_end",
                    "message": {"role": "custom", "customType": "agent_message", "content": "hi"},
                },
            },
            id="custom-message",
        ),
    ],
)
def test_events_outside_the_mapping_emit_nothing(envelope: dict) -> None:
    state, events = reduce(TranscriptState(), envelope)

    assert (state, events) == (TranscriptState(), [])


def test_source_id_over_256_chars_is_replaced_by_its_sha256() -> None:
    call_id = "c" * 300
    envelope = {
        "type": "session_event",
        "event": {
            "type": "tool_execution_start",
            "toolCallId": call_id,
            "toolName": "ipython",
            "args": {},
        },
        "meta": {"sequence": 5},
    }

    _, events = reduce(TranscriptState(), envelope)

    assert source_ids(events) == [
        "pi:63ff27051bbba611ac0479d74e81bcfd9c52a840de314558f67287b9015393b7"
    ]


ATTACH_SNAPSHOT_EVENTS = [
    item(
        "pi-recovered-0",
        "reasoning",
        {
            "agent": "Prime Native",
            "summary": [],
            "content": [
                {
                    "type": "reasoning_text",
                    "text": 'Replying with "ok".\n\nChecking the unslop skill before replyi',
                }
            ],
        },
        "prime:reasoning:response:caab9fa7-9207-98bd-8a96-c84853b94c3d:0",
    ),
    assistant_item(
        "pi-recovered-0", "ok", "pi:assistant:response:caab9fa7-9207-98bd-8a96-c84853b94c3d"
    ),
    user_item(1792255199218, USER_TOOL_TEXT),
    call_item("pi-recovered-1792255199218"),
    output_item("pi-recovered-1792255199218"),
    assistant_item(
        "pi-recovered-1792255199218",
        "42",
        "pi:assistant:response:f3028fe0-52b2-904c-a0f6-7cb3b46cece4",
    ),
    user_item(1792255251796, "reply with the word ok"),
    assistant_item(
        "pi-recovered-1792255251796",
        "ok",
        "pi:assistant:response:1154772a-7dcd-92b9-8c8d-ba1739117ca6",
    ),
    user_item(1792255296551, "write the numbers 1 to 300 separated by spaces, no tools"),
    item(
        "pi-recovered-1792255296551",
        "reasoning",
        {
            "agent": "Prime Native",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "I'll just list them directly.\n\n"}],
        },
        "prime:reasoning:timestamp:1792255302562:0",
    ),
    assistant_item(
        "pi-recovered-1792255296551",
        "1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 1",
        "pi:assistant:timestamp:1792255302562",
    ),
    usage(89100, 296, 46720, "grok-4.7"),
]


def test_attach_snapshot_from_scratch_posts_every_message_once_and_skips_custom() -> None:
    _, events = reconcile(TranscriptState(), snapshot_messages(), streaming=False)

    assert events == ATTACH_SNAPSHOT_EVENTS


def test_reconciling_the_same_snapshot_twice_posts_nothing_the_second_time() -> None:
    state, _ = reconcile(TranscriptState(), snapshot_messages(), streaming=False)

    _, again = reconcile(state, snapshot_messages(), streaming=False)

    assert again == []


def test_live_replay_after_reconcile_posts_no_conversation_item_again() -> None:
    state, _ = reconcile(TranscriptState(), snapshot_messages(), streaming=False)

    _, events = replay(envelopes("tool_call_turn"), state)

    assert events == [
        status("running", "pi-g1-21"),
        text_delta("pi-turn-2-g1-35:msg:1", 0, "42"),
        text_delta("pi-turn-2-g1-35:msg:1", 1, "", final=True),
        status("idle", "pi-g1-21"),
    ]


TURN_MESSAGES = slice(2, 6)


@pytest.mark.parametrize(
    ("cut_after_sequence", "missing"),
    [
        pytest.param(
            30,
            [
                output_item("pi-recovered-1792255199218"),
                assistant_item(
                    "pi-recovered-1792255199218",
                    "42",
                    "pi:assistant:response:f3028fe0-52b2-904c-a0f6-7cb3b46cece4",
                ),
                usage(44556, 35, 23296, "grok-4.7"),
            ],
            id="dropped-after-tool-start",
        ),
        pytest.param(
            38,
            [
                text_delta("pi-turn-2-g1-35:msg:1", 1, "", final=True),
                assistant_item(
                    "pi-turn-2-g1-35",
                    "42",
                    "pi:assistant:response:f3028fe0-52b2-904c-a0f6-7cb3b46cece4",
                ),
                usage(44556, 35, 23296, "grok-4.7"),
            ],
            id="dropped-mid-text-stream",
        ),
        pytest.param(41, [], id="nothing-dropped"),
    ],
)
def test_reconcile_after_a_partially_streamed_turn_posts_exactly_the_missing_items(
    cut_after_sequence: int, missing: list[dict]
) -> None:
    seen = [e for e in envelopes("tool_call_turn") if e["meta"]["sequence"] <= cut_after_sequence]
    state, live = replay(seen)

    _, recovered = reconcile(state, snapshot_messages()[TURN_MESSAGES], streaming=True)

    assert recovered == missing
    _, whole_turn = replay(envelopes("tool_call_turn"))
    assert sorted(source_ids(live + recovered)) == sorted(source_ids(whole_turn))


def test_a_new_assistant_message_after_a_lost_message_end_streams_under_a_fresh_id() -> None:
    def start(sequence: int, event: dict) -> dict:
        return {"type": "session_event", "event": event, "meta": {"sequence": sequence}}

    def assistant_start(ts: int) -> dict:
        return {"type": "message_start", "message": {"role": "assistant", "timestamp": ts}}

    def delta(ts: int, text: str) -> dict:
        return {
            "type": "message_update",
            "message": {"role": "assistant", "timestamp": ts},
            "assistantMessageEvent": {"type": "text_delta", "delta": text},
        }

    _, events = replay(
        [
            start(1, {"type": "turn_start"}),
            start(2, assistant_start(100)),
            start(3, delta(100, "lost")),
            start(4, assistant_start(200)),
            start(5, delta(200, "next")),
        ]
    )

    assert events == [
        text_delta("pi-turn-1-0-1:msg:0", 0, "lost"),
        text_delta("pi-turn-1-0-1:msg:1", 0, "next"),
    ]


@pytest.mark.parametrize(
    ("stop_reason", "ending"),
    [
        ("stop", [status("idle", "pi-0-1")]),
        (
            "aborted",
            [
                {"type": "external_session_interrupted", "data": {"response_id": "pi-0-1"}},
                status("failed", "pi-0-1"),
            ],
        ),
    ],
)
def test_reconcile_closes_a_loop_that_ended_while_detached(
    stop_reason: str, ending: list[dict]
) -> None:
    start = {"type": "session_event", "event": {"type": "agent_start"}, "meta": {"sequence": 1}}
    state, _ = reduce(TranscriptState(), start)
    reply = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "timestamp": 5,
        "stopReason": stop_reason,
    }

    closed, events = reconcile(state, [reply], streaming=False)
    _, while_running = reconcile(state, [reply], streaming=True)

    assert [e["type"] for e in events[: -len(ending)]] == ["external_conversation_item"]
    assert events[-len(ending) :] == ending
    assert closed.loop_response_id is None
    assert [e["type"] for e in while_running] == ["external_conversation_item"]

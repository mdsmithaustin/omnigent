"""
Pure mapping from Prime 0.10 daemon ``session_event`` envelopes to Omnigent events.

``reduce`` returns the ``{"type", "data"}`` bodies that the shared Pi extension
posts to ``/v1/sessions/{id}/events`` in Prime mode for the same moment.
``reconcile`` replays an attach snapshot after a (re)connect.

Every conversation item carries a ``source_id`` derived from the message itself,
so the server dedupes a re-post (``_new_external_conversation_item`` keys the
append on it) and live events and snapshots can race without duplicates.
``TranscriptState.posted`` holds the source ids already emitted.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any

Event = dict[str, Any]
Message = dict[str, Any]
Envelope = dict[str, Any]

_MAX_SOURCE_ID = 256
_DEFAULT_LABEL = "Prime Native"


@dataclass(frozen=True)
class TranscriptState:
    """
    Everything ``reduce`` and ``reconcile`` carry between calls.

    :param agent_label: Value of ``item_data.agent``, e.g. ``"Prime Native"``.
    :param loop_response_id: Status ``response_id`` pinned from ``agent_start``
        to ``agent_end``.
    :param turn: Turn ordinal inside the agent loop.
    :param turn_response_id: ``response_id`` of the items in the current turn.
    :param message_ordinal: Assistant messages finished in this agent loop; part
        of the live-preview ``message_id``.
    :param stream_ts: Timestamp of the assistant message being streamed.
    :param text_index: Next ``index`` for the streamed text of that message.
    :param thinking_fresh: Whether the next thinking delta opens a new block.
    :param reasoning_streamed: Whether that message streamed any thinking.
    :param posted: Source ids of conversation items already emitted.
    :param usage_counted: Message keys whose token usage is in the totals.
    :param cumulative_input_tokens: Session input tokens, cache reads included.
    :param cumulative_output_tokens: Session output tokens.
    :param cumulative_cache_read_tokens: Session cache-read input tokens.
    :param usage_model: Model of the latest message that reported usage.
    :param usage_posted: Totals last emitted, to skip an unchanged flush.
    """

    agent_label: str = _DEFAULT_LABEL
    loop_response_id: str | None = None
    turn: int = 0
    turn_response_id: str | None = None
    message_ordinal: int = 0
    stream_ts: int | None = None
    text_index: int = 0
    thinking_fresh: bool = True
    reasoning_streamed: bool = False
    posted: frozenset[str] = frozenset()
    usage_counted: frozenset[str] = frozenset()
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_cache_read_tokens: int = 0
    usage_model: str | None = None
    usage_posted: tuple[int, int, int, str | None] | None = None


@dataclass(frozen=True)
class _Meta:
    generation: str
    sequence: int

    @classmethod
    def of(cls, envelope: Envelope) -> _Meta:
        meta = _obj(envelope.get("meta"))
        cursor = _obj(meta.get("cursor"))
        return cls(str(cursor.get("generation", "0")), _to_int(meta.get("sequence")))


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _to_int(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(number) if math.isfinite(number) and number > 0 else 0


def _json(value: Any) -> str:
    return json.dumps({} if value is None else value, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(text: str) -> str:
    h = 5381
    for ch in text:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return f"{len(text)}-{_base36(h)}"


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
        if not n:
            return out


def _bounded(source_id: str) -> str:
    if len(source_id) <= _MAX_SOURCE_ID:
        return source_id
    return "pi:" + hashlib.sha256(source_id.encode()).hexdigest()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") == "reasoning":
            continue
        text = block.get("text") or block.get("input_text") or block.get("output_text")
        text = text or block.get("content")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _tool_text(content: Any, result: Any) -> str:
    text = _text(content)
    if text:
        return text
    if isinstance(result, str):
        return result
    return "" if result is None else _json(result)


def _message_key(message: Message) -> str:
    response_id = message.get("responseId")
    if isinstance(response_id, str) and response_id:
        return f"response:{response_id}"
    timestamp = message.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return f"timestamp:{int(timestamp)}"
    return f"content:{_fingerprint(_text(message.get('content')))}"


def _timestamp(message: Message) -> int | None:
    timestamp = message.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return int(timestamp)
    return None


def _item(response_id: str, item_type: str, item_data: dict[str, Any], source_id: str) -> Event:
    return {
        "type": "external_conversation_item",
        "data": {
            "response_id": response_id,
            "item_type": item_type,
            "item_data": item_data,
            "source_id": _bounded(source_id),
        },
    }


def _post(
    state: TranscriptState,
    out: list[Event],
    source_id: str,
    response_id: str,
    item_type: str,
    item_data: dict[str, Any],
) -> TranscriptState:
    """Append the item unless ``source_id`` was already emitted."""
    source_id = _bounded(source_id)
    if source_id in state.posted:
        return state
    out.append(_item(response_id, item_type, item_data, source_id))
    return replace(state, posted=state.posted | {source_id})


def _post_call(
    state: TranscriptState,
    out: list[Event],
    response_id: str,
    call_id: Any,
    name: Any,
    arguments: Any,
) -> TranscriptState:
    if not call_id or not name:
        return state
    item_data = {
        "agent": state.agent_label,
        "name": str(name),
        "arguments": _json(arguments),
        "call_id": str(call_id),
    }
    return _post(state, out, f"prime:call:{call_id}", response_id, "function_call", item_data)


def _post_output(
    state: TranscriptState, out: list[Event], response_id: str, call_id: Any, output: str
) -> TranscriptState:
    if not call_id:
        return state
    item_data = {"call_id": str(call_id), "output": output}
    return _post(
        state, out, f"prime:result:{call_id}", response_id, "function_call_output", item_data
    )


def _post_tool_result(
    state: TranscriptState, out: list[Event], response_id: str, message: Message
) -> TranscriptState:
    output = _tool_text(message.get("content"), message.get("details"))
    return _post_output(state, out, response_id, message.get("toolCallId"), output)


def _post_user(state: TranscriptState, out: list[Event], message: Message) -> TranscriptState:
    text = _text(message.get("content"))
    if not text:
        return state
    ts = _timestamp(message)
    identity = f"{ts}" if ts is not None else _fingerprint(text)
    item_data = {"role": "user", "content": [{"type": "input_text", "text": text}]}
    return _post(state, out, f"prime:user:{identity}", f"pi-user-{identity}", "message", item_data)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _mirror(
    state: TranscriptState,
    out: list[Event],
    message: Message,
    response_id: str,
    *,
    in_flight: bool,
) -> TranscriptState:
    """
    Post an assistant message's tool calls and reasoning, in content order.

    :param in_flight: The message is the one being streamed live, so reasoning
        that was not streamed gets a one-shot preview delta before its item.
    """
    key = _message_key(message)
    for index, block in enumerate(_list(message.get("content"))):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "toolCall":
            state = _post_call(
                state, out, response_id, block.get("id"), block.get("name"), block.get("arguments")
            )
        elif block.get("type") == "thinking":
            text = block.get("thinking")
            source_id = _bounded(f"prime:reasoning:{key}:{index}")
            if not isinstance(text, str) or not text.strip() or source_id in state.posted:
                continue
            if in_flight and not state.reasoning_streamed:
                out.append(
                    {
                        "type": "external_output_reasoning_delta",
                        "data": {"delta": text, "started": True},
                    }
                )
            item_data = {
                "agent": state.agent_label,
                "summary": [],
                "content": [{"type": "reasoning_text", "text": text}],
            }
            state = _post(state, out, source_id, response_id, "reasoning", item_data)
    return state


def _count_usage(state: TranscriptState, message: Message) -> TranscriptState:
    """Fold one assistant message's usage into the session totals, once per message."""
    usage = _obj(message.get("usage"))
    if message.get("role") != "assistant" or not usage:
        return state
    tokens = {k: _to_int(usage.get(k)) for k in ("input", "output", "cacheRead", "cacheWrite")}
    if not any(tokens.values()):
        return state
    key = _message_key(message)
    if key in state.usage_counted:
        return state
    model = message.get("model")
    return replace(
        state,
        usage_counted=state.usage_counted | {key},
        # The server prices cache reads out of the input total; cache writes have
        # no field of their own, so they ride along as plain input.
        cumulative_input_tokens=state.cumulative_input_tokens
        + tokens["input"]
        + tokens["cacheRead"]
        + tokens["cacheWrite"],
        cumulative_output_tokens=state.cumulative_output_tokens + tokens["output"],
        cumulative_cache_read_tokens=state.cumulative_cache_read_tokens + tokens["cacheRead"],
        usage_model=model if isinstance(model, str) and model else state.usage_model,
    )


def _flush_usage(state: TranscriptState, out: list[Event]) -> TranscriptState:
    if not (state.cumulative_input_tokens or state.cumulative_output_tokens):
        return state
    posted = (
        state.cumulative_input_tokens,
        state.cumulative_output_tokens,
        state.cumulative_cache_read_tokens,
        state.usage_model,
    )
    if posted == state.usage_posted:
        return state
    data: dict[str, Any] = {
        "cumulative_input_tokens": state.cumulative_input_tokens,
        "cumulative_output_tokens": state.cumulative_output_tokens,
        "cumulative_cache_read_input_tokens": state.cumulative_cache_read_tokens,
    }
    if state.usage_model:
        data["model"] = state.usage_model
    out.append({"type": "external_session_usage", "data": data})
    return replace(state, usage_posted=posted)


def _message_id(state: TranscriptState) -> str:
    return f"{state.turn_response_id}:msg:{state.message_ordinal}"


def _end_stream(state: TranscriptState) -> TranscriptState:
    """Close the streamed assistant message so the next one gets a fresh ``message_id``."""
    return replace(
        state,
        message_ordinal=state.message_ordinal + 1,
        stream_ts=None,
        text_index=0,
        thinking_fresh=True,
        reasoning_streamed=False,
    )


def _complete_assistant(
    state: TranscriptState,
    out: list[Event],
    message: Message,
    response_id: str,
    *,
    live: bool,
) -> TranscriptState:
    """
    Emit the items for a finished assistant message.

    :param live: ``True`` for a ``message_end`` event; ``False`` for a snapshot
        message, which only closes the live stream when it is that message.
    """
    in_flight = live or (state.stream_ts is not None and state.stream_ts == _timestamp(message))
    if in_flight and state.turn_response_id:
        response_id = state.turn_response_id
    if in_flight and state.text_index:
        out.append(
            {
                "type": "external_output_text_delta",
                "data": {
                    "delta": "",
                    "message_id": _message_id(state),
                    "index": state.text_index,
                    "final": True,
                },
            }
        )
    state = _mirror(state, out, message, response_id, in_flight=in_flight)
    if in_flight:
        state = _end_stream(state)
    state = _count_usage(state, message)
    if live:
        state = _flush_usage(state, out)
    key = _message_key(message)
    error = message.get("errorMessage")
    if message.get("stopReason") == "error" and isinstance(error, str) and error:
        item_data = {
            "source": "execution",
            "code": "RuntimeError",
            "message": f"{state.agent_label} model error: {error}",
        }
        return _post(state, out, f"prime:error:{key}", response_id, "error", item_data)
    text = _text(message.get("content"))
    if not text:
        return state
    item_data = {
        "role": "assistant",
        "agent": state.agent_label,
        "content": [{"type": "output_text", "text": text}],
    }
    return _post(state, out, f"pi:assistant:{key}", response_id, "message", item_data)


def _ensure_turn(state: TranscriptState, meta: _Meta) -> tuple[TranscriptState, str]:
    """Return the current turn's ``response_id``, minting one for a turn whose start was missed."""
    if state.turn_response_id is None:
        state = replace(state, turn_response_id=_turn_id(state.turn, meta))
    assert state.turn_response_id is not None
    return state, state.turn_response_id


def _turn_id(turn: int, meta: _Meta) -> str:
    return f"pi-turn-{turn}-{meta.generation}-{meta.sequence}"


def _status(status: str, response_id: str) -> Event:
    return {
        "type": "external_session_status",
        "data": {"status": status, "response_id": response_id},
    }


def _agent_start(
    state: TranscriptState, _event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    response_id = f"pi-{meta.generation}-{meta.sequence}"
    state = replace(
        state,
        loop_response_id=response_id,
        turn=0,
        turn_response_id=None,
        message_ordinal=0,
        stream_ts=None,
        text_index=0,
        thinking_fresh=True,
        reasoning_streamed=False,
    )
    return state, [_status("running", response_id)]


def _turn_start(
    state: TranscriptState, _event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    turn = state.turn + 1
    return replace(state, turn=turn, turn_response_id=_turn_id(turn, meta)), []


def _message_start(
    state: TranscriptState, event: Event, _meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    message = _obj(event.get("message"))
    if message.get("role") != "assistant":
        return state, []
    timestamp = _timestamp(message)
    if state.stream_ts is not None and state.stream_ts != timestamp:
        state = _end_stream(state)
    return replace(state, stream_ts=timestamp), []


def _message_update(
    state: TranscriptState, event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    update = _obj(event.get("assistantMessageEvent"))
    kind = update.get("type")
    if kind == "thinking_start":
        return replace(state, thinking_fresh=True), []
    delta = update.get("delta")
    if kind not in ("text_delta", "thinking_delta") or not isinstance(delta, str) or not delta:
        return state, []
    if state.stream_ts is None:
        state = replace(state, stream_ts=_timestamp(_obj(event.get("message"))))
    if kind == "thinking_delta":
        data = {"delta": delta, "started": state.thinking_fresh}
        state = replace(state, thinking_fresh=False, reasoning_streamed=True)
        return state, [{"type": "external_output_reasoning_delta", "data": data}]
    state, _ = _ensure_turn(state, meta)
    data = {
        "delta": delta,
        "message_id": _message_id(state),
        "index": state.text_index,
        "final": False,
    }
    return replace(state, text_index=state.text_index + 1), [
        {"type": "external_output_text_delta", "data": data}
    ]


def _message_end(
    state: TranscriptState, event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    message = _obj(event.get("message"))
    role = message.get("role")
    out: list[Event] = []
    if role == "user":
        return _post_user(state, out, message), out
    if role == "toolResult":
        state, response_id = _ensure_turn(state, meta)
        return _post_tool_result(state, out, response_id, message), out
    if role == "assistant":
        state, response_id = _ensure_turn(state, meta)
        return _complete_assistant(state, out, message, response_id, live=True), out
    return state, out


def _tool_start(
    state: TranscriptState, event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    out: list[Event] = []
    state, response_id = _ensure_turn(state, meta)
    state = _post_call(
        state,
        out,
        response_id,
        event.get("toolCallId"),
        event.get("toolName"),
        event.get("args"),
    )
    return state, out


def _tool_end(
    state: TranscriptState, event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    out: list[Event] = []
    state, response_id = _ensure_turn(state, meta)
    result = event.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    output = _tool_text(content, result)
    return _post_output(state, out, response_id, event.get("toolCallId"), output), out


def _turn_end(
    state: TranscriptState, event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    out: list[Event] = []
    state, response_id = _ensure_turn(state, meta)
    message = _obj(event.get("message"))
    state = _mirror(state, out, message, response_id, in_flight=False)
    state = _flush_usage(_count_usage(state, message), out)
    for result in _list(event.get("toolResults")):
        state = _post_tool_result(state, out, response_id, _obj(result))
    return state, out


def _agent_end(
    state: TranscriptState, event: Event, meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    out: list[Event] = []
    response_id = state.loop_response_id or f"pi-{meta.generation}-{meta.sequence}"
    messages = [_obj(m) for m in _list(event.get("messages"))]
    last = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
    reason = last.get("stopReason")
    interrupted = reason == "aborted"
    for message in messages:
        state = _count_usage(state, message)
    state = _flush_usage(state, out)
    if interrupted:
        out.append({"type": "external_session_interrupted", "data": {"response_id": response_id}})
    failed = interrupted or reason == "error"
    out.append(_status("failed" if failed else "idle", response_id))
    return replace(state, loop_response_id=None, turn_response_id=None), out


def _compaction_start(
    state: TranscriptState, _event: Event, _meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    return state, [{"type": "external_compaction_status", "data": {"status": "in_progress"}}]


def _compaction_end(
    state: TranscriptState, event: Event, _meta: _Meta
) -> tuple[TranscriptState, list[Event]]:
    failed = bool(event.get("aborted") or event.get("errorMessage"))
    status = "failed" if failed else "completed"
    return state, [{"type": "external_compaction_status", "data": {"status": status}}]


_HANDLERS = {
    "agent_start": _agent_start,
    "turn_start": _turn_start,
    "message_start": _message_start,
    "message_update": _message_update,
    "message_end": _message_end,
    "tool_execution_start": _tool_start,
    "tool_execution_end": _tool_end,
    "turn_end": _turn_end,
    "agent_end": _agent_end,
    "compaction_start": _compaction_start,
    "compaction_end": _compaction_end,
}


def reduce(state: TranscriptState, envelope: Envelope) -> tuple[TranscriptState, list[Event]]:
    """
    Map one daemon ``session_event`` envelope to the Omnigent events to POST.

    :param state: State returned by the previous call.
    :param envelope: ``{"type": "session_event", "event": {...}, "meta": {...}}``.
    :returns: The next state and the ``{"type", "data"}`` event bodies, in post order.
    """
    event = _obj(envelope.get("event"))
    handler = _HANDLERS.get(event.get("type", ""))
    if envelope.get("type") != "session_event" or handler is None:
        return state, []
    return handler(state, event, _Meta.of(envelope))


def reconcile(
    state: TranscriptState, snapshot_messages: list[Message], *, streaming: bool
) -> tuple[TranscriptState, list[Event]]:
    """
    Emit the items for attach-snapshot messages not yet posted.

    ``snapshot_messages`` are complete messages in transcript order (the attach
    snapshot's ``messages``). Each item is keyed like its live counterpart, so
    items already posted are skipped here and a re-post is harmless server-side.
    ``custom`` messages are ignored, as in ``reduce``.

    :param state: Current state, live or fresh.
    :param snapshot_messages: ``snapshot["messages"]`` from ``attach``.
    :param streaming: ``snapshot["state"]["isStreaming"]``. When ``False``, an
        agent loop still open in *state* ended while detached, so it is closed
        as its missed ``agent_end`` would have closed it. When ``True`` and no
        loop is open, one started while detached, so it is opened as ``running``.
    :returns: The next state and the missing event bodies, in post order.
    """
    out: list[Event] = []
    response_id = "pi-recovered-0"
    for message in map(_obj, snapshot_messages):
        role = message.get("role")
        if role == "user":
            response_id = f"pi-recovered-{_timestamp(message)}"
            state = _post_user(state, out, message)
        elif role == "toolResult":
            state = _post_tool_result(state, out, response_id, message)
        elif role == "assistant":
            state = _complete_assistant(state, out, message, response_id, live=False)
    state = _flush_usage(state, out)
    if streaming and state.loop_response_id is None:
        state = replace(state, loop_response_id=response_id)
        return state, [*out, _status("running", response_id)]
    if state.loop_response_id is None or streaming:
        return state, out
    state, ended = _agent_end(state, {"messages": snapshot_messages}, _Meta("0", 0))
    return state, out + ended

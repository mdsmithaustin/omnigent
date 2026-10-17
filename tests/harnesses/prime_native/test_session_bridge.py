"""The Prime 0.10 session bridge against a fake daemon socket and a recording Omnigent API."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.pi_native.bridge import enqueue_user_message
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.harnesses.prime_native.catalog import PrimeModelRef
from omnigent.harnesses.prime_native.controls import (
    Compact,
    ControlOutcome,
    ControlStatus,
    Interrupt,
    PrimeExtensionBinding,
    SetEffort,
    SetModel,
)
from omnigent.harnesses.prime_native.session_bridge import run_bridge

from .test_daemon import HELLO_0_10_0

FIXTURES = Path(__file__).parent / "fixtures"
ROOT_ID = "aaaaaaaaaaaa"
NATIVE_ID = "01a14ae0-f698-7360-afe7-c791001a2fbd"


def _ok(command: dict[str, Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    reply = {"id": command["id"], "type": "response", "command": command["type"], "success": True}
    return reply if data is None else {**reply, "data": data}


@dataclass
class FakePrime:
    """Answers commands like Prime 0.10.0 and records them; one live TUI session."""

    session_dir: Path
    state: dict[str, Any] = field(
        default_factory=lambda: {
            "model": {"provider": "verify", "id": "fixture"},
            "thinkingLevel": "off",
            "isStreaming": False,
        }
    )
    messages: list[dict[str, Any]] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    commands: list[dict[str, Any]] = field(default_factory=list)
    writers: list[asyncio.StreamWriter] = field(default_factory=list)

    def sessions(self) -> list[dict[str, Any]]:
        child = {"id": "bbbbbbbbbbbb", "rlmDepth": 1, "sessionId": "child"}
        root = {"id": ROOT_ID, "rlmDepth": 0, "sessionId": NATIVE_ID}
        child["sessionFile"] = str(self.session_dir / "child.jsonl")
        root["sessionFile"] = str(self.session_dir / f"{NATIVE_ID}.jsonl")
        return [child, root]

    def reply(self, command: dict[str, Any]) -> dict[str, Any]:
        kind = command["type"]
        if kind in self.failures:
            return {**_ok(command), "success": False, "error": self.failures[kind]}
        match kind:
            case "list":
                return _ok(command, {"sessions": self.sessions()})
            case "attach":
                return _ok(command, {"snapshot": {"messages": self.messages, "state": self.state}})
            case "get_state":
                return _ok(command, self.state)
            case "get_available_models":
                return _ok(command, {"models": [{**self.state["model"], "name": "Fixture"}]})
            case "set_model":
                self.state["model"] = {"provider": command["provider"], "id": command["modelId"]}
            case "set_thinking_level":
                self.state["thinkingLevel"] = command["level"]
            case "resume_queue":
                return {**_ok(command), "success": False, "error": "No queued work to resume"}
        return _ok(command)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        await self.send(writer, HELLO_0_10_0)
        while line := await reader.readline():
            command = json.loads(line)["command"]
            self.commands.append(command)
            await self.send(writer, self.reply(command))

    @staticmethod
    async def send(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
        writer.write(json.dumps(frame).encode() + b"\n")
        await writer.drain()

    async def emit(self, *envelopes: dict[str, Any]) -> None:
        for envelope in envelopes:
            await self.send(self.writers[-1], envelope)

    def sent(self, kind: str) -> list[dict[str, Any]]:
        return [command for command in self.commands if command["type"] == kind]


@dataclass
class Bridge:
    paths: PrimeRuntimePaths
    prime: FakePrime
    requests: list[httpx.Request]
    stop: Callable[[], Awaitable[None]]

    def posted(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/events")]

    def posted_types(self) -> list[str]:
        return [event["type"] for event in self.posted()]


async def eventually(check: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while not (result := check()):
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.02)
    return result


@pytest.fixture
async def bridge(tmp_path: Path) -> AsyncIterator[Bridge]:
    paths = PrimeRuntimePaths(tmp_path / "runtime")
    for directory in (paths.session_dir, paths.controls_dir / "requests", paths.root / "inbox"):
        directory.mkdir(parents=True)
    (paths.controls_dir / "results").mkdir()
    (paths.root / "config.json").write_text(
        json.dumps(
            {
                "sessionId": "conv_1",
                "serverUrl": "http://omnigent.test",
                "inboxDir": str(paths.root / "inbox"),
                "authHeaders": {"Authorization": "Bearer launch"},
                "agentLabel": "Prime Native",
            }
        )
    )
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    prime = FakePrime(paths.session_dir)
    socket_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    server = await asyncio.start_unix_server(prime.serve, str(socket_dir / "d.sock"))
    stop = asyncio.Event()
    running = asyncio.create_task(
        run_bridge(paths, socket_dir / "d.sock", stop, transport=httpx.MockTransport(record))
    )

    async def shutdown() -> None:
        stop.set()
        await asyncio.wait_for(running, 5)

    try:
        yield Bridge(paths, prime, requests, shutdown)
    finally:
        await shutdown()
        server.close()
        for writer in prime.writers:
            writer.close()
        shutil.rmtree(socket_dir)


async def ready(bridge: Bridge) -> PrimeExtensionBinding:
    binding = PrimeExtensionBinding(bridge.paths.root)
    assert await binding.wait_until_ready()
    await eventually(lambda: "external_model_options" in bridge.posted_types())
    return binding


async def test_attach_reports_identity_catalog_and_settings_without_reposting_history(
    bridge: Bridge,
) -> None:
    bridge.prime.messages = json.loads((FIXTURES / "attach_snapshot.json").read_text())["messages"]

    await ready(bridge)

    patch = next(r for r in bridge.requests if r.method == "PATCH")
    assert (patch.url.path, json.loads(patch.content)) == (
        "/v1/sessions/conv_1",
        {"external_session_id": NATIVE_ID},
    )
    assert patch.headers["authorization"] == "Bearer launch"
    assert [c["activeSessionId"] for c in bridge.prime.sent("attach")] == [ROOT_ID]
    assert bridge.posted() == [
        {"type": "external_model_change", "data": {"model": "verify/fixture"}},
        {"type": "external_reasoning_effort_change", "data": {"reasoning_effort": "none"}},
        {
            "type": "external_model_options",
            "data": {
                "models": [
                    {"id": "verify/fixture", "model": "verify/fixture", "displayName": "Fixture"}
                ]
            },
        },
    ]
    assert (bridge.paths.root / "input_ready").is_file()


async def test_web_message_is_steered_into_prime_and_consumed(bridge: Bridge) -> None:
    await ready(bridge)
    inbox = bridge.paths.root / "inbox"
    (inbox / "00000000000000000000_00000000_interrupt.json").write_text('{"type": "interrupt"}')

    enqueue_user_message(bridge.paths.root, "hello from the web")

    await eventually(lambda: not list(inbox.iterdir()))
    assert [
        (c["activeSessionId"], c["message"], c["streamingBehavior"])
        for c in bridge.prime.sent("prompt")
    ] == [(ROOT_ID, "hello from the web", "steer")]


async def test_undeliverable_message_is_dropped_with_a_visible_error(bridge: Bridge) -> None:
    await ready(bridge)
    bridge.prime.failures["prompt"] = "Session is shutting down"

    message_id = enqueue_user_message(bridge.paths.root, "lost")

    error = await eventually(
        lambda: next((e for e in bridge.posted() if e["data"].get("item_type") == "error"), None)
    )
    assert len(bridge.prime.sent("prompt")) == 5
    assert error["data"]["item_data"]["code"] == "pi_followup_delivery_dropped"
    assert message_id in error["data"]["item_data"]["message"]
    assert not list((bridge.paths.root / "inbox").iterdir())


async def test_root_session_events_stream_to_omnigent_in_order(bridge: Bridge) -> None:
    await ready(bridge)
    before = len(bridge.posted())
    envelopes = [json.loads(line) for line in (FIXTURES / "aborted_turn.jsonl").open()]
    child_event = {**envelopes[0], "activeSessionId": "bbbbbbbbbbbb"}
    bridge.prime.state["model"] = {"provider": "verify", "id": "picked-in-tui"}

    await bridge.prime.emit(child_event, *envelopes)

    await eventually(lambda: "external_model_change" in bridge.posted_types()[before + 3 :])
    streamed = bridge.posted()[before:]
    assert [event["type"] for event in streamed] == [
        "external_session_status",
        "external_conversation_item",
        "external_output_text_delta",
        "external_output_text_delta",
        "external_output_text_delta",
        "external_output_text_delta",
        "external_conversation_item",
        "external_session_interrupted",
        "external_session_status",
        "external_model_change",
    ]
    assert [streamed[0]["data"]["status"], streamed[8]["data"]["status"]] == ["running", "failed"]
    assert streamed[6]["data"]["item_data"]["content"] == [
        {"type": "output_text", "text": "ok\n\n1 2 3 4 5"}
    ]
    assert streamed[9]["data"] == {"model": "verify/picked-in-tui"}


async def test_model_and_effort_controls_apply_and_report_the_observed_setting(
    bridge: Bridge,
) -> None:
    binding = await ready(bridge)

    model = await binding.execute(SetModel(PrimeModelRef("verify", "other")))
    effort = await binding.execute(SetEffort("none"))

    assert model == ControlOutcome(ControlStatus.APPLIED, model=PrimeModelRef("verify", "other"))
    assert effort == ControlOutcome(ControlStatus.APPLIED, effort="none")
    assert [(c["provider"], c["modelId"]) for c in bridge.prime.sent("set_model")] == [
        ("verify", "other")
    ]
    assert [c["level"] for c in bridge.prime.sent("set_thinking_level")] == ["off"]
    assert bridge.posted()[-2:] == [
        {"type": "external_model_change", "data": {"model": "verify/other"}},
        {"type": "external_reasoning_effort_change", "data": {"reasoning_effort": "none"}},
    ]


async def test_failed_compaction_is_rejected_with_prime_reason(bridge: Bridge) -> None:
    binding = await ready(bridge)
    reason = "Session is too short to compact — try again once it grows"
    bridge.prime.failures["compact"] = reason

    outcome = await binding.execute(Compact("keep decisions"))

    assert outcome == ControlOutcome(ControlStatus.REJECTED, reason)
    assert [c["customInstructions"] for c in bridge.prime.sent("compact")] == ["keep decisions"]


async def test_interrupt_aborts_only_a_running_turn_and_lifts_the_queue_pause(
    bridge: Bridge,
) -> None:
    binding = await ready(bridge)

    idle = await binding.execute(Interrupt())
    bridge.prime.state["isStreaming"] = True
    running = await binding.execute(Interrupt())

    assert idle.status == ControlStatus.UNAVAILABLE
    assert running.status == ControlStatus.INTERRUPT_ACCEPTED
    assert [
        c["type"] for c in bridge.prime.commands if c["type"] in ("abort", "resume_queue")
    ] == [
        "abort",
        "resume_queue",
    ]


async def test_reconnect_posts_only_messages_missed_while_detached(bridge: Bridge) -> None:
    bridge.prime.messages = [
        {"role": "user", "content": [{"type": "text", "text": "before"}], "timestamp": 1}
    ]
    await ready(bridge)
    bridge.prime.messages = [
        *bridge.prime.messages,
        {"role": "user", "content": [{"type": "text", "text": "while away"}], "timestamp": 2},
    ]

    bridge.prime.writers[-1].close()

    await eventually(lambda: len(bridge.prime.sent("attach")) == 2)
    items = await eventually(
        lambda: [e for e in bridge.posted() if e["type"] == "external_conversation_item"]
    )
    assert [item["data"]["item_data"]["content"][0]["text"] for item in items] == ["while away"]


async def test_stopping_releases_the_controls_binding(bridge: Bridge) -> None:
    binding = await ready(bridge)

    await bridge.stop()

    assert not (bridge.paths.controls_dir / "binding.json").exists()
    assert not await binding.wait_until_ready(timeout_s=0.1)

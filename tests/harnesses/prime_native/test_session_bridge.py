"""The Prime 0.10 session bridge against a fake daemon socket and a recording Omnigent API."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.pi_native.bridge import enqueue_user_message
from omnigent.harnesses.prime_native import process
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
from omnigent.native import owner_claim

from .test_daemon import HELLO_0_10_0, PROMPT_SUSPENDED

FIXTURES = Path(__file__).parent / "fixtures"
ROOT_ID = "aaaaaaaaaaaa"
NATIVE_ID = "01a14ae0-f698-7360-afe7-c791001a2fbd"
TICK_S = 0.25
POLICY_NOTICE = "Prime 0.10 does not check its own tools against this agent's policies."


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
    ignored: set[str] = field(default_factory=set)
    hung: set[str] = field(default_factory=set)
    dropped: set[str] = field(default_factory=set)
    suspended: bool = False
    commands: list[dict[str, Any]] = field(default_factory=list)
    writers: list[asyncio.StreamWriter] = field(default_factory=list)
    # None while the TUI shows no chat, as in the /resume picker.
    tui_root: str | None = ROOT_ID
    attached: dict[asyncio.StreamWriter, str] = field(default_factory=dict)
    new_roots: list[dict[str, Any]] = field(default_factory=list)
    lists_message_counts: bool = True
    # Further clients attached to a chat besides the bridge and the TUI.
    also_on: list[str] = field(default_factory=list)

    def sessions(self) -> list[dict[str, Any]]:
        foreign = {"id": "cccccccccccc", "rlmDepth": 0, "sessionFile": "/elsewhere/other.jsonl"}
        child = {"id": "bbbbbbbbbbbb", "rlmDepth": 1, "sessionId": "child"}
        root = {"id": ROOT_ID, "rlmDepth": 0, "sessionId": NATIVE_ID}
        child["sessionFile"] = str(self.session_dir / "child.jsonl")
        root["sessionFile"] = str(self.session_dir / f"{NATIVE_ID}.jsonl")
        root["messageCount"] = len(self.messages)
        roots = [root, *self.new_roots]
        viewers = [*self.attached.values(), self.tui_root, *self.also_on]
        for session in roots:
            session["attachedClients"] = viewers.count(session["id"])
            session.setdefault("messageCount", 0)
            if not self.lists_message_counts:
                del session["messageCount"]
        return [foreign, child, *roots]

    def reply(self, command: dict[str, Any]) -> dict[str, Any]:
        kind = command["type"]
        if kind in self.failures:
            return {**_ok(command), "success": False, "error": self.failures[kind]}
        if kind in self.ignored:
            return _ok(command)
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
            case "prompt" if self.suspended:
                return {**_ok(command), "success": False, "error": PROMPT_SUSPENDED["error"]}
            case "resume_queue":
                self.suspended = False
                return {**_ok(command), "success": False, "error": "No queued work to resume"}
        return _ok(command)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        await self.send(writer, HELLO_0_10_0)
        while line := await reader.readline():
            command = json.loads(line)["command"]
            self.commands.append(command)
            if command["type"] in self.dropped:
                self.attached.pop(writer, None)
                writer.close()
                return
            if command["type"] == "attach":
                self.attached[writer] = command["activeSessionId"]
            if command["type"] not in self.hung:
                await self.send(writer, self.reply(command))
        self.attached.pop(writer, None)

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
    outage: list[int]
    # The Omnigent session's external_session_id; the server keeps it write-once.
    stored: list[str]
    # Statuses, or transport errors, for the next external_session_id updates.
    patch_faults: list[int | Exception]
    # Cleared, the Omnigent API holds each event post open until it is set again.
    gate: asyncio.Event
    blocked: list[str]

    def posted(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/events")]

    def posted_types(self) -> list[str]:
        return [event["type"] for event in self.posted()]

    def statuses(self) -> list[tuple[str, str]]:
        return [
            (event["data"]["status"], event["data"]["response_id"])
            for event in self.posted()
            if event["type"] == "external_session_status"
        ]

    def errors(self) -> list[dict[str, Any]]:
        return [e["data"] for e in self.posted() if e["data"].get("item_type") == "error"]

    def error_codes(self) -> list[str]:
        return [error["item_data"]["code"] for error in self.errors()]


async def eventually(check: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while not (result := check()):
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.02)
    return result


def _runtime(root: Path, server_url: str, **config: object) -> PrimeRuntimePaths:
    paths = PrimeRuntimePaths(root)
    for directory in (
        paths.session_dir,
        paths.controls_dir / "requests",
        paths.controls_dir / "results",
        paths.root / "inbox",
    ):
        directory.mkdir(parents=True)
    settings = {
        "sessionId": "conv_1",
        "serverUrl": server_url,
        "inboxDir": str(paths.root / "inbox"),
        "authHeaders": {"Authorization": "Bearer launch"},
        "agentLabel": "Prime Native",
    }
    (paths.root / "config.json").write_text(json.dumps({**settings, **config}))
    owner_claim.write_owner_claim(paths.root)
    return paths


def _event(sequence: int, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "session_event",
        "activeSessionId": ROOT_ID,
        "event": event,
        "meta": {"sequence": sequence},
    }


def _user(text: str, timestamp: int) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": timestamp}


def _stage_request(controls: Path, request: dict[str, Any]) -> Path:
    staged = controls / "requests" / ".staged"
    staged.write_text(json.dumps(request))
    return staged.rename(controls / "requests" / f"{request['id']}.json")


@pytest.fixture
async def bridge(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Bridge]:
    config = getattr(request, "param", {})
    paths = _runtime(tmp_path / "runtime", "http://omnigent.test", **config)
    requests: list[httpx.Request] = []
    outage: list[int] = []
    patch_faults: list[int | Exception] = []
    gate, blocked = asyncio.Event(), []
    gate.set()
    stored = [config["externalSessionId"]] if "externalSessionId" in config else []

    async def record(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            if not gate.is_set():
                blocked.append(request.url.path)
                await gate.wait()
            if outage:
                return httpx.Response(outage.pop(0))
        requests.append(request)
        if request.method == "PATCH":
            if patch_faults:
                fault = patch_faults.pop(0)
                if isinstance(fault, Exception):
                    raise fault
                return httpx.Response(fault)
            chosen = json.loads(request.content)["external_session_id"]
            if stored and stored[0] != chosen:
                return httpx.Response(400, json={"detail": "external_session_id is already set"})
            stored[:] = [chosen]
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
        yield Bridge(paths, prime, requests, shutdown, outage, stored, patch_faults, gate, blocked)
    finally:
        gate.set()
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

    error = await eventually(bridge.errors)
    assert len(bridge.prime.sent("prompt")) == 5
    assert error[0]["item_data"]["code"] == "pi_followup_delivery_dropped"
    assert message_id in error[0]["item_data"]["message"]
    assert error[0]["item_data"]["message"].endswith('Content preview: "lost"')
    assert not list((bridge.paths.root / "inbox").iterdir())


async def test_root_session_events_stream_to_omnigent_in_order(bridge: Bridge) -> None:
    await ready(bridge)
    before, requests_before = len(bridge.posted()), len(bridge.requests)
    config = json.loads((bridge.paths.root / "config.json").read_text())
    config["authHeaders"] = {"Authorization": "Bearer rotated"}
    (bridge.paths.root / "config.json").write_text(json.dumps(config))
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
    rotated = {r.headers["authorization"] for r in bridge.requests[requests_before:]}
    assert rotated == {"Bearer rotated"}


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


async def test_unconfirmed_model_switch_is_reported_unknown(bridge: Bridge) -> None:
    binding = await ready(bridge)
    bridge.prime.ignored.add("set_model")

    outcome = await binding.execute(SetModel(PrimeModelRef("verify", "other")))

    assert outcome.status == ControlStatus.UNKNOWN
    assert "external_model_change" not in bridge.posted_types()[3:]


async def test_compaction_reports_prime_outcome(bridge: Bridge) -> None:
    binding = await ready(bridge)
    reason = "Session is too short to compact — try again once it grows"

    applied = await binding.execute(Compact())
    bridge.prime.failures["compact"] = reason
    rejected = await binding.execute(Compact("keep decisions"))

    assert applied == ControlOutcome(ControlStatus.APPLIED)
    assert rejected == ControlOutcome(ControlStatus.REJECTED, reason)
    assert [c.get("customInstructions") for c in bridge.prime.sent("compact")] == [
        None,
        "keep decisions",
    ]


@pytest.mark.parametrize(
    "stale",
    [{"incarnation": "previous-bridge"}, {"expiresAt": 0}],
    ids=["other-incarnation", "expired"],
)
async def test_stale_control_is_rejected_without_reaching_prime(
    bridge: Bridge, stale: dict[str, object]
) -> None:
    await ready(bridge)
    controls = bridge.paths.controls_dir
    incarnation = json.loads((controls / "binding.json").read_text())["incarnation"]
    request = {
        "id": "a" * 32,
        "type": "interrupt",
        "incarnation": incarnation,
        "expiresAt": time.time() * 1000 + 10_000,
        **stale,
    }
    _stage_request(controls, request)

    result_file = controls / "results" / f"{'a' * 32}.json"
    await eventually(result_file.exists)

    assert json.loads(result_file.read_text()) == {
        "id": "a" * 32,
        "incarnation": request["incarnation"],
        "status": "rejected",
        "detail": "Expired or stale Prime control",
    }
    assert bridge.prime.sent("get_state") == []


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


async def test_wrapper_runs_the_bridge_only_when_prime_uses_the_private_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _runtime(Path(tempfile.mkdtemp(dir="/tmp")), "http://127.0.0.1:9")
    socket_path = process._default_supervisor_socket(paths)
    socket_path.parent.mkdir(parents=True)
    server = await asyncio.start_unix_server(FakePrime(paths.session_dir).serve, str(socket_path))
    root_logger = logging.getLogger()
    try:
        monkeypatch.delenv(process.DAEMON_SOCKET_ENV_VAR, raising=False)
        process._start_session_bridge(paths)()
        assert not (paths.root / "bridge.log").exists()

        monkeypatch.setenv(process.DAEMON_SOCKET_ENV_VAR, str(socket_path))
        stop = process._start_session_bridge(paths)
        binding = PrimeExtensionBinding(paths.root)
        assert await binding.wait_until_ready()
        await asyncio.to_thread(stop)
        assert not (paths.controls_dir / "binding.json").exists()
        assert (paths.root / "bridge.log").is_file()
        assert [h for h in root_logger.handlers if _logs_to(h, paths.root)] == []
    finally:
        server.close()
        for handler in [h for h in root_logger.handlers if _logs_to(h, paths.root)]:
            root_logger.removeHandler(handler)
            handler.close()
        shutil.rmtree(paths.root)


def _logs_to(handler: logging.Handler, root: Path) -> bool:
    return str(getattr(handler, "baseFilename", "")).startswith(str(root))


async def test_a_hung_prompt_does_not_hold_up_an_interrupt(bridge: Bridge) -> None:
    binding = await ready(bridge)
    bridge.prime.hung.add("prompt")
    enqueue_user_message(bridge.paths.root, "never answered")
    await eventually(lambda: bridge.prime.sent("prompt"))
    bridge.prime.state["isStreaming"] = True

    outcome = await binding.execute(Interrupt(), timeout_s=3.0)

    assert outcome.status == ControlStatus.INTERRUPT_ACCEPTED
    assert len(bridge.prime.sent("abort")) == 1


async def test_an_item_whose_post_failed_is_reposted_from_the_next_snapshot(
    bridge: Bridge,
) -> None:
    await ready(bridge)
    user = _user("during the outage", 7)
    bridge.outage.extend([503, 503, 503])
    await bridge.prime.emit(_event(1, {"type": "message_end", "message": user}))
    await eventually(lambda: not bridge.outage)
    bridge.prime.messages = [user]

    await bridge.prime.emit(_event(2, {"type": "agent_end", "messages": [user]}))

    items = await eventually(
        lambda: [
            event["data"]["item_data"]
            for event in bridge.posted()
            if event["type"] == "external_conversation_item"
        ]
    )
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "during the outage"}]}
    ]
    assert len(bridge.prime.sent("attach")) == 2


async def test_an_outage_past_the_reattach_is_recovered_while_prime_is_idle(
    bridge: Bridge, caplog: pytest.LogCaptureFixture
) -> None:
    await ready(bridge)
    user = _user("final reply lost", 21)
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "answer"}],
        "timestamp": 22,
        "responseId": "r1",
        "stopReason": "stop",
    }
    bridge.prime.messages = [user, assistant]
    await bridge.prime.emit(_event(1, {"type": "agent_start"}))
    await eventually(bridge.statuses)
    lost = ["user", "assistant", "idle status", "user again", "assistant again"]
    bridge.outage.extend([503] * 3 * len(lost))

    await bridge.prime.emit(
        _event(2, {"type": "message_end", "message": user}),
        _event(3, {"type": "message_end", "message": assistant}),
        _event(4, {"type": "agent_end", "messages": [user, assistant]}),
    )

    await eventually(lambda: not bridge.outage)
    items = await eventually(
        lambda: [
            (e["data"]["item_data"]["role"], e["data"]["item_data"]["content"][0]["text"])
            for e in bridge.posted()
            if e["type"] == "external_conversation_item"
        ]
    )
    assert items == [("user", "final reply lost"), ("assistant", "answer")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Prime bridge cannot post to Omnigent; failed items wait for a re-attach"]


async def test_a_lost_idle_status_is_not_posted_over_a_newer_turn(bridge: Bridge) -> None:
    await ready(bridge)
    await bridge.prime.emit(_event(1, {"type": "agent_start"}))
    await eventually(bridge.statuses)
    bridge.prime.messages = [_user("the next turn", 8)]
    bridge.prime.state["isStreaming"] = True
    bridge.outage.extend([503] * 3)

    await bridge.prime.emit(_event(2, {"type": "agent_end", "messages": []}))

    await eventually(lambda: len(bridge.prime.sent("attach")) == 2)
    await eventually(lambda: len(bridge.statuses()) == 2)
    await asyncio.sleep(2 * TICK_S)
    assert bridge.statuses() == [("running", "pi-0-1"), ("running", "pi-recovered-8")]


async def test_an_idle_status_lost_in_an_outage_is_reposted(bridge: Bridge) -> None:
    await ready(bridge)
    await bridge.prime.emit(_event(1, {"type": "agent_start"}))
    await eventually(bridge.statuses)
    bridge.outage.extend([503] * 3)

    await bridge.prime.emit(_event(2, {"type": "agent_end", "messages": []}))

    await eventually(lambda: not bridge.outage)
    statuses = await eventually(lambda: len(bridge.statuses()) == 2 and bridge.statuses())
    assert statuses == [("running", "pi-0-1"), ("idle", "pi-0-1")]


async def test_reattaching_mid_turn_reports_running_until_the_turn_ends(bridge: Bridge) -> None:
    await ready(bridge)
    bridge.prime.messages = [_user("still working", 5)]
    bridge.prime.state["isStreaming"] = True

    bridge.prime.writers[-1].close()

    await eventually(lambda: len(bridge.prime.sent("attach")) == 2)
    await eventually(bridge.statuses)
    bridge.prime.state["isStreaming"] = False
    await bridge.prime.emit(_event(9, {"type": "agent_end", "messages": []}))
    statuses = await eventually(lambda: len(bridge.statuses()) == 2 and bridge.statuses())
    assert statuses == [("running", "pi-recovered-5"), ("idle", "pi-recovered-5")]


async def test_a_turn_that_ended_while_detached_reports_idle_on_reattach(bridge: Bridge) -> None:
    await ready(bridge)
    await bridge.prime.emit(_event(1, {"type": "agent_start"}))
    await eventually(bridge.statuses)

    bridge.prime.writers[-1].close()

    await eventually(lambda: len(bridge.prime.sent("attach")) == 2)
    statuses = await eventually(lambda: len(bridge.statuses()) == 2 and bridge.statuses())
    assert statuses == [("running", "pi-0-1"), ("idle", "pi-0-1")]


async def test_a_message_refused_while_input_is_suspended_lifts_the_pause(
    bridge: Bridge,
) -> None:
    await ready(bridge)
    bridge.prime.suspended = True

    enqueue_user_message(bridge.paths.root, "after esc in the terminal")

    await eventually(lambda: not list((bridge.paths.root / "inbox").iterdir()))
    assert [
        c["type"] for c in bridge.prime.commands if c["type"] in ("prompt", "resume_queue")
    ] == ["prompt", "resume_queue", "prompt"]
    assert bridge.errors() == []


def _replace_binding(paths: PrimeRuntimePaths) -> None:
    record = {"incarnation": "newer-bridge", "pid": 1, "heartbeat": time.time() * 1000}
    (paths.controls_dir / "binding.json").write_text(json.dumps(record))


def _reclaim_runtime(paths: PrimeRuntimePaths) -> None:
    (paths.root / owner_claim.OWNER_PID_FILENAME).write_text("1\npid_ns=elsewhere\n")


@pytest.mark.parametrize(
    "supersede",
    [_replace_binding, _reclaim_runtime],
    ids=["binding-replaced", "runtime-reclaimed"],
)
async def test_a_superseded_bridge_leaves_binding_inbox_and_controls_alone(
    bridge: Bridge, supersede: Callable[[PrimeRuntimePaths], None]
) -> None:
    await ready(bridge)
    controls = bridge.paths.controls_dir
    supersede(bridge.paths)
    await asyncio.sleep(2 * TICK_S)
    binding = (controls / "binding.json").read_text()
    enqueue_user_message(bridge.paths.root, "for the newer bridge")
    request = _stage_request(
        controls,
        {
            "id": "b" * 32,
            "type": "interrupt",
            "incarnation": json.loads(binding)["incarnation"],
            "expiresAt": time.time() * 1000 + 10_000,
        },
    )

    await asyncio.sleep(4 * TICK_S)

    assert (controls / "binding.json").read_text() == binding
    assert request.exists()
    assert len(list((bridge.paths.root / "inbox").iterdir())) == 1
    assert bridge.prime.sent("prompt") == []


@pytest.mark.parametrize("bridge", [{"policyNotice": POLICY_NOTICE}], indirect=True)
async def test_a_launch_policy_notice_is_posted_as_a_session_warning(bridge: Bridge) -> None:
    await ready(bridge)

    errors = await eventually(bridge.errors)

    assert errors == [
        {
            "response_id": "prime-policy-notice",
            "item_type": "error",
            "item_data": {
                "source": "execution",
                "code": "native_policy_not_enforced",
                "message": POLICY_NOTICE,
            },
            "source_id": "prime:policy-notice",
        }
    ]


async def test_a_redelivered_inbox_file_is_not_prompted_twice(bridge: Bridge) -> None:
    await ready(bridge)
    inbox = bridge.paths.root / "inbox"
    message_id = enqueue_user_message(bridge.paths.root, "once only")
    payload = next(inbox.iterdir()).read_text()
    await eventually(lambda: not list(inbox.iterdir()))

    (inbox / "99999999999999999999_99999999_again.json").write_text(payload)

    await eventually(lambda: not list(inbox.iterdir()))
    assert [c["message"] for c in bridge.prime.sent("prompt")] == ["once only"]
    assert json.loads(payload)["id"] == message_id


async def test_a_prompt_cut_off_before_its_reply_is_reported_not_resent(bridge: Bridge) -> None:
    await ready(bridge)
    bridge.prime.dropped.add("prompt")

    message_id = enqueue_user_message(bridge.paths.root, "maybe delivered")

    error = await eventually(bridge.errors)
    await eventually(lambda: len(bridge.prime.sent("attach")) == 2)
    await asyncio.sleep(2 * TICK_S)
    assert len(bridge.prime.sent("prompt")) == 1
    assert message_id in error[0]["item_data"]["message"]
    assert "not resent" in error[0]["item_data"]["message"]
    assert not list((bridge.paths.root / "inbox").iterdir())


async def test_an_outage_logs_one_warning_until_posting_recovers(
    bridge: Bridge, caplog: pytest.LogCaptureFixture
) -> None:
    await ready(bridge)
    await bridge.prime.emit(_event(1, {"type": "agent_start"}))
    await eventually(bridge.statuses)
    bridge.outage.extend([503] * 6)

    await bridge.prime.emit(
        _event(2, {"type": "message_end", "message": _user("first", 1)}),
        _event(3, {"type": "message_end", "message": _user("second", 2)}),
    )
    await eventually(lambda: not bridge.outage)
    await bridge.prime.emit(_event(4, {"type": "message_end", "message": _user("third", 3)}))
    await eventually(lambda: "external_conversation_item" in bridge.posted_types())

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Prime bridge cannot post to Omnigent; failed items wait for a re-attach"]


async def test_an_item_the_server_rejects_does_not_force_a_reattach(bridge: Bridge) -> None:
    await ready(bridge)
    user = _user("rejected", 9)
    bridge.outage.append(422)
    await bridge.prime.emit(_event(1, {"type": "message_end", "message": user}))
    await eventually(lambda: not bridge.outage)

    await bridge.prime.emit(_event(2, {"type": "agent_end", "messages": [user]}))

    await eventually(lambda: "external_session_status" in bridge.posted_types())
    await asyncio.sleep(2 * TICK_S)
    assert len(bridge.prime.sent("attach")) == 1


NEW_CHAT = "01a14b3e-b41c-73fd-856d-c1b3cc28c6b8"


def _chat(bridge: Bridge, session_id: str = NEW_CHAT, runtime_id: str = "dddddddddddd") -> dict:
    root = {
        "id": runtime_id,
        "rlmDepth": 0,
        "sessionId": session_id,
        "sessionFile": str(bridge.paths.session_dir / f"{session_id}.jsonl"),
    }
    bridge.prime.new_roots.append(root)
    return root


def _message_texts(bridge: Bridge) -> list[str]:
    return [
        event["data"]["item_data"]["content"][0]["text"]
        for event in bridge.posted()
        if event["type"] == "external_conversation_item"
        and event["data"]["item_type"] == "message"
    ]


def _patched_ids(bridge: Bridge) -> list[str]:
    return [
        json.loads(r.content)["external_session_id"]
        for r in bridge.requests
        if r.method == "PATCH"
    ]


def _notices(bridge: Bridge) -> list[str]:
    """Each notice code once per source id, as the server dedups a repeated post."""
    posted = {
        error["source_id"]: error["item_data"]["code"]
        for error in bridge.errors()
        if error["item_data"]["code"].startswith("prime_terminal_")
    }
    return list(posted.values())


async def test_the_terminal_switching_chats_detaches_the_bridge_until_it_returns(
    bridge: Bridge, caplog: pytest.LogCaptureFixture
) -> None:
    bridge.prime.messages = [_user("in the pinned chat", 10)]
    binding = await ready(bridge)
    other = _chat(bridge)
    bridge.prime.messages = [*bridge.prime.messages, _user("in the other chat", 11)]
    bridge.prime.tui_root = other["id"]
    enqueue_user_message(bridge.paths.root, "sent while away")

    notice = await eventually(bridge.errors)
    await asyncio.sleep(4 * TICK_S)

    assert [e["item_data"]["code"] for e in notice] == ["prime_terminal_detached"]
    assert notice[0]["item_data"]["level"] == "info"
    assert notice[0]["item_data"]["message"] == (
        "The terminal switched to another Prime chat. This web session mirrors only its "
        "original chat, so the other chat's messages do not appear here, and messages sent "
        "from the web wait until the terminal returns. To continue here, return to the "
        "original chat in the terminal, for example with /resume. A chat started with /new "
        "in this terminal is not shown on the web."
    )
    assert [c["activeSessionId"] for c in bridge.prime.sent("attach")] == [ROOT_ID]
    assert _patched_ids(bridge) == [NATIVE_ID]
    assert _message_texts(bridge) == []
    assert bridge.prime.sent("prompt") == []
    assert len(list((bridge.paths.root / "inbox").iterdir())) == 1
    refused = await binding.execute(Compact())
    assert refused.status == ControlStatus.REJECTED
    assert "different Prime chat" in refused.detail
    assert bridge.prime.sent("compact") == []
    bridge.prime.state["isStreaming"] = True
    stopped = await binding.execute(Interrupt())
    bridge.prime.state["isStreaming"] = False
    assert stopped.status == ControlStatus.INTERRUPT_ACCEPTED
    assert [c["activeSessionId"] for c in bridge.prime.sent("abort")] == [ROOT_ID]

    bridge.prime.tui_root = None
    await asyncio.sleep(4 * TICK_S)

    assert bridge.error_codes() == ["prime_terminal_detached"]
    assert bridge.prime.sent("prompt") == []

    bridge.prime.tui_root = ROOT_ID

    await eventually(lambda: not list((bridge.paths.root / "inbox").iterdir()))
    assert [(c["activeSessionId"], c["message"]) for c in bridge.prime.sent("prompt")] == [
        (ROOT_ID, "sent while away")
    ]
    await eventually(
        lambda: bridge.error_codes() == ["prime_terminal_detached", "prime_terminal_returned"]
    )
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_dropped_connection_while_away_reattaches_to_the_original_chat(
    bridge: Bridge,
) -> None:
    bridge.prime.messages = [_user("before the switch", 11)]
    binding = await ready(bridge)
    other = _chat(bridge)
    bridge.prime.tui_root = other["id"]
    await eventually(bridge.errors)
    bridge.prime.messages = [*bridge.prime.messages, _user("said in the original chat", 12)]
    bridge.prime.state["isStreaming"] = True

    bridge.prime.writers[-1].close()

    assert await eventually(lambda: _message_texts(bridge)) == ["said in the original chat"]
    assert [c["activeSessionId"] for c in bridge.prime.sent("attach")] == [ROOT_ID, ROOT_ID]
    stopped = await binding.execute(Interrupt())
    assert stopped.status == ControlStatus.INTERRUPT_ACCEPTED
    assert bridge.error_codes() == ["prime_terminal_detached"]


@pytest.mark.parametrize("bridge", [{"externalSessionId": NEW_CHAT}], indirect=True)
async def test_a_resumed_launch_mirrors_only_the_saved_chat(bridge: Bridge) -> None:
    saved = _chat(bridge)

    notice = await eventually(bridge.errors)
    await asyncio.sleep(2 * TICK_S)

    assert [e["item_data"]["code"] for e in notice] == ["prime_terminal_detached"]
    assert [c["activeSessionId"] for c in bridge.prime.sent("attach")] == [saved["id"]]
    assert await PrimeExtensionBinding(bridge.paths.root).wait_until_ready()

    bridge.prime.tui_root = saved["id"]

    await eventually(
        lambda: bridge.error_codes() == ["prime_terminal_detached", "prime_terminal_returned"]
    )
    assert [c["activeSessionId"] for c in bridge.prime.sent("attach")] == [saved["id"]]
    assert bridge.stored == [NEW_CHAT]
    assert _patched_ids(bridge) == []


async def test_a_new_chat_before_any_message_takes_the_pin(bridge: Bridge) -> None:
    await ready(bridge)
    other = _chat(bridge)

    bridge.prime.tui_root = other["id"]

    await eventually(
        lambda: (
            [c["activeSessionId"] for c in bridge.prime.sent("attach")] == [ROOT_ID, other["id"]]
        )
    )
    enqueue_user_message(bridge.paths.root, "first message")
    await eventually(lambda: bridge.prime.sent("prompt"))
    assert [c["activeSessionId"] for c in bridge.prime.sent("prompt")] == [other["id"]]
    assert _patched_ids(bridge) == []
    other["messageCount"] = 1
    await eventually(lambda: bridge.stored == [NEW_CHAT])
    assert bridge.errors() == []

    bridge.prime.tui_root = ROOT_ID

    await eventually(lambda: bridge.error_codes() == ["prime_terminal_detached"])
    assert _patched_ids(bridge) == [NEW_CHAT]


@pytest.mark.parametrize(
    "fault",
    [503, httpx.ConnectError("down"), 401, 403, 404],
    ids=["server-error", "unreachable", "stale-token", "forbidden", "not-found"],
)
async def test_the_chat_id_is_recorded_once_the_chat_has_a_message(
    bridge: Bridge, fault: int | Exception
) -> None:
    await ready(bridge)
    await asyncio.sleep(2 * TICK_S)
    assert _patched_ids(bridge) == []
    bridge.patch_faults.append(fault)

    bridge.prime.messages = [_user("first", 1)]

    await eventually(lambda: bridge.stored == [NATIVE_ID])
    bridge.prime.writers[-1].close()
    await eventually(lambda: len(bridge.prime.sent("attach")) == 2)
    await asyncio.sleep(2 * TICK_S)
    assert _patched_ids(bridge) == [NATIVE_ID, NATIVE_ID]


async def test_a_failing_chat_id_record_backs_off_between_attempts(bridge: Bridge) -> None:
    await ready(bridge)
    bridge.patch_faults.extend([503, 503])

    bridge.prime.messages = [_user("first", 1)]

    await eventually(lambda: _patched_ids(bridge))
    await asyncio.sleep(3 * TICK_S)
    assert _patched_ids(bridge) == [NATIVE_ID]
    await eventually(lambda: bridge.stored == [NATIVE_ID])
    assert _patched_ids(bridge) == [NATIVE_ID] * 3


async def test_a_listing_without_message_counts_records_the_first_chat(
    bridge: Bridge,
) -> None:
    bridge.prime.lists_message_counts = False

    await ready(bridge)

    await eventually(lambda: bridge.stored == [NATIVE_ID])


async def test_a_refused_identity_update_is_logged_and_not_retried(
    bridge: Bridge, caplog: pytest.LogCaptureFixture
) -> None:
    await ready(bridge)
    bridge.stored[:] = ["01a14b3e-0000-0000-0000-000000000000"]

    bridge.prime.messages = [_user("first", 1)]

    await eventually(lambda: _patched_ids(bridge))
    await asyncio.sleep(2 * TICK_S)
    assert _patched_ids(bridge) == [NATIVE_ID]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("external_session_id" in w and "400" in w for w in warnings)


DETACHED, RETURNED = "prime_terminal_detached", "prime_terminal_returned"


@pytest.mark.parametrize(
    ("returns_first", "expected"),
    [(False, [DETACHED]), (True, [])],
    ids=["terminal-stays-away", "terminal-returns-first"],
)
async def test_a_departure_notice_lost_to_an_outage_is_retried_not_counted_as_shown(
    bridge: Bridge, returns_first: bool, expected: list[str]
) -> None:
    bridge.prime.messages = [_user("in the pinned chat", 10)]
    await ready(bridge)
    bridge.outage.extend([503] * 1000)
    bridge.prime.tui_root = _chat(bridge)["id"]
    await eventually(lambda: len(bridge.outage) <= 997)
    if returns_first:
        bridge.prime.tui_root = ROOT_ID
        await asyncio.sleep(4 * TICK_S)

    bridge.outage.clear()

    await asyncio.sleep(4 * TICK_S)
    assert _notices(bridge) == expected


async def test_a_return_seen_while_the_departure_notice_is_in_flight_still_posts_the_return(
    bridge: Bridge,
) -> None:
    bridge.prime.messages = [_user("in the pinned chat", 10)]
    await ready(bridge)
    bridge.gate.clear()
    bridge.prime.tui_root = _chat(bridge)["id"]
    await eventually(lambda: bridge.blocked)
    bridge.prime.tui_root = ROOT_ID
    bridge.prime.writers[-1].close()
    await eventually(lambda: len(bridge.prime.sent("attach")) == 2)

    bridge.gate.set()

    await eventually(lambda: _notices(bridge) == [DETACHED, RETURNED])


async def test_each_departure_posts_its_own_notice_pair(bridge: Bridge) -> None:
    bridge.prime.messages = [_user("in the pinned chat", 10)]
    await ready(bridge)
    other = _chat(bridge)

    steps = [
        (other["id"], [DETACHED]),
        (ROOT_ID, [DETACHED, RETURNED]),
        (other["id"], [DETACHED, RETURNED, DETACHED]),
        (ROOT_ID, [DETACHED, RETURNED, DETACHED, RETURNED]),
    ]
    for terminal, expected in steps:
        bridge.prime.tui_root = terminal
        await eventually(lambda expected=expected: _notices(bridge) == expected)

    assert len({error["source_id"] for error in bridge.errors()}) == 4


async def test_the_resume_picker_opened_from_the_pinned_chat_is_not_a_departure(
    bridge: Bridge,
) -> None:
    bridge.prime.messages = [_user("in the pinned chat", 10)]
    await ready(bridge)

    bridge.prime.tui_root = None
    await asyncio.sleep(4 * TICK_S)
    enqueue_user_message(bridge.paths.root, "sent while the picker is open")

    await eventually(lambda: bridge.prime.sent("prompt"))
    assert [c["activeSessionId"] for c in bridge.prime.sent("prompt")] == [ROOT_ID]
    assert bridge.errors() == []


async def test_a_client_on_another_chat_is_the_terminal_even_when_the_pinned_chat_has_more(
    bridge: Bridge,
) -> None:
    bridge.prime.messages = [_user("in the pinned chat", 10)]
    await ready(bridge)

    bridge.prime.also_on.append(ROOT_ID)
    bridge.prime.tui_root = _chat(bridge)["id"]

    await eventually(lambda: _notices(bridge) == [DETACHED])


@pytest.mark.parametrize("bridge", [{"externalSessionId": NATIVE_ID}], indirect=True)
async def test_a_resumed_launch_with_the_terminal_on_the_saved_chat_just_mirrors_it(
    bridge: Bridge,
) -> None:
    bridge.prime.messages = [_user("from before", 10)]
    await ready(bridge)

    enqueue_user_message(bridge.paths.root, "sent from the web")

    await eventually(lambda: bridge.prime.sent("prompt"))
    await asyncio.sleep(2 * TICK_S)
    assert [c["activeSessionId"] for c in bridge.prime.sent("prompt")] == [ROOT_ID]
    assert bridge.errors() == []
    assert _patched_ids(bridge) == []


async def test_a_conflicting_identity_update_is_logged_and_not_retried(
    bridge: Bridge, caplog: pytest.LogCaptureFixture
) -> None:
    await ready(bridge)
    bridge.patch_faults.append(409)

    bridge.prime.messages = [_user("first", 1)]

    await eventually(lambda: _patched_ids(bridge))
    await asyncio.sleep(2 * TICK_S)
    assert _patched_ids(bridge) == [NATIVE_ID]
    assert bridge.stored == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("external_session_id" in w and "409" in w for w in warnings)

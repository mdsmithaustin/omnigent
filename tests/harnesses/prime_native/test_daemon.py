"""DaemonClient against an in-process fake Prime daemon replaying lines captured from 0.10.0."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.prime_native import daemon
from omnigent.harnesses.prime_native.daemon import (
    DaemonClient,
    DaemonClosedError,
    DaemonCommandError,
    DaemonError,
    DaemonProtocolError,
)

HELLO_0_10_0: dict[str, Any] = {
    "type": "daemon_hello",
    "socketPath": "/private/tmp/claude-501/pa11/d.sock",
    "protocol": {"name": "prime-agent.daemon", "version": 7},
    "schemaId": "protocol-7-schema-30-8e4b17c2a9f5",
    "schemaRevision": 30,
    "appVersion": "0.10.0",
    "runtime": {"buildId": "pa-daemon-rs-0.10.0", "executablePath": "/bin/prime-agent"},
    "supervisorGeneration": "sup:9555",
    "supervisorPid": 9555,
    "supervisorSocketPath": "/private/tmp/claude-501/pa11/d.sock",
    "updateResume": {"complete": True},
    "clientId": "09f581326094",
    "serverCapabilities": ["attach_snapshot", "event_sequence", "session_input_pause"],
}
HELLO_0_9_6 = {
    **HELLO_0_10_0,
    "schemaId": "protocol-7-schema-30-f908f493c9e1",
    "appVersion": "0.9.6",
}


def _session_event(sequence: int, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "session_event",
        "activeSessionId": "f2582bad16a9",
        "event": event,
        "meta": {
            "id": f"f2582bad16a9:{sequence}",
            "protocol": {"name": "prime-agent.daemon", "version": 7},
            "activeSessionId": "f2582bad16a9",
            "sequence": sequence,
            "cursor": {"generation": "4748632b5184", "sequence": sequence},
            "emittedAt": "2026-10-17T16:39:59.218Z",
        },
    }


AGENT_START = _session_event(21, {"type": "agent_start"})
TURN_START = _session_event(22, {"type": "turn_start"})
PROMPT_OK = {"id": "1", "type": "response", "command": "prompt", "success": True}
PROMPT_SUSPENDED = {
    "id": "1",
    "type": "response",
    "command": "prompt",
    "success": False,
    "error": "Cannot admit a session action while queued session input is suspended.",
}
RESUME_EMPTY = {
    "id": "1",
    "type": "response",
    "command": "resume_queue",
    "success": False,
    "error": "No queued work to resume",
}
CLOSING = {"type": "daemon_closing", "reason": "shutdown"}


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, *frames: dict[str, Any]) -> None:
        for frame in frames:
            self._writer.write(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        await self._writer.drain()

    async def receive(self) -> dict[str, Any]:
        return json.loads(await self._reader.readline())

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


Script = Callable[[Connection], Awaitable[None]]
Serve = Callable[[Script], Awaitable[str]]


@pytest.fixture
async def serve() -> AsyncIterator[Serve]:
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    tasks: list[asyncio.Task[None]] = []
    servers: list[asyncio.Server] = []

    async def start(script: Script) -> str:
        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            tasks.append(task)
            await script(Connection(reader, writer))

        path = str(directory / "d.sock")
        servers.append(await asyncio.start_unix_server(accept, path))
        return path

    yield start
    for server in servers:
        server.close()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    shutil.rmtree(directory)
    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert not failures, failures


def hello_then(*replies: dict[str, Any], hello: dict[str, Any] = HELLO_0_10_0) -> Script:
    """Send the hello, read one command, send replies, then wait for the client to hang up."""

    async def script(conn: Connection) -> None:
        await conn.send(hello)
        await conn.receive()
        await conn.send(*replies)
        await conn._reader.read()

    return script


@pytest.mark.parametrize("hello", [HELLO_0_9_6, HELLO_0_10_0], ids=["0.9.6", "0.10.0"])
async def test_connect_accepts_both_build_schema_ids(serve: Serve, hello: dict[str, Any]) -> None:
    async def script(conn: Connection) -> None:
        await conn.send(hello)
        await conn._reader.read()

    async with await DaemonClient.connect(await serve(script)) as client:
        assert client.hello.client_id == "09f581326094"
        assert client.hello.app_version == hello["appVersion"]
        assert client.hello.supervisor_pid == 9555


@pytest.mark.parametrize(
    "override",
    [
        {"protocol": {"name": "prime-agent.daemon", "version": 6}},
        {"protocol": {"name": "other-agent.daemon", "version": 7}},
        {"schemaRevision": 29},
        {"type": "response"},
        {"clientId": ""},
    ],
    ids=["version-6", "other-name", "revision-29", "not-a-hello", "empty-client-id"],
)
async def test_connect_rejects_unqualified_hello(serve: Serve, override: dict[str, Any]) -> None:
    async def script(conn: Connection) -> None:
        await conn.send({**HELLO_0_10_0, **override})
        await conn._reader.read()

    with pytest.raises(DaemonProtocolError):
        await DaemonClient.connect(await serve(script))


async def test_connect_rejects_eof_before_hello(serve: Serve) -> None:
    async def script(conn: Connection) -> None:
        await conn.close()

    with pytest.raises(DaemonProtocolError, match="closed before sending a hello"):
        await DaemonClient.connect(await serve(script))


async def test_command_sends_the_envelope_and_returns_data(serve: Serve) -> None:
    received: list[dict[str, Any]] = []

    async def script(conn: Connection) -> None:
        await conn.send(HELLO_0_10_0)
        received.append(await conn.receive())
        await conn.send(
            {
                "id": "1",
                "type": "response",
                "command": "attach",
                "success": True,
                "data": {"activeSessionId": "c300661e6bf7", "snapshot": {"messageCount": 0}},
            }
        )
        await conn._reader.read()

    async with await DaemonClient.connect(await serve(script)) as client:
        data = await client.command("attach", activeSessionId="c300661e6bf7")

    assert data == {"activeSessionId": "c300661e6bf7", "snapshot": {"messageCount": 0}}
    assert received == [
        {
            "type": "command",
            "id": "1",
            "protocol": {"name": "prime-agent.daemon", "version": 7},
            "clientId": "09f581326094",
            "command": {"id": "1", "type": "attach", "activeSessionId": "c300661e6bf7"},
        }
    ]


async def test_command_without_data_returns_empty_dict(serve: Serve) -> None:
    async with await DaemonClient.connect(await serve(hello_then(PROMPT_OK))) as client:
        assert await client.command("prompt", message="hi") == {}


@pytest.mark.parametrize(
    ("reply", "command", "error"),
    [
        (
            PROMPT_SUSPENDED,
            "prompt",
            "Cannot admit a session action while queued session input is suspended.",
        ),
        (RESUME_EMPTY, "resume_queue", "No queued work to resume"),
    ],
    ids=["prompt-suspended", "resume-empty"],
)
async def test_failed_command_raises_the_daemon_error(
    serve: Serve, reply: dict[str, Any], command: str, error: str
) -> None:
    async with await DaemonClient.connect(await serve(hello_then(reply))) as client:
        with pytest.raises(DaemonCommandError) as raised:
            await client.command(command, activeSessionId="f2582bad16a9")

    assert raised.value.command == command
    assert raised.value.error == error


async def test_events_interleaved_with_a_response_are_all_delivered(serve: Serve) -> None:
    script = hello_then(AGENT_START, PROMPT_OK, TURN_START, CLOSING)
    async with await DaemonClient.connect(await serve(script)) as client:
        result = await client.command("prompt", activeSessionId="f2582bad16a9", message="hi")
        events = [event async for event in client.events()]

    assert result == {}
    assert [event["event"] for event in events] == [
        {"type": "agent_start"},
        {"type": "turn_start"},
    ]
    assert [event["meta"]["sequence"] for event in events] == [21, 22]


async def test_responses_are_matched_by_id_when_replies_arrive_out_of_order(
    serve: Serve,
) -> None:
    async def script(conn: Connection) -> None:
        await conn.send(HELLO_0_10_0)
        first, second = await conn.receive(), await conn.receive()
        await conn.send(
            {
                "id": second["id"],
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"which": "second"},
            },
            {
                "id": first["id"],
                "type": "response",
                "command": "list",
                "success": True,
                "data": {"which": "first"},
            },
        )
        await conn._reader.read()

    async with await DaemonClient.connect(await serve(script)) as client:
        first, second = await asyncio.gather(client.command("list"), client.command("get_state"))

    assert (first, second) == ({"which": "first"}, {"which": "second"})


async def test_daemon_closing_ends_events_after_the_shutdown_reply(serve: Serve) -> None:
    shutdown_ok = {"id": "1", "type": "response", "command": "shutdown", "success": True}
    async with await DaemonClient.connect(await serve(hello_then(shutdown_ok, CLOSING))) as client:
        assert await client.command("shutdown") == {}
        assert [event async for event in client.events()] == []
        assert [event async for event in client.events()] == []
        with pytest.raises(DaemonClosedError):
            await client.command("list")


async def test_eof_fails_pending_commands_and_ends_events(serve: Serve) -> None:
    async def script(conn: Connection) -> None:
        await conn.send(HELLO_0_10_0, AGENT_START)
        await conn.receive()
        await conn.close()

    async with await DaemonClient.connect(await serve(script)) as client:
        with pytest.raises(DaemonClosedError):
            await client.command("list")
        assert [event["meta"]["sequence"] async for event in client.events()] == [21]


async def test_snapshot_line_larger_than_the_default_stream_limit(serve: Serve) -> None:
    messages = [{"role": "assistant", "text": "x" * 1024} for _ in range(3072)]
    reply = {
        "id": "1",
        "type": "response",
        "command": "attach",
        "success": True,
        "data": {"snapshot": {"messages": messages}},
    }
    async with await DaemonClient.connect(await serve(hello_then(reply))) as client:
        data = await client.command("attach", activeSessionId="f2582bad16a9")

    assert len(data["snapshot"]["messages"]) == 3072


async def test_an_unanswered_command_times_out_and_the_connection_stays_usable(
    serve: Serve,
) -> None:
    state = {"id": "2", "type": "response", "command": "get_state", "success": True, "data": {}}

    async def script(conn: Connection) -> None:
        await conn.send(HELLO_0_10_0)
        await conn.receive()
        if await conn._reader.readline():
            await conn.send(state)
        await conn._reader.read()

    async with asyncio.timeout(2):
        async with await DaemonClient.connect(await serve(script)) as client:
            with pytest.raises(DaemonError, match=r"prompt got no reply within 0\.1 s"):
                await client.command("prompt", timeout=0.1)
            assert await client.command("get_state") == {}


async def test_an_event_backlog_past_its_bound_ends_the_connection(
    serve: Serve, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(daemon, "_EVENT_BACKLOG", 2)
    updates = [_session_event(n, {"type": "message_update"}) for n in range(1, 6)]

    async def script(conn: Connection) -> None:
        await conn.send(HELLO_0_10_0, *updates)
        await conn._reader.read()

    async with asyncio.timeout(2):
        async with await DaemonClient.connect(await serve(script)) as client:
            assert [event async for event in client.events()] == []
            with pytest.raises(DaemonClosedError):
                await client.command("list")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "Prime session events backed up past 2; dropping them and closing the daemon connection"
    ]

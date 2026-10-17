"""Asyncio client for the Prime daemon's NDJSON socket (protocol 7, schema revision 30)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

DAEMON_PROTOCOL = {"name": "prime-agent.daemon", "version": 7}
DAEMON_SCHEMA_REVISION = 30
# schemaId is a per-build hash, so only protocol and schemaRevision gate compatibility.

_LINE_LIMIT = 64 * 1024 * 1024
_HELLO_TIMEOUT_S = 5.0


class DaemonError(Exception):
    """Base class for daemon client failures."""


class DaemonProtocolError(DaemonError):
    """The peer sent a hello or frame this client does not accept."""


class DaemonClosedError(DaemonError):
    """The connection ended before the awaited reply."""


class DaemonCommandError(DaemonError):
    """The daemon answered a command with ``success: false``."""

    def __init__(self, command: str, error: str) -> None:
        super().__init__(f"{command}: {error}")
        self.command = command
        self.error = error


@dataclass(frozen=True)
class DaemonHello:
    client_id: str
    schema_id: str | None
    app_version: str | None
    socket_path: str | None
    supervisor_socket_path: str | None
    supervisor_pid: int | None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_pid(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def parse_hello(raw: Mapping[str, object]) -> DaemonHello:
    """Validate a ``daemon_hello`` frame and return its typed fields."""
    if raw.get("type") != "daemon_hello":
        raise DaemonProtocolError("first frame is not a daemon_hello")
    if raw.get("protocol") != DAEMON_PROTOCOL:
        raise DaemonProtocolError(f"unsupported daemon protocol {raw.get('protocol')!r}")
    if raw.get("schemaRevision") != DAEMON_SCHEMA_REVISION:
        raise DaemonProtocolError(f"unsupported schema revision {raw.get('schemaRevision')!r}")
    client_id = raw.get("clientId")
    if not isinstance(client_id, str) or not client_id:
        raise DaemonProtocolError("daemon_hello has no clientId")
    return DaemonHello(
        client_id=client_id,
        schema_id=_optional_str(raw.get("schemaId")),
        app_version=_optional_str(raw.get("appVersion")),
        socket_path=_optional_str(raw.get("socketPath")),
        supervisor_socket_path=_optional_str(raw.get("supervisorSocketPath")),
        supervisor_pid=_optional_pid(raw.get("supervisorPid")),
    )


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    line = await reader.readline()
    if not line:
        return None
    try:
        frame = json.loads(line)
    except ValueError as exc:
        raise DaemonProtocolError("daemon sent a malformed frame") from exc
    if not isinstance(frame, dict):
        raise DaemonProtocolError("daemon sent a non-object frame")
    return frame


class DaemonClient:
    """One daemon connection; a single reader task demultiplexes replies and events."""

    def __init__(
        self,
        hello: DaemonHello,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.hello = hello
        self._reader = reader
        self._writer = writer
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._closed = False
        self._pump_task = asyncio.create_task(self._pump())

    @classmethod
    async def connect(
        cls, socket_path: str | Path, *, hello_timeout: float = _HELLO_TIMEOUT_S
    ) -> Self:
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=_LINE_LIMIT)
        try:
            async with asyncio.timeout(hello_timeout):
                raw = await _read_frame(reader)
            if raw is None:
                raise DaemonProtocolError("daemon closed before sending a hello")
            hello = parse_hello(raw)
        except BaseException:
            writer.close()
            raise
        return cls(hello, reader, writer)

    async def command(self, type: str, /, **fields: object) -> dict[str, Any]:
        """Send one command and return its ``data`` (``{}`` when the reply has none)."""
        if self._closed:
            raise DaemonClosedError("daemon connection is closed")
        command_id = str(next(self._ids))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        envelope = {
            "type": "command",
            "id": command_id,
            "protocol": DAEMON_PROTOCOL,
            "clientId": self.hello.client_id,
            "command": {"id": command_id, "type": type, **fields},
        }
        try:
            self._writer.write(json.dumps(envelope, separators=(",", ":")).encode() + b"\n")
            await self._writer.drain()
            return await future
        except OSError as exc:
            raise DaemonClosedError(f"daemon connection failed: {exc}") from exc
        finally:
            self._pending.pop(command_id, None)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield ``session_event`` envelopes until EOF or ``daemon_closing``."""
        while (event := await self._events.get()) is not None:
            yield event
        self._events.put_nowait(None)

    async def close(self) -> None:
        self._closed = True
        self._writer.close()
        self._pump_task.cancel()
        await asyncio.gather(self._pump_task, return_exceptions=True)
        self._finish(DaemonClosedError("daemon connection is closed"))
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _pump(self) -> None:
        failure: DaemonError = DaemonClosedError("daemon connection ended")
        try:
            while (frame := await _read_frame(self._reader)) is not None:
                match frame.get("type"):
                    case "response":
                        self._resolve(frame)
                    case "session_event":
                        self._events.put_nowait(frame)
                    case "daemon_closing":
                        break
        except DaemonProtocolError as exc:
            failure = exc
        except (OSError, ValueError) as exc:
            failure = DaemonClosedError(f"daemon connection failed: {exc}")
        self._finish(failure)

    def _resolve(self, frame: dict[str, Any]) -> None:
        future = self._pending.get(str(frame.get("id")))
        if future is None or future.done():
            return
        if frame.get("success") is True:
            data = frame.get("data")
            future.set_result(data if isinstance(data, dict) else {})
        else:
            future.set_exception(
                DaemonCommandError(str(frame.get("command")), str(frame.get("error")))
            )

    def _finish(self, failure: DaemonError) -> None:
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._events.put_nowait(None)

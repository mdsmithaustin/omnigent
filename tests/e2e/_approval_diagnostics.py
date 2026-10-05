"""Passive receipts for the mock sessions approval test."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil

from omnigent.process_logging import (
    _AUTHORIZATION_PATTERN,
    _NAMED_SECRET_PATTERN,
    _SECRET_PATTERNS,
    _WHITESPACE_SECRET_PATTERN,
)

_PTY_BYTES = 512 * 1024
_PTY_EVENTS = 4096
_LINE_BYTES = 16 * 1024
_RUNTIME_FILE_BYTES = 256 * 1024
_RUNTIME_TOTAL_BYTES = 1024 * 1024
_RUNTIME_FILES = 8
_HTTP_BYTES = 128 * 1024
_PENDING_ENTRIES = 32

_UNKNOWN = "unknown"
_SESSION = re.compile(r"\[sessions-adapter\] session created id='([A-Za-z0-9_-]+)'")
_RUNNER = re.compile(r"\[sessions-adapter\] runner bound id='([A-Za-z0-9_-]+)'")
_SERVER = re.compile(r"(http://127\.0\.0\.1:[0-9]+)\s+·\s+server\b")
_SENSITIVE_LINE = re.compile(
    r"(?im)^.*(?:authorization|cookie|content_preview|\bpreview\b|requestedSchema|raw_bundle|"
    r"\bconfig\b|\benvironment\b|\benv\b|\bprovider\b|api[_-]?key|"
    r"password|credential|secret|token\s*[:=]).*$"
)
_SECRET = re.compile(r"(?i)\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+")
_SAFE_LINE = re.compile(r"^(?:ready|approval required|approved|Hello)[\r\n]*$")


def redact(text: str) -> str:
    """Mask sensitive records and continuations without changing event offsets."""
    lines = []
    sensitive = False
    for line in text.splitlines(keepends=True):
        if _SENSITIVE_LINE.search(line):
            sensitive = True
        lines.append(
            "".join(c if c in "\r\n" else "*" for c in line)
            if sensitive and not _SAFE_LINE.match(line)
            else line
        )
    text = "".join(lines)
    for pattern in (
        _AUTHORIZATION_PATTERN,
        _NAMED_SECRET_PATTERN,
        _WHITESPACE_SECRET_PATTERN,
        *_SECRET_PATTERNS,
        _SECRET,
    ):
        text = pattern.sub(lambda match: "*" * len(match[0]), text)
    return text


def session_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Select only approval correlation metadata from a session response."""
    pending = payload.get("pending_elicitations")
    result = {
        "session_id": _metadata_value(payload.get("id")),
        "status": _metadata_value(payload.get("status")),
        "runner_id": _metadata_value(payload.get("runner_id")),
        "pending_elicitations": [
            {
                "elicitation_id": _metadata_value(
                    event.get("elicitation_id") if isinstance(event, dict) else None
                ),
                "type": _metadata_value(event.get("type") if isinstance(event, dict) else None),
                "phase": _metadata_value(_params(event).get("phase")),
                "policy_name": _metadata_value(_params(event).get("policy_name")),
            }
            for event in pending[:_PENDING_ENTRIES]
        ]
        if isinstance(pending, list)
        else _UNKNOWN,
    }
    if isinstance(pending, list) and len(pending) > _PENDING_ENTRIES:
        result["pending_truncated"] = True
    return result


def _metadata_value(value: Any) -> str:
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        and redact(value) == value
        else _UNKNOWN
    )


def _params(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("params") if isinstance(event, dict) else None
    return params if isinstance(params, dict) else {}


def runtime_records(text: str, session_id: str) -> str:
    """Keep bounded complete log headers and owned IDs; discard message payloads."""
    records = []
    used = 0
    session_id = _metadata_value(session_id)
    for line in text[:_RUNTIME_FILE_BYTES].splitlines(keepends=True):
        if not line.endswith(("\n", "\r")) or len(line) > _LINE_BYTES:
            record = "[TRUNCATED]\n"
        else:
            header, separator, message = line.rstrip("\r\n").partition(" | ")
            if separator and re.match(r"^(?:DEBUG|INFO|WARN|ERROR|CRIT)\s+\d", header):
                correlation = (
                    f" session={session_id}"
                    if session_id != _UNKNOWN and session_id in message
                    else ""
                )
                record = f"{redact(header)} | [REDACTED]{correlation}\n"
            else:
                record = "[REDACTED]\n"
        size = len(record.encode("utf-8"))
        if used + size > _RUNTIME_FILE_BYTES - 32:
            records.append("[TRUNCATED]\n")
            break
        records.append(record)
        used += size
    return "".join(records)


class _PtyWriter:
    def __init__(self, capture: ApprovalDiagnostics, direction: str) -> None:
        self.capture = capture
        self.direction = direction

    def write(self, text: str) -> None:
        try:
            self.capture.record(self.direction, text)
        except Exception as error:
            self.capture.metadata["pty_error"] = type(error).__name__[:64]

    def flush(self) -> None:
        pass


class ApprovalDiagnostics:
    def __init__(self, directory: Path, data_dir: Path) -> None:
        self.directory = directory / ".omnigent" / "logs" / "approval"
        self.directory.mkdir(parents=True)
        self.data_dir = data_dir
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {}
        self.pty_bytes = 0
        self.line_bytes = {"read": 0, "send": 0}
        self.stopped: set[str] = set()

    def record(self, direction: str, text: str) -> None:
        if direction in self.stopped:
            return
        remaining = _PTY_BYTES - self.pty_bytes
        if remaining <= 0 or len(self.events) >= _PTY_EVENTS:
            self.stopped.add(direction)
            self.metadata["pty_truncated"] = True
            return
        raw = text[:remaining].encode("utf-8")[:remaining]
        bounded = raw.decode("utf-8", errors="ignore")
        accepted = []
        for line in bounded.splitlines(keepends=True):
            size = len(line.encode("utf-8"))
            available = _LINE_BYTES - self.line_bytes[direction]
            if size > available:
                accepted.append(
                    line[:available].encode("utf-8")[:available].decode("utf-8", errors="ignore")
                )
                self.stopped.add(direction)
                break
            accepted.append(line)
            self.line_bytes[direction] = (
                0 if line.endswith(("\n", "\r")) else self.line_bytes[direction] + size
            )
        bounded = "".join(accepted)
        self.pty_bytes += len(bounded.encode("utf-8"))
        if len(bounded) < len(text):
            self.stopped.add(direction)
        if self.stopped:
            self.metadata["pty_truncated"] = True
        self.events.append(
            {
                "direction": direction,
                "utc": datetime.now(timezone.utc).isoformat(),
                "elapsed": time.monotonic() - self.started,
                "text": bounded,
            }
        )

    def failure(self, error: Exception) -> None:
        self.metadata["capture_error"] = type(error).__name__[:64]
        self.write_metadata()

    def write_metadata(self) -> None:
        try:
            (self.directory / "capture.log").write_text(json.dumps(self.metadata, indent=2))
        except Exception as error:
            self.metadata["receipt_error"] = type(error).__name__[:64]

    def attach(self, child: Any) -> None:
        child.logfile_read = _PtyWriter(self, "read")
        child.logfile_send = _PtyWriter(self, "send")
        self.metadata["cli_pid"] = child.pid

    def write_pty(self) -> None:
        sanitized = {}
        for direction in ("read", "send"):
            text = "".join(
                event["text"] for event in self.events if event["direction"] == direction
            )
            complete = max(text.rfind("\n"), text.rfind("\r")) + 1
            sanitized[direction] = redact(text[:complete]) + "*" * (len(text) - complete)
            if direction == "send" and text.endswith("\x04"):
                sanitized[direction] = sanitized[direction][:-1] + "\x04"
            if complete < len(text):
                self.metadata["pty_partial_line_discarded"] = True
        offsets = {"read": 0, "send": 0}
        with (self.directory / "pty.log").open("w") as log:
            for event in self.events:
                direction = event["direction"]
                start = offsets[direction]
                end = start + len(event["text"])
                log.write(json.dumps({**event, "text": sanitized[direction][start:end]}) + "\n")
                offsets[direction] = end
            if self.metadata.get("pty_truncated"):
                log.write(json.dumps({"truncated": True, "text": "[TRUNCATED]"}) + "\n")

    async def _snapshot(self, url: str, session_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(trust_env=False, timeout=1.0) as client:
            async with client.stream(
                "GET", f"{url}/v1/sessions/{session_id}", headers={"Accept-Encoding": "identity"}
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=8192):
                    if len(body) + len(chunk) > _HTTP_BYTES:
                        self.metadata["http_truncated"] = True
                        return session_projection({"id": session_id})
                    body.extend(chunk)
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("unavailable session metadata")
                return session_projection(payload)

    def capture(self, child: Any, *, timeout: bool) -> None:
        """Diagnostic errors remain receipts rather than replacing a test failure."""
        self.metadata["timeout"] = timeout
        try:
            self.write_pty()
            output = "".join(e["text"] for e in self.events if e["direction"] == "read")
            match = _SESSION.search(output)
            session_id = _metadata_value(match[1]) if match else _UNKNOWN
            runner = _RUNNER.search(output)
            self.metadata.update(
                session_id=session_id,
                runner_id=_metadata_value(runner[1]) if runner else _UNKNOWN,
                stream_subscription_started="[sessions-adapter] subscribing /stream" in output,
                post_started="[sessions-adapter] POST /events session=" in output,
                post_returned="[sessions-adapter] POST returned;" in output,
                timeout=timeout,
                captured_elapsed=time.monotonic() - self.started,
                session=session_projection({"id": session_id}),
            )
            processes = [psutil.Process(child.pid)]
            children = processes[0].children(recursive=True)
            processes.extend(children[:15])
            if len(children) > 15:
                self.metadata["runtime_truncated"] = True
            owned_pids = {process.pid for process in processes}
            paths: set[Path] = set()
            for process in processes:
                with contextlib.suppress(psutil.Error):
                    for opened in process.open_files():
                        path = Path(opened.path)
                        if (
                            path.resolve().is_relative_to((self.data_dir / "logs").resolve())
                            and path.suffix == ".log"
                        ):
                            if len(paths) >= _RUNTIME_FILES and path not in paths:
                                self.metadata["runtime_truncated"] = True
                                break
                            paths.add(path)
            remaining = _RUNTIME_TOTAL_BYTES
            output_remaining = _RUNTIME_TOTAL_BYTES
            copied = 0
            for index, path in enumerate(sorted(paths)):
                if min(remaining, output_remaining) < 32:
                    self.metadata["runtime_truncated"] = True
                    break
                limit = min(_RUNTIME_FILE_BYTES, remaining, output_remaining)
                with path.open("rb") as source:
                    raw = source.read(limit)
                truncated = path.stat().st_size > len(raw)
                remaining -= len(raw)
                text = runtime_records(raw.decode("utf-8", errors="replace"), session_id)
                truncated = truncated or "[TRUNCATED]" in text
                encoded = text.encode("utf-8")
                if len(encoded) > limit - 32:
                    text = (
                        encoded[: max(0, limit - 32)]
                        .decode("utf-8", errors="ignore")
                        .rsplit("\n", 1)[0]
                        + "\n"
                    )
                    truncated = True
                if truncated:
                    text += "[TRUNCATED]\n"
                    self.metadata["runtime_truncated"] = True
                (self.directory / f"runtime-{index}.log").write_text(text)
                output_remaining -= len(text.encode("utf-8"))
                copied += 1
            self.metadata["runtime_bytes_copied"] = _RUNTIME_TOTAL_BYTES - output_remaining
            self.metadata["runtime_bytes_read"] = _RUNTIME_TOTAL_BYTES - remaining
            self.metadata["runtime_files_copied"] = copied
            self.metadata["owned_process_count"] = len(owned_pids)
            self.metadata["owned_log_count"] = len(paths)
            if timeout and session_id != _UNKNOWN:
                plain_output = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output)
                server = _SERVER.search(plain_output)
                if server is None:
                    raise ValueError("unknown owned server")
                self.metadata["session"] = asyncio.run(
                    asyncio.wait_for(self._snapshot(server[1], session_id), 1.0)
                )
        except Exception as error:
            self.metadata["capture_error"] = type(error).__name__[:64]
        finally:
            self.write_metadata()

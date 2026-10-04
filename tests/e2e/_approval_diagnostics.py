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

_UNKNOWN = "unknown"
_SESSION = re.compile(r"\[sessions-adapter\] session created id='([A-Za-z0-9_-]+)'")
_RUNNER = re.compile(r"\[sessions-adapter\] runner bound id='([A-Za-z0-9_-]+)'")
_SERVER = re.compile(r"(http://127\.0\.0\.1:[0-9]+)\s+·\s+server\b")
_SENSITIVE_LINE = re.compile(
    r"(?im)^.*(?:authorization|cookie|content_preview|\bpreview\b|requestedSchema|raw_bundle|"
    r"\bconfig\b|\benvironment\b|\benv\b|\bprovider\b|api[_-]?key|"
    r"password|credential|secret|token\s*[:=]).*$"
)
_SECRET = re.compile(r"(?i)bearer\s+\S+|\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    """Mask complete sensitive records, preserving PTY event offsets."""
    for pattern in (_SENSITIVE_LINE, _SECRET):
        text = pattern.sub(lambda match: "*" * len(match[0]), text)
    return text


def session_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Select only approval correlation metadata from a session response."""
    pending = payload.get("pending_elicitations")
    return {
        "session_id": _metadata_value(payload.get("id")),
        "status": _metadata_value(payload.get("status")),
        "runner_id": _metadata_value(payload.get("runner_id")),
        "pending_elicitations": [
            {
                "elicitation_id": _metadata_value(event.get("elicitation_id")),
                "type": _metadata_value(event.get("type")),
                "phase": _metadata_value(_params(event).get("phase")),
                "policy_name": _metadata_value(_params(event).get("policy_name")),
            }
            for event in pending
        ]
        if isinstance(pending, list)
        else _UNKNOWN,
    }


def _metadata_value(value: Any) -> str:
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        and redact(value) == value
        else _UNKNOWN
    )


def _params(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("params")
    return params if isinstance(params, dict) else {}


def runtime_records(text: str, session_id: str) -> str:
    """Keep log record headers and owned IDs; discard arbitrary payloads."""
    records = []
    for line in text.splitlines():
        header, separator, message = line.partition(" | ")
        if separator and re.match(r"^(?:DEBUG|INFO|WARN|ERROR|CRIT)\s+\d", header):
            correlation = (
                f" session={session_id}"
                if session_id != _UNKNOWN and session_id in message
                else ""
            )
            records.append(f"{redact(header)} | [REDACTED]{correlation}")
        else:
            records.append("[REDACTED]")
    return "\n".join(records) + "\n"


class _PtyWriter:
    def __init__(self, capture: ApprovalDiagnostics, direction: str) -> None:
        self.capture = capture
        self.direction = direction

    def write(self, text: str) -> None:
        self.capture.events.append(
            {
                "direction": self.direction,
                "utc": datetime.now(timezone.utc).isoformat(),
                "elapsed": time.monotonic() - self.capture.started,
                "text": text,
            }
        )

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

    def attach(self, child: Any) -> None:
        child.logfile_read = _PtyWriter(self, "read")
        child.logfile_send = _PtyWriter(self, "send")
        self.metadata["cli_pid"] = child.pid

    def write_pty(self) -> None:
        sanitized = {
            direction: redact(
                "".join(event["text"] for event in self.events if event["direction"] == direction)
            )
            for direction in ("read", "send")
        }
        offsets = {"read": 0, "send": 0}
        with (self.directory / "pty.log").open("w") as log:
            for event in self.events:
                direction = event["direction"]
                start = offsets[direction]
                end = start + len(event["text"])
                log.write(json.dumps({**event, "text": sanitized[direction][start:end]}) + "\n")
                offsets[direction] = end

    async def _snapshot(self, url: str, session_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"{url}/v1/sessions/{session_id}")
            response.raise_for_status()
            return session_projection(response.json())

    def capture(self, child: Any, *, timeout: bool) -> None:
        """Diagnostic errors remain receipts rather than replacing a test failure."""
        try:
            self.write_pty()
            output = "".join(e["text"] for e in self.events if e["direction"] == "read")
            match = _SESSION.search(output)
            session_id = match[1] if match else _UNKNOWN
            runner = _RUNNER.search(output)
            self.metadata.update(
                session_id=session_id,
                runner_id=runner[1] if runner else _UNKNOWN,
                stream_subscription_started="[sessions-adapter] subscribing /stream" in output,
                post_started="[sessions-adapter] POST /events session=" in output,
                post_returned="[sessions-adapter] POST returned;" in output,
                timeout=timeout,
                captured_elapsed=time.monotonic() - self.started,
                session=session_projection({"id": session_id}),
            )
            processes = [psutil.Process(child.pid)]
            processes.extend(processes[0].children(recursive=True))
            owned_pids = {process.pid for process in processes}
            paths: set[Path] = set()
            for process in processes:
                with contextlib.suppress(psutil.Error):
                    for opened in process.open_files():
                        path = Path(opened.path)
                        if path.is_relative_to(self.data_dir / "logs") and path.suffix == ".log":
                            paths.add(path)
            for index, path in enumerate(sorted(paths)):
                (self.directory / f"runtime-{index}.log").write_text(
                    runtime_records(path.read_text(), session_id)
                )
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
            self.metadata["capture_error"] = type(error).__name__
        finally:
            with contextlib.suppress(OSError):
                (self.directory / "capture.log").write_text(json.dumps(self.metadata, indent=2))

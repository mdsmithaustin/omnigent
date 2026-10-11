#!/usr/bin/env -S uv run --no-sync python

from __future__ import annotations

import argparse
import ast
import asyncio
import codecs
import errno
import hashlib
import json
import os
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import ClassVar, Literal, TextIO

import httpx
import pexpect
import psutil
import yaml
from adapter_probe import (
    PRIME_SHA256,
    ProcessIdentity,
    SelectedRootIdentity,
    assert_same_selected_root_identity,
    assert_same_sources,
    deployed_extension_identity,
    import_witnesses,
    live_initial_process_records,
    qualify_kernel_identity,
    qualify_owned_host_record,
    qualify_selected_root_roles,
    read_actual_kernel_identity,
    requested_kernel_identity,
    selected_root_identity_record,
    sha256,
    source_snapshot,
    unused_port,
)
from verify import assistant_text, doctor, wait_for

REPO = Path(__file__).resolve().parents[4]
FINALIZATION_ERRORS = (Exception, KeyboardInterrupt, SystemExit)
# Covers the host startup maintenance delay plus its Prime orphan stage.
MAINTENANCE_RECLAIM_TIMEOUT_S = 30


@dataclass(frozen=True)
class Request:
    prime_path: Path
    kernel_entry: Path
    auth_source: Path
    evidence_parent: Path
    case: Literal["selector", "runner", "pane", "waits"]
    expected_memory: str | None = None
    cooperative_cleanup: bool = False


@dataclass(frozen=True)
class ClockProfile:
    name: Literal["runner", "pane"]
    runner_timeout_s: int
    pane_timeout_s: int
    wait_s: int
    quiet_s: int
    deadline_s: int


RUNNER = ClockProfile("runner", 30, 0, 60, 37, 120)
PANE = ClockProfile("pane", 0, 30, 300, 280, 360)


@dataclass(frozen=True)
class Observation:
    claim: str
    status: Literal["VERIFIED", "FAILED", "NOT VERIFIED"]
    evidence: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class CaseResult:
    receipt: Path
    required_claims: tuple[str, ...]
    observations: tuple[Observation, ...]
    cleanup: Observation

    @property
    def qualified(self) -> bool:
        claims = {item.claim: item for item in self.observations}
        return self.cleanup.status == "VERIFIED" and all(
            name in claims and claims[name].status == "VERIFIED" for name in self.required_claims
        )

    @property
    def committed(self) -> bool:
        completion = self.receipt.parent / "completion.json"
        if not completion.is_file():
            return False
        value = json.loads(completion.read_text())
        return (
            value["passed"] is self.qualified
            and value["result_sha256"] == sha256(self.receipt)
            and value["manifest_sha256"] == sha256(self.receipt.parent / "manifest.json")
        )

    @property
    def passed(self) -> bool:
        return self.qualified and self.committed


@dataclass(frozen=True)
class _CaseDraft:
    result: CaseResult
    payload: dict
    manifest: dict


def _publish_case(draft: _CaseDraft) -> CaseResult:
    result = draft.result
    evidence = result.receipt.parent
    write_json(
        result.receipt,
        {
            **draft.payload,
            "qualified": result.qualified,
            "authority": "completion.json",
        },
        immutable=True,
    )
    artifacts = []
    for path in sorted(evidence.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("publication_symlink")
        if path.is_file():
            path.chmod(0o400)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            artifacts.append(
                {
                    "path": str(path.relative_to(evidence)),
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
    manifest = evidence / "manifest.json"
    write_json(
        manifest,
        {
            **draft.manifest,
            "artifacts": artifacts,
            "qualified": result.qualified,
            "authority": "completion.json",
        },
        immutable=True,
    )
    staged = evidence / ".completion-pending.json"
    write_json(
        staged,
        {
            "passed": result.qualified,
            "result_sha256": sha256(result.receipt),
            "manifest_sha256": sha256(manifest),
        },
        immutable=True,
    )
    for path in (result.receipt, manifest, staged):
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    os.rename(staged, evidence / "completion.json")
    return result


@dataclass(frozen=True)
class _ReplyOperation:
    prompt: str
    literal: str
    code: str | None
    deadline: float
    native_baseline: tuple[str, ...]
    public_baseline: tuple[str, ...]
    native_ids: tuple[str, ...]
    public_ids: tuple[str, ...]
    journal_path: Path
    session_id: str
    external_id: str
    session_headers: tuple[str, ...]
    root: SelectedRootIdentity | None


@dataclass(frozen=True)
class _NativeReceivedRecord:
    record: dict
    received_at: float
    end_existed: bool


@dataclass(frozen=True)
class _NativeQueueReceipt:
    root: SelectedRootIdentity
    active_session_id: str
    prompt: str
    generation: str
    sequence: int
    post_started_at: float
    received_at: float
    end_existed: bool


@dataclass(frozen=True)
class _CompletedWait:
    root: SelectedRootIdentity
    nonce: str
    pid: int
    started_at: float
    ended_at: float
    call_id: str
    result_id: str
    observed_at: float


@dataclass(frozen=True)
class _SteeringConsumption:
    root: SelectedRootIdentity
    prompt: str
    user_id: str
    reply_id: str
    user_observed_at: float
    reply_observed_at: float


def _native_acceptance(
    receipt: _NativeQueueReceipt, completed: _CompletedWait, consumed: _SteeringConsumption
) -> bool:
    assert_same_selected_root_identity(receipt.root, completed.root)
    assert_same_selected_root_identity(completed.root, consumed.root)
    return (
        receipt.prompt == consumed.prompt
        and not receipt.end_existed
        and completed.started_at < receipt.post_started_at <= receipt.received_at
        and receipt.received_at < completed.ended_at
        and completed.ended_at <= consumed.user_observed_at <= consumed.reply_observed_at
    )


class _NativeReceiveClosed(ConnectionError):
    pass


class _NativeQueueObserver:
    protocol: ClassVar[dict] = {"name": "prime-agent.daemon", "version": 7}
    sequenced_types: ClassVar[set[str]] = {
        "session_event",
        "session_status",
        "session_replaced",
        "session_resynced",
        "session_closed",
        "extension_ui_request",
        "extension_error",
    }

    def __init__(
        self,
        root: SelectedRootIdentity,
        journal: Path,
        end_path: Path,
        prompt: str,
        deadline: float,
        check_owner: Callable[[], None],
        record_evidence: Callable[[dict], None] | None = None,
    ):
        self.root = root
        self.journal = journal
        self.end_path = end_path
        self.prompt = prompt
        self.deadline = deadline
        self.check_owner = check_owner
        self.record_evidence = record_evidence
        self.selected_records: list[dict] = []
        self.state: Literal["created", "connected", "attached", "closed"] = "created"
        self.socket: socket.socket | None = None
        self.closing_socket: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.buffer = b""
        self.client_id = "wait-observer-" + uuid.uuid4().hex
        self.active_session_id: str | None = None
        self.generation: str | None = None
        self.sequence: int | None = None
        self.baseline_sequence: int | None = None
        self.post_started_at: float | None = None
        self.receipt: _NativeQueueReceipt | None = None
        self.error: str | None = None
        self.socket_path: Path | None = None
        self.hello: dict | None = None
        self.command_receipts: list[dict] = []
        self.baseline_prompt_absent = False

    def receive(self, deadline: float) -> _NativeReceivedRecord:
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("native_observer_deadline")
            connection = self.socket
            if connection is None and self.stop.is_set() and self.closing_socket is not None:
                raise _NativeReceiveClosed("native_observer_owned_socket_closed")
            try:
                connection.settimeout(remaining)
                chunk = connection.recv(65536)
            except OSError as exc:
                if (
                    self.stop.is_set()
                    and connection is self.closing_socket
                    and exc.errno
                    in (
                        errno.EBADF,
                        errno.ENOTCONN,
                        errno.ECONNRESET,
                    )
                ):
                    raise _NativeReceiveClosed("native_observer_owned_socket_closed") from exc
                raise
            if not chunk:
                if self.stop.is_set() and connection is self.closing_socket:
                    raise _NativeReceiveClosed("native_observer_owned_socket_closed")
                raise ConnectionError("native_observer_socket_closed")
            self.buffer += chunk
            if len(self.buffer) > 8 * 1024 * 1024:
                raise RuntimeError("native_observer_record_too_large")
        line, self.buffer = self.buffer.split(b"\n", 1)
        received_at = time.monotonic()
        end_existed = self.end_path.exists()
        record = json.loads(line)
        if not isinstance(record, dict):
            raise RuntimeError("native_observer_record_malformed")
        return _NativeReceivedRecord(record, received_at, end_existed)

    def request(self, command: dict, pending: list[_NativeReceivedRecord]) -> dict:
        identity = uuid.uuid4().hex
        envelope = {
            "type": "command",
            "id": identity,
            "protocol": self.protocol,
            "clientId": self.client_id,
            "command": command,
        }
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native_observer_deadline")
        self.socket.settimeout(remaining)
        self.socket.sendall(json.dumps(envelope).encode() + b"\n")
        while True:
            received = self.receive(self.deadline)
            record = received.record
            if record.get("type") == "response" and record.get("id") == identity:
                if record.get("command") != command["type"] or record.get("success") is not True:
                    raise RuntimeError("native_observer_command_rejected")
                data = record.get("data")
                if not isinstance(data, dict):
                    raise RuntimeError("native_observer_response_malformed")
                self.command_receipts.append(
                    {
                        "id": identity,
                        "command": command["type"],
                        "success": True,
                        "received_at": received.received_at,
                    }
                )
                return data
            pending.append(received)

    def admit_snapshot(self, attached: dict, pending: list[_NativeReceivedRecord]) -> None:
        snapshot = attached.get("snapshot", {})
        summary = snapshot.get("summary", {})
        if (
            attached.get("protocol") != self.protocol
            or attached.get("activeSessionId") != self.active_session_id
            or snapshot.get("activeSessionId") != self.active_session_id
            or summary.get("activeSessionId") != self.active_session_id
            or summary.get("sessionId") != self.root.external_session_id
            or not isinstance(summary.get("sessionFile"), str)
            or Path(summary["sessionFile"]).resolve() != self.journal.resolve()
            or summary.get("workerPid") != self.root.roles.worker.pid
            or summary.get("workerState") != "ready"
        ):
            raise RuntimeError("native_observer_attach_identity_mismatch")
        cursor = attached.get("lastEventCursor", {})
        sequence = attached.get("lastEventSequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or not isinstance(cursor.get("generation"), str)
            or not cursor["generation"]
            or cursor.get("sequence") != sequence
            or snapshot.get("lastEventSequence") != sequence
            or snapshot.get("lastEventCursor") != cursor
            or attached.get("replay", {}).get("status") != "complete"
        ):
            raise RuntimeError("native_observer_baseline_metadata_missing")
        self.baseline_sequence = sequence
        if self.prompt in json.dumps(attached):
            raise RuntimeError("native_observer_baseline_duplicate")
        for received in pending:
            record = received.record
            if self.selected(record) and self.prompt in json.dumps(record):
                self.observe_record(received, "native_observer_baseline_duplicate", sequence)
                raise RuntimeError("native_observer_baseline_duplicate")
        self.baseline_prompt_absent = True
        self.generation = cursor["generation"]
        self.sequence = self.baseline_sequence = sequence
        self.state = "attached"
        for received in pending:
            record = received.record
            if not self.selected(record):
                continue
            reason = "queued_baseline_record"
            live = False
            try:
                if record.get("activeSessionId") != self.active_session_id:
                    raise RuntimeError("native_observer_event_metadata_missing_or_mismatch")
                if record.get("type") in {
                    "session_replaced",
                    "session_resynced",
                    "session_closed",
                    "extension_error",
                }:
                    raise RuntimeError("native_observer_attach_recovery_or_error")
                if record.get("type") in self.sequenced_types:
                    meta = record.get("meta", {})
                    if (
                        not isinstance(meta, dict)
                        or meta.get("activeSessionId") != self.active_session_id
                        or not isinstance(meta.get("cursor"), dict)
                        or meta["cursor"].get("generation") != cursor["generation"]
                        or not isinstance(meta.get("sequence"), int)
                        or isinstance(meta.get("sequence"), bool)
                        or meta["sequence"] < 0
                        or meta["cursor"].get("sequence") != meta["sequence"]
                        or meta.get("protocol") != self.protocol
                        or meta.get("id") != f"{self.active_session_id}:{meta['sequence']}"
                        or not isinstance(meta.get("emittedAt"), str)
                    ):
                        raise RuntimeError("native_observer_attach_sequence_uncertain")
                    live = meta["sequence"] > sequence
            except FINALIZATION_ERRORS as exc:
                reason = type(exc).__name__
                if isinstance(exc, RuntimeError):
                    reason = str(exc)
                self.observe_record(received, reason)
                raise
            if live:
                self.consume(received)
            else:
                self.observe_record(received, reason)

    def open(self, path: Path) -> None:
        if self.state != "created":
            raise RuntimeError("native_observer_already_opened")
        self.check_owner()
        self.socket_path = path
        self.socket = socket.socket(socket.AF_UNIX)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native_observer_deadline")
        self.socket.settimeout(remaining)
        self.socket.connect(str(path))
        self.state = "connected"
        hello = self.receive(self.deadline).record
        if (
            hello.get("type") != "daemon_hello"
            or hello.get("protocol") != self.protocol
            or hello.get("schemaRevision") != 30
            or hello.get("schemaId") != "protocol-7-schema-30-f908f493c9e1"
            or hello.get("appVersion") != "0.9.6"
            or hello.get("runtime", {}).get("buildId")
            != "e260085dd8f742e0def3d871860c9a888b114851"
            or not {"attach_snapshot", "event_sequence", "session_input_admission"}
            <= set(hello.get("serverCapabilities", []))
        ):
            raise RuntimeError("native_observer_protocol_mismatch")
        self.hello = {
            "protocol": hello["protocol"],
            "schema_revision": hello["schemaRevision"],
            "schema_id": hello["schemaId"],
            "app_version": hello["appVersion"],
            "build_id": hello["runtime"]["buildId"],
        }
        pending = []
        listing = self.request({"type": "list"}, pending)
        rows = [
            row
            for row in listing.get("sessions", [])
            if row.get("sessionId") == self.root.external_session_id
        ]
        if len(rows) != 1:
            raise RuntimeError("native_observer_expected_one_owned_session")
        row = rows[0]
        if (
            not isinstance(row.get("sessionFile"), str)
            or Path(row["sessionFile"]).resolve() != self.journal.resolve()
            or row.get("workerPid") != self.root.roles.worker.pid
            or row.get("workerState") != "ready"
            or not isinstance(row.get("activeSessionId"), str)
            or not row["activeSessionId"]
        ):
            raise RuntimeError("native_observer_list_identity_mismatch")
        self.active_session_id = row["activeSessionId"]
        attached = self.request(
            {
                "type": "attach",
                "activeSessionId": self.active_session_id,
                "supportsExtensionUi": False,
                "clientId": self.client_id,
                "capabilities": ["attach_snapshot", "event_sequence"],
            },
            pending,
        )
        self.admit_snapshot(attached, pending)
        self.check_owner()
        self.thread = threading.Thread(target=self.read, name=self.client_id, daemon=True)
        self.thread.start()

    def selected(self, record: dict) -> bool:
        meta = record.get("meta")
        return record.get("activeSessionId") == self.active_session_id or (
            isinstance(meta, dict) and meta.get("activeSessionId") == self.active_session_id
        )

    def observe_record(
        self, received: _NativeReceivedRecord, reason: str, previous: int | None = None
    ) -> None:
        record = received.record
        meta = record.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        cursor = meta.get("cursor")
        cursor = cursor if isinstance(cursor, dict) else {}
        event = record.get("event")
        event = event if isinstance(event, dict) else {}
        actions = event.get("actions")
        actions = actions if isinstance(actions, dict) else {}
        steering = actions.get("steering")
        followups = actions.get("followUps")
        steering_count = steering.count(self.prompt) if isinstance(steering, list) else 0
        followup_count = followups.count(self.prompt) if isinstance(followups, list) else 0
        sequence = meta.get("sequence")
        sequence = (
            sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None
        )
        previous = self.sequence if previous is None else previous
        gap = (
            max(0, sequence - previous - 1) if sequence is not None and previous is not None else 0
        )
        evidence = {
            "previous_sequence": previous,
            "baseline_sequence": self.baseline_sequence,
            "observed_sequence": sequence,
            "envelope_type": record.get("type") if isinstance(record.get("type"), str) else None,
            "event_type": event.get("type") if isinstance(event.get("type"), str) else None,
            "selected_session_id": self.active_session_id,
            "source_session_id": record.get("activeSessionId")
            if isinstance(record.get("activeSessionId"), str)
            else None,
            "meta_session_id": meta.get("activeSessionId")
            if isinstance(meta.get("activeSessionId"), str)
            else None,
            "generation": cursor.get("generation")
            if isinstance(cursor.get("generation"), str)
            else None,
            "cursor_sequence": cursor.get("sequence")
            if isinstance(cursor.get("sequence"), int)
            and not isinstance(cursor.get("sequence"), bool)
            else None,
            "received_at": received.received_at,
            "end_existed": received.end_existed,
            "subscription_gap": gap,
            "subscription_observation": "publication_counter_gap_delivery_not_exhaustive"
            if gap
            else None,
            "reason": reason,
            "owned_prompt_match": bool(steering_count or followup_count),
            "steering_count": steering_count,
            "followup_count": followup_count,
        }
        self.selected_records.append(evidence)
        if self.record_evidence is not None:
            self.record_evidence(evidence)

    def consume(self, received: _NativeReceivedRecord) -> None:
        record = received.record
        if not self.selected(record):
            return
        previous = self.sequence
        reason = "selected_record_advanced"
        try:
            if self.state != "attached":
                raise RuntimeError("native_observer_not_attached")
            self.check_owner()
            if record.get("activeSessionId") != self.active_session_id:
                raise RuntimeError("native_observer_event_metadata_missing_or_mismatch")
            kind = record.get("type")
            if kind not in self.sequenced_types:
                raise RuntimeError("native_observer_unsequenced_selected_record")
            if kind in {"session_replaced", "session_resynced", "session_closed"}:
                raise RuntimeError("native_session_recovery_or_closure")
            if kind == "extension_error":
                raise RuntimeError("native_extension_error")
            meta = record.get("meta", {})
            cursor = meta.get("cursor", {}) if isinstance(meta, dict) else None
            sequence = meta.get("sequence") if isinstance(meta, dict) else None
            if (
                not isinstance(meta, dict)
                or not isinstance(cursor, dict)
                or meta.get("activeSessionId") != self.active_session_id
                or meta.get("protocol") != self.protocol
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or cursor.get("sequence") != sequence
                or meta.get("id") != f"{self.active_session_id}:{sequence}"
                or not isinstance(meta.get("emittedAt"), str)
            ):
                raise RuntimeError("native_observer_event_metadata_missing_or_mismatch")
            if cursor.get("generation") != self.generation:
                raise RuntimeError("native_observer_generation_changed")
            if sequence <= self.sequence:
                raise RuntimeError("native_observer_sequence_continuity_lost")
            self.sequence = sequence
            event = record.get("event", {})
            if kind != "session_event":
                return
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise RuntimeError("native_observer_event_shape_mismatch")
            if event["type"] != "session_action_update":
                return
            actions = event.get("actions", {})
            if not isinstance(actions, dict):
                raise RuntimeError("native_observer_queue_shape_mismatch")
            steering, followups = actions.get("steering"), actions.get("followUps")
            if (
                not isinstance(steering, list)
                or not isinstance(followups, list)
                or not all(isinstance(prompt, str) for prompt in steering + followups)
            ):
                raise RuntimeError("native_observer_queue_shape_mismatch")
            if self.prompt in followups or steering.count(self.prompt) > 1:
                raise RuntimeError("native_observer_queue_prompt_ambiguous")
            if self.prompt not in steering:
                return
            if self.post_started_at is None or received.received_at < self.post_started_at:
                raise RuntimeError("native_observer_prompt_before_post")
            if received.end_existed:
                raise RuntimeError("native_observer_receipt_after_wait_end")
            if self.receipt is None:
                self.receipt = _NativeQueueReceipt(
                    self.root,
                    self.active_session_id,
                    self.prompt,
                    self.generation,
                    sequence,
                    self.post_started_at,
                    received.received_at,
                    received.end_existed,
                )
        except FINALIZATION_ERRORS as exc:
            reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            raise
        finally:
            self.observe_record(received, reason, previous)

    def read(self) -> None:
        try:
            while not self.stop.is_set() and time.monotonic() < self.deadline:
                self.check_owner()
                try:
                    received = self.receive(min(self.deadline, time.monotonic() + 0.25))
                except TimeoutError:
                    continue
                except _NativeReceiveClosed:
                    if self.stop.is_set():
                        return
                    raise
                self.consume(received)
        except FINALIZATION_ERRORS as exc:
            if self.error is None:
                self.error = type(exc).__name__ + ": " + sanitize(str(exc))

    def close(self) -> None:
        if self.socket is not None:
            self.closing_socket = self.socket
        self.stop.set()
        close_error = None
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                if exc.errno not in (errno.ENOTCONN, errno.EBADF):
                    close_error = exc
            finally:
                try:
                    self.socket.close()
                except OSError as exc:
                    close_error = exc
                finally:
                    self.socket = None
        if self.thread is not None:
            self.thread.join(timeout=1)
            if self.thread.is_alive():
                raise RuntimeError("native_observer_reader_survives_close")
        self.state = "closed"
        if close_error:
            raise close_error

    def evidence(self) -> dict:
        return {
            "state": self.state,
            "hello": self.hello,
            "command_receipts": self.command_receipts,
            "prompt": self.prompt,
            "baseline_prompt_absent": self.baseline_prompt_absent,
            "socket": self.socket_path,
            "client_id": self.client_id,
            "active_session_id": self.active_session_id,
            "native_session_id": self.root.external_session_id,
            "worker_pid": self.root.roles.worker.pid,
            "journal": self.journal,
            "binding_incarnation": self.root.binding_incarnation,
            "generation": self.generation,
            "baseline_sequence": self.baseline_sequence,
            "last_sequence": self.sequence,
            "selected_records": self.selected_records,
            "post_started_at": self.post_started_at,
            "receipt": {
                "prompt": self.receipt.prompt,
                "generation": self.receipt.generation,
                "sequence": self.receipt.sequence,
                "received_at": self.receipt.received_at,
                "end_existed": self.receipt.end_existed,
            }
            if self.receipt
            else None,
            "error": self.error,
            "reader_alive": self.thread.is_alive() if self.thread else False,
        }


def sanitize(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[^\s\"\x1b]+", r"\1[REDACTED]", value)
    value = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", value)
    return re.sub(
        r"(?i)((?:access_token|refresh_token|api_key|apiKey|authorization)[\"\s:=]+)[^\s\",}]+",
        r"\1[REDACTED]",
        value,
    )


def write_json(path: Path, value: object, *, immutable: bool = False) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if immutable else os.O_TRUNC)
    with os.fdopen(os.open(path, flags, 0o600), "w") as handle:
        handle.write(sanitize(payload))
    path.chmod(0o400 if immutable else 0o600)


def append_json(path: Path, value: object) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as handle:
        handle.write(sanitize(json.dumps(value, default=str)) + "\n")


def native_tool(entries: list[dict], code: str, literal: str | None) -> dict:
    calls = [
        block
        for entry in entries
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]
    if len(calls) != 1 or calls[0].get("name") != "ipython":
        raise RuntimeError("expected_exactly_one_native_ipython_call")
    actual = calls[0].get("arguments", {}).get("code", "")
    if ast.dump(ast.parse(actual)) != ast.dump(ast.parse(code)):
        raise RuntimeError("native_ipython_program_mismatch")
    results = [
        entry
        for entry in entries
        if entry.get("message", {}).get("role") == "toolResult"
        and entry["message"].get("toolCallId") == calls[0]["id"]
    ]
    if literal is not None:
        lines = [
            line.strip()
            for entry in results
            for block in entry["message"].get("content", [])
            for line in block.get("text", "").splitlines()
        ]
        if len(results) != 1 or literal not in lines or results[0]["message"].get("isError"):
            raise RuntimeError("native_ipython_literal_mismatch")
    return {"call": calls[0], "results": results}


def native_user_text(message: dict) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def _reply_rows(rows: list[dict]) -> tuple[str, ...]:
    return tuple(json.dumps(row, sort_keys=True) for row in rows)


def _reply_ids(rows: list[dict], label: str) -> tuple[str, ...]:
    ids = tuple(row.get("id") for row in rows)
    if any(not isinstance(value, str) or not value.strip() for value in ids) or len(
        set(ids)
    ) != len(ids):
        raise RuntimeError(f"{label}_id_malformed_or_duplicate")
    return ids


def _reply_text(message: dict) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    return "".join(
        text
        for block in content
        if isinstance(block, dict) and block.get("type") != "reasoning"
        for text in [
            block.get("text")
            or block.get("input_text")
            or block.get("output_text")
            or block.get("content")
        ]
        if isinstance(text, str)
    )


def _native_reply_content(message: dict) -> None:
    role = message.get("role")
    if role not in {"user", "assistant", "toolResult"}:
        return
    content = message.get("content")
    if role == "user" and isinstance(content, str):
        return
    if not isinstance(content, list):
        raise RuntimeError("native_message_content_malformed")
    for block in content:
        if not isinstance(block, dict):
            raise RuntimeError("native_message_content_malformed")
        kind = block.get("type")
        if kind == "toolCall":
            if role != "assistant":
                raise RuntimeError("native_tool_carrier_malformed")
            if "thoughtSignature" in block and not isinstance(block["thoughtSignature"], str):
                raise RuntimeError("native_message_content_malformed")
            continue
        if kind == "text":
            if "textSignature" in block and not isinstance(block["textSignature"], str):
                raise RuntimeError("native_message_content_malformed")
            fields = ("text",)
        elif kind == "thinking" and role == "assistant":
            if "thinkingSignature" in block and not isinstance(block["thinkingSignature"], str):
                raise RuntimeError("native_message_content_malformed")
            if "redacted" in block and not isinstance(block["redacted"], bool):
                raise RuntimeError("native_message_content_malformed")
            fields = ("thinking",)
        elif kind == "image" and role in {"user", "toolResult"}:
            fields = ("data", "mimeType")
        else:
            raise RuntimeError("native_message_content_malformed")
        if any(not isinstance(block.get(field), str) for field in fields):
            raise RuntimeError("native_message_content_malformed")


def _public_reply_content(item: dict) -> None:
    if item.get("type") != "message":
        return
    content = item.get("content")
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise RuntimeError("public_message_content_malformed")
    if item.get("role") == "user" and any(
        block.get("type") != "input_text" or not isinstance(block.get("text"), str)
        for block in content
    ):
        raise RuntimeError("public_user_content_malformed")
    if not isinstance(item.get("interrupted", False), bool):
        raise RuntimeError("public_message_interrupted_malformed")


_SEED_DIAGNOSTIC_ENUMS = {
    "schema": ("native_seed_diagnostic_v1",),
    "operation": ("kernel_seed", "unknown"),
    "observation_scope": ("current_rejected_entry", "same_operation_events", "unavailable"),
    "stop_reason": (
        "stop",
        "error",
        "aborted",
        "length",
        "toolUse",
        "missing",
        "malformed",
        "unknown",
    ),
    "diagnostic_origin": (
        "provider_stream_failure",
        "agent_lifecycle_failure",
        "both",
        "absent",
        "malformed",
        "unknown",
    ),
    "failure_kind": (
        "refusal",
        "safety",
        "overloaded",
        "rate_limit",
        "server_error",
        "auth",
        "permission",
        "invalid_request",
        "malformed_response",
        "unknown",
        "unavailable",
    ),
    "http_status": (
        "http_400",
        "http_401",
        "http_403",
        "http_404",
        "http_408",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "http_529",
        "other",
        "absent",
        "malformed",
        "unavailable",
    ),
    "error_code": (
        "ENOENT",
        "EACCES",
        "EPERM",
        "ECONNREFUSED",
        "ECONNRESET",
        "ETIMEDOUT",
        "other",
        "absent",
        "malformed",
        "unavailable",
    ),
    "phase": (
        "launch",
        "kernel_start",
        "runtime_bootstrap",
        "model_setup",
        "provider_stream",
        "cell_execution",
        "agent_lifecycle",
        "unknown",
    ),
    "phase_evidence": (
        "explicit_producer_flag",
        "native_diagnostic_type",
        "tool_status_only",
        "helper_admission_only",
        "unavailable",
    ),
    "kernel_ready": ("observed", "not_observed", "unavailable"),
    "protocol_match": ("match", "mismatch", "unavailable"),
    "bootstrap_done": ("ok", "error", "aborted", "unavailable"),
    "ipython_call": ("observed", "not_observed", "unavailable"),
    "ipython_done": ("ok", "error", "aborted", "starting", "absent", "malformed", "unavailable"),
    "ipython_is_error": ("true", "false", "absent", "malformed", "unavailable"),
}


def _validate_seed_diagnostic(value: dict) -> dict:
    booleans = {"error_present", "kernel_identity_receipt_present", "projection_complete"}
    if (
        type(value) is not dict
        or not all(type(key) is str for key in value)
        or set(value) != set(_SEED_DIAGNOSTIC_ENUMS) | booleans
    ):
        raise ValueError("native_seed_diagnostic_schema")
    for field, allowed in _SEED_DIAGNOSTIC_ENUMS.items():
        item = value[field]
        if type(item) is not str or len(item) > 64 or item not in allowed:
            raise ValueError("native_seed_diagnostic_enum")
    if any(type(value[field]) is not bool for field in booleans):
        raise ValueError("native_seed_diagnostic_boolean")
    return value


def _seed_diagnostic(diagnostics, tools: list[dict], called: bool, stop: str, error: bool) -> dict:
    result = {
        "schema": "native_seed_diagnostic_v1",
        "operation": "unknown",
        "observation_scope": "current_rejected_entry",
        "stop_reason": stop,
        "error_present": error,
        "diagnostic_origin": "absent",
        "failure_kind": "unavailable",
        "http_status": "unavailable",
        "error_code": "unavailable",
        "phase": "unknown",
        "phase_evidence": "unavailable",
        "kernel_ready": "unavailable",
        "protocol_match": "unavailable",
        "bootstrap_done": "unavailable",
        "ipython_call": "observed" if called else "not_observed",
        "ipython_done": "unavailable",
        "ipython_is_error": "unavailable",
        "kernel_identity_receipt_present": False,
        "projection_complete": False,
    }
    known = {"provider_stream_failure", "agent_lifecycle_failure"}
    if diagnostics is not None:
        if type(diagnostics) is not list or len(diagnostics) > 16:
            result["diagnostic_origin"] = "malformed"
        elif diagnostics:
            origins = []
            for diagnostic in diagnostics:
                origin = diagnostic.get("type") if type(diagnostic) is dict else None
                origins.append(
                    "malformed"
                    if type(diagnostic) is not dict or type(origin) is not str
                    else origin
                    if len(origin) <= 64 and origin in known
                    else "unknown"
                )
            result["diagnostic_origin"] = (
                "both" if set(origins) == known else origins[0] if len(origins) == 1 else "unknown"
            )
            if len(diagnostics) == 1 and origins[0] in known:
                diagnostic = diagnostics[0]
                result["phase"] = (
                    "provider_stream"
                    if origins[0] == "provider_stream_failure"
                    else "agent_lifecycle"
                )
                result["phase_evidence"] = "native_diagnostic_type"
                details = diagnostic.get("details")
                if type(details) is dict:
                    kind = details.get("kind")
                    result["failure_kind"] = (
                        kind
                        if type(kind) is str
                        and len(kind) <= 64
                        and kind in _SEED_DIAGNOSTIC_ENUMS["failure_kind"][:-1]
                        else "unknown"
                    )
                    status = details.get("status")
                    result["http_status"] = (
                        "absent"
                        if status is None
                        else "malformed"
                        if type(status) is not int
                        else f"http_{status}"
                        if status in (400, 401, 403, 404, 408, 429, 500, 502, 503, 504, 529)
                        else "other"
                    )
                elif details is not None:
                    result["failure_kind"] = "unknown"
                    result["http_status"] = "malformed"
                error_info = diagnostic.get("error")
                if type(error_info) is dict:
                    code = error_info.get("code")
                    result["error_code"] = (
                        "absent"
                        if code is None
                        else "malformed"
                        if type(code) not in (str, int)
                        else code
                        if type(code) is str
                        and code
                        in ("ENOENT", "EACCES", "EPERM", "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT")
                        else "other"
                    )
                elif error_info is not None:
                    result["error_code"] = "malformed"
                else:
                    result["error_code"] = "absent"
    if called:
        result["observation_scope"] = "same_operation_events"
        if result["phase_evidence"] == "unavailable":
            result["phase_evidence"] = "tool_status_only"
        if not tools:
            result["ipython_done"] = "absent"
            result["ipython_is_error"] = "absent"
        elif len(tools) == 1 and type(tools[0]) is dict:
            details = tools[0].get("details")
            status = details.get("status") if type(details) is dict else None
            result["ipython_done"] = (
                "malformed"
                if details is not None and type(details) is not dict
                else "absent"
                if status is None
                else status
                if type(status) is str and status in ("ok", "error", "aborted", "starting")
                else "malformed"
            )
            is_error = tools[0].get("isError")
            result["ipython_is_error"] = (
                "absent"
                if is_error is None
                else "malformed"
                if type(is_error) is not bool
                else "true"
                if is_error
                else "false"
            )
        else:
            result["ipython_done"] = "malformed"
            result["ipython_is_error"] = "malformed"
    return _validate_seed_diagnostic(result)


@dataclass
class _RetirementAttempt:
    allocation_id: str
    session_id: str | None
    schema: str = "native_retirement_attempt_v1"
    root_identity: tuple[int, int] | None = None
    parent_identity: tuple[int, int] | None = None
    external_root: bool | None = None
    delete_step: int | None = None
    http_status: int | None = None
    phase: str = "admission"
    branch: str = "unobserved"
    held_root_matches: bool | None = None
    held_parent_matches: bool | None = None
    name_state: str = "unobserved"
    event_flags: int | None = None


@dataclass(frozen=True)
class _NativeFailureObservation:
    version: int
    session_id: str | None
    native_session_id: str | None
    session_matches_operation: bool
    baseline_count: int
    public_baseline_count: int
    native_user_position: int
    native_final_position: int
    native_user_id: str | None
    native_reply_id: str | None
    ids_omitted: bool
    stop_reason: str
    error_present: bool
    error_class: str
    classification_input_truncated: bool
    diagnostic: dict


class _NativeReplyFailure(RuntimeError):
    def __init__(
        self,
        operation: _ReplyOperation,
        entries: list[dict],
        user: int,
        final: int,
        calls: dict,
        results: dict,
    ):
        super().__init__("native_assistant_not_successful")
        message = entries[final]["message"]
        reason = message.get("stopReason")
        stop = (
            "missing"
            if reason is None
            else "malformed"
            if not isinstance(reason, str)
            else reason
            if reason in {"stop", "error", "aborted", "length", "toolUse"}
            else "unknown"
        )
        error = message.get("errorMessage")
        absent = error is None or (isinstance(error, str) and error == "")
        category = "absent" if absent else "unknown"
        truncated = isinstance(error, str) and len(error) > 4096
        if not absent and not isinstance(error, str):
            category = "malformed"
        elif isinstance(error, str) and error:
            bounded = error[:4096].lower()
            for label, needles in (
                ("authentication", ("unauthorized", "authentication failed", "invalid api key")),
                ("rate_limit", ("rate limit", "too many requests")),
                ("timeout", ("timed out", "timeout")),
                ("transport", ("connection refused", "connection reset")),
            ):
                if any(needle in bounded for needle in needles):
                    category = label
                    break
        values = (
            operation.session_id,
            operation.external_id,
            entries[user]["id"],
            entries[final]["id"],
        )
        ids = tuple(
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value)
            else None
            for value in values
        )
        self.observation = _NativeFailureObservation(
            1,
            ids[0],
            ids[1],
            True,
            len(operation.native_baseline),
            len(operation.public_baseline),
            user,
            final,
            ids[2],
            ids[3],
            any(value is None for value in ids),
            stop,
            not absent,
            category,
            truncated,
            _seed_diagnostic(
                message.get("diagnostics"),
                [
                    results[key]["message"]
                    for key, (index, _, call) in calls.items()
                    if user < index < final and call["name"] == "ipython" and key in results
                ],
                any(
                    user < index < final and call["name"] == "ipython"
                    for index, _, call in calls.values()
                ),
                stop,
                not absent,
            ),
        )


class _PublicPromptError(RuntimeError):
    def __init__(self, reason: str, predicates: dict):
        super().__init__(reason)
        self.predicates = {**predicates, "reason": reason}


def _reply_verdict(
    operation: _ReplyOperation, entries: list[dict], items: list[dict] | None
) -> dict:
    native_ids = _reply_ids(entries, "native_entry")
    if _reply_rows(entries[: len(operation.native_baseline)]) != operation.native_baseline:
        raise RuntimeError("native_baseline_changed")
    headers = tuple(entry["id"] for entry in entries if entry.get("type") == "session")
    if headers != operation.session_headers or headers != (operation.external_id,):
        raise RuntimeError("native_journal_identity_changed")
    parent = None
    for entry in entries:
        if entry.get("type") == "session":
            if entry is not entries[0]:
                raise RuntimeError("native_branch_ambiguous")
            continue
        if "parentId" not in entry or entry["parentId"] != parent:
            raise RuntimeError("native_branch_ambiguous")
        parent = entry["id"]
        _native_reply_content(entry.get("message", {}))
    before = len(operation.native_baseline)
    if any(
        entry.get("message", {}).get("role") == "user"
        and native_user_text(entry["message"]) == operation.prompt
        for entry in entries[:before]
    ):
        raise RuntimeError("native_owned_prompt_in_baseline")
    users = [
        i
        for i, entry in enumerate(entries[before:], before)
        if entry.get("message", {}).get("role") == "user"
        and native_user_text(entry["message"]) == operation.prompt
    ]
    if len(users) > 1:
        raise RuntimeError("fresh_native_user_or_assistant_ambiguous")
    turn = entries[users[0] :] if users else []
    if sum(entry.get("message", {}).get("role") == "user" for entry in turn) > 1:
        raise RuntimeError("fresh_native_user_or_assistant_ambiguous")
    calls = {}
    results = {}
    carriers = {}
    finals = []
    projections = []
    for index, entry in enumerate(entries):
        message = entry.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, (list, str)):
            raise RuntimeError("native_message_content_malformed")
        blocks = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "toolCall"
        ]
        role = message.get("role")
        if blocks:
            if role != "assistant" or message.get("stopReason", "toolUse") != "toolUse":
                raise RuntimeError("native_tool_carrier_malformed")
            carriers[entry["id"]] = blocks
            for call in blocks:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id.strip() or call_id in calls:
                    raise RuntimeError("native_tool_call_id_malformed_or_duplicate")
                if (
                    not isinstance(call.get("name"), str)
                    or not call["name"].strip()
                    or not isinstance(call.get("arguments"), dict)
                ):
                    raise RuntimeError("native_tool_call_malformed")
                calls[call_id] = (index, entry["id"], call)
                projections.append(("function_call", entry["id"], call))
        elif role == "assistant":
            if message.get("stopReason") == "toolUse":
                raise RuntimeError("native_tool_carrier_malformed")
            if users and index > users[0]:
                finals.append(entry)
                error = message.get("errorMessage")
                absent = error is None or (isinstance(error, str) and error == "")
                if message.get("stopReason") != "stop" or not absent:
                    raise _NativeReplyFailure(operation, entries, users[0], index, calls, results)
                if assistant_text(message).strip() != operation.literal:
                    raise RuntimeError("native_assistant_literal_mismatch")
        if role == "assistant" and _reply_text(message):
            projections.append(("message", entry["id"], message))
        if role == "toolResult":
            call_id = message.get("toolCallId")
            if not isinstance(call_id, str) or call_id not in calls:
                raise RuntimeError("native_tool_result_unmatched")
            if call_id in results:
                raise RuntimeError("native_tool_result_ambiguous")
            if message.get("toolName") != calls[call_id][2]["name"]:
                raise RuntimeError("native_tool_result_name_mismatch")
            if users and index > users[0] and calls[call_id][0] < users[0]:
                raise RuntimeError("native_tool_result_from_previous_turn")
            results[call_id] = entry
            projections.append(("function_call_output", calls[call_id][1], message))
    if len(finals) > 1:
        raise RuntimeError("fresh_native_user_or_assistant_ambiguous")
    current_calls = {
        call_id: value for call_id, value in calls.items() if users and value[0] > users[0]
    }
    if finals:
        final_index = native_ids.index(finals[0]["id"])
        if any(
            index > final_index
            or (call_id in results and native_ids.index(results[call_id]["id"]) > final_index)
            for call_id, (index, _, _) in current_calls.items()
        ):
            raise RuntimeError("native_tool_after_final")
        if any(call_id not in results for call_id in calls):
            raise RuntimeError("native_final_before_tool_result")
    tool = None
    if operation.code is not None:
        if len(current_calls) > 1:
            raise RuntimeError("native_ipython_witness_ambiguous")
        if current_calls:
            paired = all(call_id in results for call_id in current_calls)
            tool = native_tool(turn, operation.code, operation.literal if paired else None)
    native_complete = bool(
        users
        and len(finals) == 1
        and all(call_id in results for call_id in calls)
        and (operation.code is None or (tool is not None and len(tool["results"]) == 1))
    )
    predicates = {
        "reason": "public_projection_pending" if native_complete else "native_journal_pending",
        "user_count": len(users),
        "user_ids": [entries[index]["id"] for index in users],
        "assistant_count": len(finals),
        "assistant_ids": [entry["id"] for entry in finals],
        "carrier_count": sum(entry["id"] in carriers for entry in turn),
        "call_count": len(current_calls),
        "call_ids": list(current_calls),
        "result_count": sum(call_id in results for call_id in current_calls),
        "result_ids": [results[key]["id"] for key in current_calls if key in results],
        "public_user_count": 0,
        "public_user_ids": [],
        "public_user_positions": [],
        "public_baseline_count": len(operation.public_baseline),
        "public_final_position": None,
        "public_user_fresh": False,
        "public_user_before_final": False,
        "public_user_exact_prompt": False,
    }
    verdict = {"complete": False, "predicates": predicates}
    if items is None:
        return verdict
    _reply_ids(items, "public_item")
    if _reply_rows(items[: len(operation.public_baseline)]) != operation.public_baseline:
        raise RuntimeError("public_baseline_changed")
    for item in items:
        _public_reply_content(item)
    public_users = [
        index
        for index, item in enumerate(items)
        if item.get("type") == "message" and item.get("role") == "user"
    ]
    matches = [
        index
        for index in public_users
        if "".join(block["text"] for block in items[index]["content"]) == operation.prompt
    ]
    fresh_matches = [index for index in matches if index >= len(operation.public_baseline)]
    predicates.update(
        public_user_count=len(fresh_matches),
        public_user_ids=[items[index]["id"] for index in fresh_matches],
        public_user_positions=fresh_matches,
        public_user_fresh=len(fresh_matches) == 1,
        public_user_exact_prompt=len(fresh_matches) == 1,
    )
    if any(index < len(operation.public_baseline) for index in matches):
        raise _PublicPromptError("public_owned_prompt_in_baseline", predicates)
    if len(fresh_matches) > 1:
        raise _PublicPromptError("public_owned_prompt_ambiguous", predicates)
    if native_complete and not fresh_matches:
        predicates["reason"] = "public_prompt_pending"
    public = [
        item
        for item in items
        if item.get("type") in {"function_call", "function_call_output"}
        or (item.get("type") == "message" and item.get("role") == "assistant")
    ]
    public_calls = {}
    public_results = set()
    public_assistants = set()
    for item in public:
        response_id = item.get("response_id")
        if not isinstance(response_id, str) or not response_id.strip():
            raise RuntimeError("public_response_id_missing")
        if item.get("status") != "completed":
            raise RuntimeError("public_projection_not_completed")
        kind = item["type"]
        if kind == "function_call":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip() or call_id in public_calls:
                raise RuntimeError("public_call_id_malformed_or_duplicate")
            try:
                arguments = json.loads(item["arguments"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("public_call_arguments_malformed") from exc
            if not isinstance(arguments, dict) or not isinstance(item.get("name"), str):
                raise RuntimeError("public_call_malformed")
            public_calls[call_id] = item
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id not in public_calls:
                raise RuntimeError("public_result_unmatched_or_before_call")
            if call_id in public_results:
                raise RuntimeError("public_result_duplicate")
            if response_id != public_calls[call_id]["response_id"]:
                raise RuntimeError("public_result_response_mismatch")
            public_results.add(call_id)
        else:
            if response_id in public_assistants:
                raise RuntimeError("public_assistant_response_duplicate")
            public_assistants.add(response_id)
    groups = {}
    response_owners = {}
    final_item = None
    for (kind, entry_id, native), item in zip(projections, public, strict=False):
        if item["type"] != kind:
            raise RuntimeError("public_projection_order_mismatch")
        response_id = item["response_id"]
        if (
            groups.setdefault(entry_id, response_id) != response_id
            or response_owners.setdefault(response_id, entry_id) != entry_id
        ):
            raise RuntimeError("public_projection_response_mismatch")
        if kind == "function_call":
            if (
                item["call_id"] != native["id"]
                or item.get("name") != native["name"]
                or json.loads(item["arguments"]) != native["arguments"]
            ):
                raise RuntimeError("public_native_call_mismatch")
        elif kind == "function_call_output":
            if item.get("call_id") != native["toolCallId"] or item.get("output") != _reply_text(
                native
            ):
                raise RuntimeError("public_native_result_mismatch")
        else:
            if _reply_text(item) != _reply_text(native):
                raise RuntimeError("public_native_assistant_mismatch")
            if finals and entry_id == finals[0]["id"]:
                if item.get("interrupted", False) is True:
                    raise RuntimeError("public_final_interrupted")
                final_item = item
    if final_item is not None:
        final_position = items.index(final_item)
        predicates["public_final_position"] = final_position
        if fresh_matches:
            user_position = fresh_matches[0]
            predicates["public_user_before_final"] = user_position < final_position
            if user_position >= final_position:
                raise _PublicPromptError("public_owned_prompt_after_final", predicates)
            if any(user_position < index < final_position for index in public_users):
                raise _PublicPromptError("public_owned_turn_interrupted", predicates)
    if native_complete and len(public) > len(projections):
        raise RuntimeError("public_projection_after_final")
    if not native_complete or len(public) != len(projections) or final_item is None:
        return verdict
    if final_item["id"] in operation.public_ids:
        raise RuntimeError("public_final_not_fresh")
    if not fresh_matches:
        return verdict
    predicates["reason"] = "native_public_reply_complete"
    return {
        "complete": True,
        "predicates": predicates,
        "prompt": operation.prompt,
        "literal": operation.literal,
        "fresh_items": items[len(operation.public_baseline) :],
        "native": entries[before:],
        "tool": tool,
        "journal": operation.journal_path,
        "native_user_id": entries[users[0]]["id"],
        "public_user_id": items[fresh_matches[0]]["id"],
        "native_reply_id": finals[0]["id"],
        "public_reply_id": final_item["id"],
        "response_id": final_item["response_id"],
    }


def process_exe(process: psutil.Process) -> str | None:
    try:
        return process.exe()
    except (psutil.Error, OSError, SystemError):
        return None


def identity_alive(identity: ProcessIdentity) -> bool:
    try:
        process = psutil.Process(identity.pid)
        return (
            process.create_time() == identity.started and process.status() != psutil.STATUS_ZOMBIE
        )
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def user_prime_baseline() -> list[dict]:
    records = []
    for process in psutil.process_iter():
        try:
            if process.uids().real != os.getuid() or process.pid == os.getpid():
                continue
            name = process.name()
            if not any(part in name.lower() for part in ("prime", "python", "node", "bun")):
                continue
            argv = process.cmdline()
            if not any(
                "prime-agent" in arg
                or "rlm.repl" in arg
                or "omnigent_pi_native_extension.js" in arg
                for arg in argv
            ):
                continue
            if process.status() != psutil.STATUS_ZOMBIE:
                records.append(
                    {"pid": process.pid, "started": process.create_time(), "argv": argv}
                )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return records


@dataclass
class _CredentialTarget:
    path: Path
    directories: dict[Path, tuple[int, int]]
    aliases: dict[Path, tuple[int, int]]
    copy_absence_required: bool = False


@dataclass(frozen=True)
class _RetiredOwnedTree:
    root: Path
    root_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    session_id: str
    delete_step: int
    status: int
    event_flags: int
    kind: Literal["delete_event", "maintenance_retired"] = "delete_event"
    status_line: str | None = None
    epoch_sha256: str | None = None
    status_response: dict | None = None


_ACCESS_LINE = re.compile(
    r' uvicorn\.access +\S+ +\| \S+ - "(?P<request>[A-Z]+ \S+) HTTP/[0-9.]+" (?P<status>[0-9]{3}) '
)


def _open_witnessed_directory(target: _CredentialTarget, directory: Path) -> int | None:
    for alias, identity in target.aliases.items():
        metadata = alias.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity or os.readlink(alias) != "private/tmp":
            raise RuntimeError("owned_credential_temp_anchor_replaced")
    witnesses = target.directories
    ancestors = (*reversed(directory.parents), directory)
    descriptor = None
    try:
        for ancestor in ancestors:
            try:
                opened = os.open(
                    str(ancestor) if descriptor is None else ancestor.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if any(item.is_relative_to(ancestor) for item in witnesses):
                    raise RuntimeError("owned_credential_directory_missing") from None
                return None
            try:
                metadata = os.fstat(opened)
                identity = (metadata.st_dev, metadata.st_ino)
                if ancestor in witnesses and witnesses[ancestor] != identity:
                    raise RuntimeError("owned_credential_directory_replaced")
                witnesses[ancestor] = identity
            except BaseException:
                os.close(opened)
                raise
            if descriptor is not None:
                os.close(descriptor)
            descriptor = opened
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True)
class _ExternalSettlement:
    allocation_id: str
    status: Literal["no_fixture", "settled_fixture", "failed_fixture"]
    evidence: tuple[str, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RemovedRuntime:
    allocation_id: str
    root: Path
    root_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    event_flags: int
    descriptor_links: int


_CLI_ALIAS = Path("data/logs/cli/latest-cli.log")


@dataclass(frozen=True)
class _LeafIdentity:
    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int
    ctime_ns: int


@dataclass(frozen=True)
class _DiagnosticsAlias:
    allocation_id: str
    relative_path: Path
    parent_identity: tuple[int, int]
    identity: _LeafIdentity


def _leaf_identity(metadata: os.stat_result) -> _LeafIdentity:
    return _LeafIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_ctime_ns,
    )


@dataclass
class _TraversalBudget:
    deadline: float
    nodes: int = 0
    bytes: int = 0

    def visit(self, depth: int, size: int = 0) -> None:
        self.nodes += 1
        self.bytes += size
        if (
            depth > 32
            or self.nodes > 10000
            or self.bytes > 64 * 1024 * 1024
            or time.monotonic() > self.deadline
        ):
            raise RuntimeError("runtime_traversal_limit")


class _RuntimeOwner:
    def __init__(self, evidence: Path):
        self.evidence = evidence
        self.allocation_id = uuid.uuid4().hex
        self.root: Path | None = None
        self.target: _CredentialTarget | None = None
        self.root_fd: int | None = None
        self.parent_fd: int | None = None
        self.queue = None
        self.removed: _RemovedRuntime | None = None
        self.errors: list[str] = []
        self.closed = False
        self.event_flags = 0
        self.diagnostics_alias: _DiagnosticsAlias | None = None
        self.rejection_recorded = False

    @classmethod
    def allocate(cls, request: Request, evidence: Path) -> _RuntimeOwner:
        owner = cls(evidence)
        try:
            if not request.cooperative_cleanup:
                raise RuntimeError("runtime_cooperative_environment_not_admitted")
            if sys.platform != "darwin" or os.uname().machine != "arm64":
                raise RuntimeError("runtime_removal_platform_unqualified")
            lexical = Path(tempfile.mkdtemp(prefix="pw-", dir="/tmp"))
            owner.root = lexical
            canonical, aliases = _OwnedRun._credential_path(lexical / "allocation")
            owner.root = canonical.parent
            source = request.auth_source.resolve()
            retained = evidence.resolve()
            if (
                source.is_relative_to(owner.root)
                or retained.is_relative_to(owner.root)
                or owner.root.is_relative_to(retained)
            ):
                raise RuntimeError("runtime_allocation_overlaps_preserved_path")
            owner.target = _CredentialTarget(canonical, {}, aliases)
            owner.parent_fd = _open_witnessed_directory(owner.target, owner.root.parent)
            owner.root_fd = _open_witnessed_directory(owner.target, owner.root)
            metadata = os.fstat(owner.root_fd)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise RuntimeError("runtime_allocation_not_private")
            owner.queue = select.kqueue()
            owner.queue.control(
                [
                    select.kevent(
                        owner.root_fd,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=select.KQ_NOTE_DELETE
                        | select.KQ_NOTE_RENAME
                        | select.KQ_NOTE_REVOKE
                        | select.KQ_NOTE_LINK,
                    )
                ],
                0,
                0,
            )
            if owner._events():
                raise RuntimeError("runtime_event_before_admission")
            owner.check_live()
            return owner
        except BaseException as exc:
            owner.errors.append(type(exc).__name__ + ": " + sanitize(str(exc)))
            try:
                owner.close()
            finally:
                write_json(
                    evidence / "runtime-allocation-failed.json",
                    {
                        "root": owner.root,
                        "errors": owner.errors,
                        "retained": True,
                        "allocation_id": owner.allocation_id,
                    },
                    immutable=True,
                )
            raise

    def _events(self) -> int:
        flags = 0
        for event in self.queue.control([], 1, 0):
            if (
                event.ident != self.root_fd
                or event.filter != select.KQ_FILTER_VNODE
                or event.flags & ~(select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR)
                or event.fflags & (select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
                or event.fflags
                & ~(
                    select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_LINK
                    | select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_ATTRIB
                )
                or event.data != 0
            ):
                raise RuntimeError(f"runtime_deletion_event_invalid {event!r}")
            flags |= event.fflags
        self.event_flags |= flags
        return flags

    def check_live(self) -> None:
        if self.closed or self.removed is not None:
            raise RuntimeError("runtime_not_live")
        for fd, path in ((self.parent_fd, self.root.parent), (self.root_fd, self.root)):
            metadata = os.fstat(fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino) != self.target.directories[path]
            ):
                raise RuntimeError("runtime_pin_changed")
        checked = _open_witnessed_directory(self.target, self.root)
        if checked is None:
            raise RuntimeError("runtime_root_missing")
        os.close(checked)
        if self._events() & select.KQ_NOTE_DELETE:
            raise RuntimeError("runtime_deleted_before_removal")

    def check_removed(self) -> None:
        if self.removed is None:
            raise RuntimeError("runtime_removal_unproved")
        checked = _open_witnessed_directory(self.target, self.root.parent)
        if checked is None:
            raise RuntimeError("runtime_parent_missing")
        try:
            try:
                os.stat(self.root.name, dir_fd=checked, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise RuntimeError("runtime_root_recreated")
        finally:
            os.close(checked)

    def _check_directory(self, descriptor: int, directory: Path) -> None:
        self.check_live()
        checked = _open_witnessed_directory(self.target, directory)
        if checked is None:
            raise RuntimeError("runtime_descendant_missing")
        try:
            current, held = os.fstat(checked), os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise RuntimeError("runtime_descendant_replaced")
        finally:
            os.close(checked)

    def _rejection(
        self,
        phase: str,
        reason: str,
        path: Path | None = None,
        metadata: os.stat_result | None = None,
    ) -> None:
        self.errors.append(reason)
        if self.rejection_recorded:
            return
        self.rejection_recorded = True
        mode = metadata.st_mode if metadata is not None else 0
        leaf_type = next(
            (
                label
                for check, label in (
                    (stat.S_ISLNK, "symlink"),
                    (stat.S_ISREG, "regular"),
                    (stat.S_ISDIR, "directory"),
                    (stat.S_ISFIFO, "fifo"),
                    (stat.S_ISSOCK, "socket"),
                )
                if check(mode)
            ),
            "unknown",
        )
        write_json(
            self.evidence / "runtime-traversal-rejection.json",
            {
                "version": 1,
                "allocation_id": self.allocation_id,
                "phase": phase,
                "reason": reason,
                "relative_path": str(_CLI_ALIAS)
                if path == self.root / _CLI_ALIAS
                else "[redacted]",
                "leaf_type": leaf_type,
                "identity": asdict(_leaf_identity(metadata)) if metadata is not None else None,
                "parent_identity_matches": True if metadata is not None else None,
                "action": "reject",
                "target_followed": False,
            },
            immutable=True,
        )

    def _alias(self, descriptor: int, path: Path, metadata: os.stat_result, phase: str) -> Path:
        self._check_directory(descriptor, path.parent)
        parent = os.fstat(descriptor)
        witness = _DiagnosticsAlias(
            self.allocation_id,
            path.relative_to(self.root),
            (parent.st_dev, parent.st_ino),
            _leaf_identity(metadata),
        )
        if (
            witness.relative_path != _CLI_ALIAS
            or not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or metadata.st_dev != self.target.directories[self.root][0]
        ):
            raise RuntimeError("runtime_diagnostics_alias_invalid")
        if phase == "capture":
            if self.diagnostics_alias is not None and self.diagnostics_alias != witness:
                raise RuntimeError("runtime_diagnostics_alias_replaced")
        elif self.diagnostics_alias != witness:
            raise RuntimeError("runtime_diagnostics_alias_unwitnessed_or_replaced")
        self._check_directory(descriptor, path.parent)
        current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        if _leaf_identity(current) != witness.identity:
            raise RuntimeError("runtime_diagnostics_alias_replaced")
        receipt = self.evidence / f"runtime-cli-alias-{phase}.json"
        if phase == "remove":
            os.unlink(path.name, dir_fd=descriptor)
        write_json(
            receipt,
            {
                **asdict(witness),
                "version": 1,
                "phase": phase,
                "leaf_type": "symlink",
                "parent_identity_matches": True,
                "action": "skip_payload" if phase == "capture" else "unlink_alias",
                "target_followed": False,
            },
            immutable=True,
        )
        if phase == "capture":
            self.diagnostics_alias = witness
        return receipt

    def _remove_contents(self, descriptor: int, directory: Path, budget: _TraversalBudget) -> None:
        budget.visit(len(directory.relative_to(self.root).parts))
        with os.scandir(descriptor) as children:
            names = []
            for entry in children:
                budget.visit(len(directory.relative_to(self.root).parts) + 1)
                names.append(entry.name)
        for name in names:
            self._check_directory(descriptor, directory)
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                metadata.st_uid != os.getuid()
                or metadata.st_dev != self.target.directories[self.root][0]
            ):
                raise RuntimeError("runtime_descendant_not_owned")
            identity = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))
            if directory / name == self.root / _CLI_ALIAS:
                self._alias(descriptor, directory / name, metadata, "remove")
            elif stat.S_ISDIR(metadata.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                try:
                    held = os.fstat(child)
                    if (held.st_dev, held.st_ino, stat.S_IFMT(held.st_mode)) != identity:
                        raise RuntimeError("runtime_descendant_replaced")
                    self.target.directories.setdefault(directory / name, identity[:2])
                    self._check_directory(child, directory / name)
                    self._remove_contents(child, directory / name, budget)
                    self._check_directory(descriptor, directory)
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != identity:
                        raise RuntimeError("runtime_descendant_replaced")
                    os.rmdir(name, dir_fd=descriptor)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                self._check_directory(descriptor, directory)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _leaf_identity(current) != _leaf_identity(metadata):
                    raise RuntimeError("runtime_descendant_replaced")
                os.unlink(name, dir_fd=descriptor)
            else:
                self._rejection("remove", "runtime_unsafe_descendant", directory / name, metadata)
                raise RuntimeError("runtime_unsafe_descendant")

    def finish(self, settlement: _ExternalSettlement, captures: tuple[Path, ...]) -> None:
        try:
            if self.removed is not None:
                self.check_removed()
                if self.errors:
                    raise RuntimeError("runtime_prior_finalization_failed")
                return
            if self.errors:
                raise RuntimeError("runtime_prior_finalization_failed")
            if (
                settlement.allocation_id != self.allocation_id
                or settlement.status not in ("no_fixture", "settled_fixture")
                or settlement.errors
                or (settlement.status == "settled_fixture" and not settlement.evidence)
            ):
                raise RuntimeError("runtime_external_settlement_unproved")
            if any(
                Path(item).is_absolute() or ".." in Path(item).parts
                for item in settlement.evidence
            ):
                raise RuntimeError("runtime_settlement_evidence_escape")
            for path in (*captures, *(self.evidence / item for item in settlement.evidence)):
                if (
                    not path.is_relative_to(self.evidence)
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    raise RuntimeError("runtime_capture_unproved")
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            self.check_live()
            if self.diagnostics_alias is not None:
                alias = self.root / _CLI_ALIAS
                parent = _open_witnessed_directory(self.target, alias.parent)
                if parent is None:
                    raise RuntimeError("runtime_diagnostics_alias_parent_missing")
                try:
                    metadata = os.stat(alias.name, dir_fd=parent, follow_symlinks=False)
                    held = os.fstat(parent)
                    if self.diagnostics_alias != _DiagnosticsAlias(
                        self.allocation_id,
                        _CLI_ALIAS,
                        (held.st_dev, held.st_ino),
                        _leaf_identity(metadata),
                    ):
                        raise RuntimeError("runtime_diagnostics_alias_replaced")
                finally:
                    os.close(parent)
            self._remove_contents(self.root_fd, self.root, _TraversalBudget(time.monotonic() + 30))
            self.check_live()
            os.rmdir(self.root.name, dir_fd=self.parent_fd)
            self._events()
            held = os.fstat(self.root_fd)
            if (
                not self.event_flags & select.KQ_NOTE_DELETE
                or not stat.S_ISDIR(held.st_mode)
                or (held.st_dev, held.st_ino) != self.target.directories[self.root]
            ):
                raise RuntimeError("runtime_original_removal_unproved")
            self.removed = _RemovedRuntime(
                self.allocation_id,
                self.root,
                self.target.directories[self.root],
                self.target.directories[self.root.parent],
                self.event_flags,
                held.st_nlink,
            )
            self.check_removed()
            write_json(
                self.evidence / "runtime-removal.json",
                {
                    **asdict(self.removed),
                    "proof": "original_vnode_namespace_removal",
                    "precondition": "cooperative_namespace_and_settled_owned_writers",
                    "hostile_final_syscall": "BLOCKED",
                },
                immutable=True,
            )
        except BaseException as exc:
            self._rejection("remove", "runtime_removal_failed")
            self.errors.append(type(exc).__name__ + ": " + sanitize(str(exc)))
            raise

    def close(self) -> None:
        if self.closed:
            if self.errors:
                raise RuntimeError("runtime_owner_failed")
            return
        self.closed = True
        for name in ("queue", "root_fd", "parent_fd"):
            handle = getattr(self, name)
            if handle is not None:
                try:
                    handle.close() if name == "queue" else os.close(handle)
                except FINALIZATION_ERRORS as exc:
                    self.errors.append(
                        name + ": " + type(exc).__name__ + ": " + sanitize(str(exc))
                    )
        if self.errors:
            raise RuntimeError("runtime_owner_failed: " + "; ".join(self.errors))

    def __enter__(self) -> _RuntimeOwner:
        try:
            self.check_live()
            return self
        except BaseException as exc:
            self.errors.append(type(exc).__name__ + ": " + sanitize(str(exc)))
            try:
                self.close()
            finally:
                write_json(
                    self.evidence / "runtime-enter-failed.json",
                    {
                        "root": self.root,
                        "allocation_id": self.allocation_id,
                        "errors": self.errors,
                        "retained": True,
                    },
                    immutable=True,
                )
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.removed is None:
                self.errors.append("runtime_retained_unsettled")
            else:
                self.check_removed()
                receipt = json.loads((self.evidence / "runtime-removal.json").read_text())
                expected = json.loads(json.dumps(asdict(self.removed), default=str))
                if any(receipt.get(key) != value for key, value in expected.items()):
                    raise RuntimeError("runtime_removal_receipt_changed")
        except FINALIZATION_ERRORS as error:
            self.errors.append(type(error).__name__ + ": " + sanitize(str(error)))
        finally:
            try:
                self.close()
            finally:
                write_json(
                    self.evidence / "runtime-owner.json",
                    {
                        "allocation_id": self.allocation_id,
                        "root": self.root,
                        "removed": asdict(self.removed) if self.removed else None,
                        "errors": self.errors,
                        "closed": self.closed,
                    },
                    immutable=True,
                )


class _PtyDrain:
    """Reads an attached client's pty like a real terminal so the client never blocks on it."""

    def __init__(self, fd: int, sink: Callable[[str], None]):
        self.fd = fd
        self.sink = sink
        self.eof = False
        self.error: str | None = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while not self.stopping.is_set():
                if not select.select([self.fd], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                self.sink(decoder.decode(chunk, final=not chunk))
                if not chunk:
                    self.eof = True
                    return
        except FINALIZATION_ERRORS as exc:
            self.error = type(exc).__name__ + ": " + sanitize(str(exc))

    def wait_eof(self, timeout: float) -> bool:
        self.thread.join(timeout)
        if self.error:
            raise RuntimeError("attachment_drain_failed: " + self.error)
        return self.eof

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join()


class _OwnedRun:
    def __init__(
        self, request: Request, profile: ClockProfile, kind: str, runtime_owner: _RuntimeOwner
    ):
        self.request = request
        self.profile = profile
        self.kind = kind
        self.runtime_owner = runtime_owner
        self.evidence = runtime_owner.evidence
        self.runtime = runtime_owner.root
        self.workspace = self.runtime / "workspace"
        self.prime = self.runtime / "prime"
        self.url = f"http://127.0.0.1:{unused_port()}"
        self.session_id: str | None = None
        self.external_id: str | None = None
        self.server: subprocess.Popen | None = None
        self.server_thread: threading.Thread | None = None
        self.server_capture_error: str | None = None
        self.foreground_host: subprocess.Popen | None = None
        self.foreground_host_identity: ProcessIdentity | None = None
        self.host_log_handle: TextIO | None = None
        self.terminal: pexpect.spawn | None = None
        self.attachment_drain: _PtyDrain | None = None
        self.attachment_identity: ProcessIdentity | None = None
        self.terminal_log: Path | None = None
        self.owners: dict[tuple[int, float], dict] = {}
        self.bridge_roots: set[Path] = set()
        self.credential_copies: set[Path] = set()
        self.credential_targets: dict[Path, _CredentialTarget] = {}
        self._retired_trees: dict[Path, _RetiredOwnedTree] = {}
        self._retained_roots: dict[Path, _RetirementAttempt] = {}
        self._root_admissions: dict[Path, tuple[str, str]] = {}
        self.terminal_sockets: set[Path] = set()
        self.census_errors: set[str] = set()
        self.unowned_unreadable: dict[tuple[int, float], dict] = {}
        self.observations: list[Observation] = []
        self.quiet = False
        self.contaminated = False
        self.native_observer: _NativeQueueObserver | None = None
        self.native_observer_close_error: str | None = None
        self.cleanup_errors: list[str] | None = None
        self.scenario_deadline: float | None = None
        self.first_failure: str | None = None
        self.last_route = 0.0
        self.stream_thread: threading.Thread | None = None
        self.stream_response: httpx.Response | None = None
        self.stream_client: httpx.Client | None = None
        self.stream_ready = threading.Event()
        self.stream_running = threading.Event()
        self.stream_stop = threading.Event()
        self.stream_errors: list[str] = []
        self.status_edges: list[dict] = []
        self.host_record: dict | None = None
        self.host_identity: dict | None = None
        self.runner: ProcessIdentity | None = None
        self.source_before = None
        self.auth_signature = None
        self.baseline: list[dict] = []
        self.config_path: Path | None = None
        self.config: dict = {}
        self.journal_path: Path | None = None
        self.seed: SelectedRootIdentity | None = None
        self.expired: set[tuple[int, float]] = set()
        for name in ("workspace", "prime", "sessions", "data", "config", "artifacts"):
            (self.runtime / name).mkdir(mode=0o700)
        self.register_owned_credential(self.prime / "auth.json")
        self.progress("prepared_no_auth")

    def progress(self, phase: str) -> None:
        write_json(
            self.evidence / "progress.json",
            {
                "phase": phase,
                "runtime": self.runtime,
                "url": self.url,
                "session_id": self.session_id,
                "owners": list(self.owners.values()),
                "unowned_unreadable": list(self.unowned_unreadable.values()),
                "foreground_host": self.foreground_host_state(),
                "bridge_roots": sorted(map(str, self.bridge_roots)),
                "updated": time.time(),
            },
        )

    def foreground_host_state(self) -> dict:
        return {
            "pid": self.foreground_host.pid if self.foreground_host else None,
            "identity": asdict(self.foreground_host_identity)
            if self.foreground_host_identity
            else None,
            "exit": self.foreground_host.poll() if self.foreground_host else None,
            "log": self.host_log_handle.name if self.host_log_handle else None,
            "log_closed": self.host_log_handle.closed if self.host_log_handle else None,
        }

    def claim(self, name: str, status: str, reason: str, *files: str) -> Observation:
        if any(item.claim == name for item in self.observations):
            raise RuntimeError(f"duplicate_claim {name}")
        for filename in files:
            if not (self.evidence / filename).is_file():
                raise RuntimeError(f"missing_claim_evidence {filename}")
        if status == "VERIFIED" and self.scenario_deadline is not None:
            self.remaining()
        item = Observation(name, status, tuple(files), reason)
        self.observations.append(item)
        print(f"{status} {name} {reason}", flush=True)
        return item

    def guard(self, action: str) -> None:
        record = {"action": action, "monotonic": time.monotonic(), "quiet": self.quiet}
        try:
            append_json(self.evidence / "route-audit.jsonl", record)
        except FINALIZATION_ERRORS as exc:
            if self.cleanup_errors is None:
                raise
            self.cleanup_errors.append(
                "route evidence: " + type(exc).__name__ + ": " + sanitize(str(exc))
            )
        if self.quiet:
            self.contaminated = True
            raise RuntimeError(f"quiet_contamination {action}")
        self.last_route = record["monotonic"]

    def env(self) -> dict[str, str]:
        keys = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR")
        result = {key: os.environ[key] for key in keys if key in os.environ}
        result.update(
            {
                "PYTHONPATH": str(REPO),
                "OMNIGENT_CONFIG_HOME": str(self.runtime / "config"),
                "OMNIGENT_DATA_DIR": str(self.runtime / "data"),
                "OMNIGENT_SKIP_WEB_UI": "true",
                "OMNIGENT_PRIME_PATH": str(self.request.prime_path),
                "PRIME_AGENT_CODING_AGENT_DIR": str(self.prime),
                "PRIME_AGENT_SESSION_DIR": str(self.runtime / "sessions"),
                "PRIME_AGENT_KERNEL_PYTHON": str(self.request.kernel_entry),
                "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S",
                "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S": str(self.profile.pane_timeout_s),
                "BROWSER": "false",
                "TERM": "xterm-256color",
            }
        )
        return result

    def preflight(self) -> None:
        if not (REPO / "pyproject.toml").is_file():
            raise RuntimeError("checkout_manifest_missing")
        if sha256(self.request.prime_path) != PRIME_SHA256:
            raise RuntimeError("prime_artifact_not_admitted")
        if not shutil.which("tmux") or not (REPO / ".venv/bin/omnigent").is_file():
            raise RuntimeError("tmux_or_checkout_cli_missing")
        self.requested_kernel, witness = requested_kernel_identity(self.request.kernel_entry)
        write_json(self.evidence / "kernel-requested.json", witness)
        self.source_before = source_snapshot(REPO)
        write_json(self.evidence / "source-before.json", asdict(self.source_before))
        write_json(
            self.evidence / "artifact.json",
            {
                "prime_path": self.request.prime_path,
                "prime_sha256": sha256(self.request.prime_path),
                "driver_sha256": sha256(Path(__file__)),
                "checkout": REPO,
                "imports": import_witnesses(REPO),
                "revision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
                ).strip(),
            },
        )
        doctor(str(self.request.prime_path), self.evidence, self.env())
        self.baseline = user_prime_baseline()
        write_json(self.evidence / "unrelated-before.json", self.baseline)
        source = self.request.auth_source
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("auth_source_must_be_regular_file")
        source_bytes = source.read_bytes()
        auth = json.loads(source_bytes)
        if not isinstance(auth, dict) or auth.get("xai", {}).get("type") != "oauth":
            raise RuntimeError("actual_xai_oauth_required")
        self.auth_signature = (
            source.stat().st_ino,
            source.stat().st_mode,
            hashlib.sha256(source_bytes).digest(),
        )
        with os.fdopen(
            os.open(self.prime / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as handle:
            handle.write(source_bytes)
        del source_bytes, auth
        write_json(
            self.prime / "settings.json",
            {
                "onboardingShown": True,
                "defaultProvider": "xai",
                "defaultModel": "grok-4.7",
                "sessionDir": str(self.runtime / "sessions"),
                "autoRefine": {"enabled": False},
                "compaction": {"enabled": False},
            },
        )
        config = f"runner:\n  idle_timeout_s: {self.profile.runner_timeout_s}\n"
        (self.runtime / "config/config.yaml").write_text(config)
        (self.runtime / "config/config.yaml").chmod(0o600)
        self.progress("preflight_complete_auth_copied")

    def request_http(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: float = 30,
        *,
        deadline: float | None = None,
    ) -> httpx.Response:
        self.guard(f"HTTP {method} {path}")
        deadline = time.monotonic() + timeout if deadline is None else deadline
        timeout = min(timeout, self.remaining(deadline))
        response = asyncio.run(self._request_http(method, path, body, timeout, deadline))
        self.remaining(deadline)
        try:
            append_json(
                self.evidence / "http-actions.jsonl",
                {
                    "method": method,
                    "path": path,
                    "body": body,
                    "status": response.status_code,
                    "response": response.text,
                    "monotonic": time.monotonic(),
                },
            )
        except FINALIZATION_ERRORS as exc:
            if self.cleanup_errors is None:
                raise
            self.cleanup_errors.append(
                "HTTP evidence: " + type(exc).__name__ + ": " + sanitize(str(exc))
            )
        self.remaining(deadline)
        return response

    async def _request_http(
        self, method: str, path: str, body: dict | None, timeout: float, deadline: float
    ) -> httpx.Response:
        if self.scenario_deadline is not None:
            deadline = min(deadline, self.scenario_deadline)
        budget = self.remaining(deadline)
        try:
            async with asyncio.timeout(budget):
                client = httpx.AsyncClient(base_url=self.url, timeout=min(timeout, budget))
                try:
                    remaining = self.remaining(deadline)
                    async with asyncio.timeout(remaining - min(0.1, remaining / 10)):
                        response = await client.request(method, path, json=body)
                        await response.aread()
                        self.remaining(deadline)
                finally:
                    async with asyncio.timeout(max(0, deadline - time.monotonic())):
                        await client.aclose()
        except TimeoutError as exc:
            raise RuntimeError("wait_scenario_deadline") from exc
        self.remaining(deadline)
        return response

    def snapshot(self, *, deadline: float | None = None) -> dict:
        response = self.request_http("GET", f"/v1/sessions/{self.session_id}", deadline=deadline)
        response.raise_for_status()
        value = response.json()
        if value.get("llm_model") != "xai/grok-4.7":
            raise RuntimeError("selected_real_model_changed")
        if value.get("id") != self.session_id or (
            self.external_id is not None and value.get("external_session_id") != self.external_id
        ):
            raise RuntimeError("public_session_identity_changed")
        if deadline is not None:
            self.remaining(deadline)
        return value

    def items(self, *, deadline: float | None = None) -> list[dict]:
        response = self.request_http(
            "GET", f"/v1/sessions/{self.session_id}/items?limit=1000&order=asc", deadline=deadline
        )
        response.raise_for_status()
        value = response.json()
        if (
            not isinstance(value, dict)
            or value.get("object") != "list"
            or value.get("has_more") is not False
            or value.get("truncated", False) is not False
            or "first_id" not in value
            or "last_id" not in value
            or not isinstance(value.get("data"), list)
            or any(not isinstance(item, dict) for item in value["data"])
            or len(value["data"]) > 1000
        ):
            raise RuntimeError("public_items_incomplete")
        items = value["data"]
        ids = _reply_ids(items, "public_item")
        for item in items:
            _public_reply_content(item)
        if (value["first_id"], value["last_id"]) != (
            ids[0] if ids else None,
            ids[-1] if ids else None,
        ):
            raise RuntimeError("public_items_truncated")
        if deadline is not None:
            self.remaining(deadline)
        return items

    def own(self, process: psutil.Process) -> dict:
        record = {
            "pid": process.pid,
            "started": process.create_time(),
            "argv": process.cmdline(),
            "ownership": "exact_run",
        }
        self.owners[(record["pid"], record["started"])] = record
        return record

    def census(self) -> list[dict]:
        captured = set(self.owners)
        for pid, started in tuple(captured):
            try:
                process = psutil.Process(pid)
                if process.create_time() == started:
                    for child in process.children(recursive=True):
                        try:
                            captured.add((child.pid, child.create_time()))
                        except (psutil.NoSuchProcess, psutil.ZombieProcess):
                            continue
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.Error, OSError, SystemError) as exc:
                self.census_errors.add(f"owned_descendants_unreadable {pid} {type(exc).__name__}")
        records = []
        unreadable = []
        for process in psutil.process_iter():
            if process.pid == os.getpid():
                continue
            owned = False
            try:
                if process.uids().real != os.getuid():
                    continue
                started = process.create_time()
                try:
                    argv = process.cmdline()
                    env = process.environ()
                except (psutil.AccessDenied, SystemError):
                    unreadable.append((process, started))
                    continue
                agent = env.get("PRIME_AGENT_CODING_AGENT_DIR", "")
                exact = env.get("OMNIGENT_DATA_DIR") == str(self.runtime / "data")
                exact = exact or bool(agent and Path(agent).resolve().is_relative_to(self.runtime))
                exact = exact or any(
                    str(root) in arg for root in self.bridge_roots for arg in argv
                )
                owned = exact or (process.pid, started) in captured
                if owned:
                    record = {
                        "pid": process.pid,
                        "started": started,
                        "argv": argv,
                        "ownership": "exact_run",
                    }
                    self.owners[(process.pid, started)] = record
                    if process.status() == psutil.STATUS_ZOMBIE:
                        continue
                    records.append(record)
                    config = env.get("OMNIGENT_EXTENSION_NATIVE_CONFIG")
                    if config:
                        self.admit_config(Path(config))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.Error, OSError, SystemError) as exc:
                if owned or any(pid == process.pid for pid, _ in captured):
                    self.census_errors.add(
                        f"owned_identity_unreadable {process.pid} {type(exc).__name__} "
                        f"exe={process_exe(process)}"
                    )
        # macOS hides setuid and exiting processes; only an owned ancestor can own them.
        owned_keys = captured | set(self.owners)
        for process, started in unreadable:
            try:
                record = self.census_unreadable(process, started, owned_keys)
                if record is not None and process.status() != psutil.STATUS_ZOMBIE:
                    records.append(record)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.Error, OSError, SystemError) as exc:
                self.census_errors.add(
                    f"owned_identity_unreadable {process.pid} {type(exc).__name__} "
                    f"exe={process_exe(process)}"
                )
        prior = live_initial_process_records(list(self.owners.values()))
        for record in prior:
            if "process_error" in record:
                self.census_errors.add(f"owned_identity_unreadable {record['pid']}")
        self.progress("owners_observed")
        return list(
            {(record["pid"], record["started"]): record for record in records + prior}.values()
        )

    def census_unreadable(
        self, process: psutil.Process, started: float, owned_keys: set[tuple[int, float]]
    ) -> dict | None:
        key = (process.pid, started)
        owned = key in owned_keys or any(
            (parent.pid, parent.create_time()) in owned_keys for parent in process.parents()
        )
        if not owned:
            self.unowned_unreadable[key] = {
                "pid": process.pid,
                "started": started,
                "exe": process_exe(process),
            }
            return None
        record = self.owners.get(key) or {
            "pid": process.pid,
            "started": started,
            "argv": [],
            "exe": process_exe(process),
            "ownership": "owned_descendant_unreadable",
        }
        self.owners[key] = record
        return record

    def owned_paths(self, session_id: str):
        from omnigent.harnesses.prime_native import bridge

        previous = bridge._DATA_ROOT
        try:
            bridge._DATA_ROOT = self.runtime / "data"
            paths = bridge.runtime_paths(session_id)
            credential = paths.agent_dir / "auth.json"
            canonical, _ = self._credential_path(credential)
            if canonical in self.credential_targets:
                self.register_owned_credential(credential)
            if canonical.parent.parent not in self._retired_trees:
                paths.validate_existing()
            if self.session_id == session_id:
                self.bridge_roots.add(paths.root)
                self.register_owned_credential(credential)
                self.progress("session_root_owned")
            return paths
        finally:
            bridge._DATA_ROOT = previous

    def own_session(self, session_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise RuntimeError("owned_session_id_invalid")
        if self.session_id is not None and self.session_id != session_id:
            raise RuntimeError("owned_session_id_changed")
        self.session_id = session_id
        self.owned_paths(session_id)

    def recover_owned_session(self) -> None:
        if self.session_id:
            self.own_session(self.session_id)
            return
        response = self.request_http("GET", "/v1/sessions?limit=2&kind=any&include_archived=true")
        response.raise_for_status()
        page = response.json()
        sessions = page.get("data", [])
        if page.get("has_more") or len(sessions) > 1:
            raise RuntimeError("private_server_owned_session_ambiguous")
        if sessions:
            self.own_session(sessions[0]["id"])
            write_json(
                self.evidence / "recovered-session.json",
                {"session_id": self.session_id, "source": "fresh_private_server"},
            )

    def admit_config(self, path: Path) -> dict:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("native_config_unavailable")
        config = json.loads(path.read_text())
        if self.session_id is None and isinstance(config.get("sessionId"), str):
            self.own_session(config["sessionId"])
        if config.get("sessionId") != self.session_id:
            return config
        controls = Path(config["primeControlsDir"]).resolve()
        root = controls.parent
        expected = self.owned_paths(self.session_id).root.resolve()
        if root != expected or path.resolve().parent != root or controls != root / "controls":
            raise RuntimeError("native_config_root_outside_exact_session")
        auth = root / "agent/auth.json"
        if auth.exists():
            if (
                auth.is_symlink()
                or not auth.is_file()
                or stat.S_IMODE(auth.stat().st_mode) != 0o600
            ):
                raise RuntimeError("native_auth_copy_privacy_mismatch")
            for directory in (root, auth.parent):
                if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
                    raise RuntimeError("native_auth_directory_privacy_mismatch")
        self.config_path, self.config = path, config
        return config

    def write(self, text: str) -> None:
        if self.terminal_log:
            with self.terminal_log.open("a") as handle:
                handle.write(sanitize(text))

    def flush(self) -> None:
        pass

    def attach(self, *, resume: bool = False) -> None:
        if self.foreground_host_record() is None:
            raise RuntimeError("owned_foreground_host_not_ready")
        self.guard("public_resume" if resume else "public_launch")
        argv = ["prime-native", "--server", self.url]
        argv += (
            ["--resume", self.session_id]
            if resume
            else ["--", "--provider", "xai", "--model", "grok-4.7", "--no-context-files"]
        )
        self.terminal = pexpect.spawn(
            str(REPO / ".venv/bin/omnigent"),
            argv,
            cwd=str(self.workspace),
            env=self.env(),
            encoding="utf-8",
            codec_errors="replace",
            timeout=min(100, self.remaining()) if self.scenario_deadline is not None else 100,
            dimensions=(40, 140),
        )
        try:
            self.terminal_log = self.evidence / ("resume.txt" if resume else "terminal.txt")
            self.terminal_log.touch(mode=0o600)
            attachment = self.own(psutil.Process(self.terminal.pid))
            self.attachment_identity = ProcessIdentity(
                attachment["pid"], attachment["started"], tuple(attachment["argv"])
            )
            self.terminal.logfile_read = self
            self.terminal.expect(r"Web UI: [^\r\n]*/c/([A-Za-z0-9_-]+)")
        finally:
            self.attachment_drain = _PtyDrain(self.terminal.child_fd, self.write)
        sid = self.terminal.match.group(1)
        if self.foreground_host_record() is None:
            raise RuntimeError("native_foreground_host_reuse_missing")
        if resume and sid != self.session_id:
            raise RuntimeError("resume_selected_different_session")
        self.own_session(sid)
        write_json(
            self.evidence / ("resume-launch.json" if resume else "terminal-launch.json"),
            {
                "argv": [str(REPO / ".venv/bin/omnigent"), *argv],
                "cwd": self.workspace,
                "identity": self.own(psutil.Process(self.terminal.pid)),
                "env": self.env(),
            },
        )
        self.census()
        self.progress("reattached" if resume else "attached")

    def foreground_host_record(self, stopped: dict | None = None) -> tuple[dict, dict] | None:
        identity = self.foreground_host_identity
        if not self.foreground_host or identity is None:
            raise RuntimeError("exact_foreground_host_identity_missing")
        if self.foreground_host.poll() is not None or not identity_alive(identity):
            raise RuntimeError("owned_foreground_host_exited")
        records = [
            json.loads(path.read_text())
            for path in (self.runtime / "data/daemons").glob("*.json")
            if not path.is_symlink()
        ]
        hosts = [record for record in records if record.get("target") == self.url]
        if not hosts:
            return None
        if len(hosts) != 1:
            raise RuntimeError("expected_exact_owned_host")
        record = hosts[0]
        if record == stopped:
            return None
        qualified = qualify_owned_host_record(
            record, self.url, self.runtime / "data", psutil.Process(identity.pid)
        )
        if qualified["pid"] != identity.pid or qualified["started"] != identity.started:
            raise RuntimeError("foreground_host_registry_identity_mismatch")
        if self.host_record is not None and record != self.host_record:
            raise RuntimeError("native_foreground_host_registry_changed")
        return record, qualified

    def start_host(self, name: str = "host") -> None:
        # A stopped host leaves its registry record until the next host replaces it.
        stopped, self.host_record = self.host_record, None
        self.guard("foreground_host_launch")
        argv = [
            str(REPO / ".venv/bin/omnigent"),
            "host",
            "--server",
            self.url,
            "--no-open",
            "--non-interactive",
        ]
        self.host_log_handle = (self.evidence / f"{name}.log").open("w")
        self.foreground_host = subprocess.Popen(
            argv,
            cwd=self.workspace,
            env=self.env(),
            stdout=self.host_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process = psutil.Process(self.foreground_host.pid)
        self.foreground_host_identity = ProcessIdentity(
            process.pid, process.create_time(), tuple(argv)
        )
        owned = self.own(process)
        self.foreground_host_identity = ProcessIdentity(
            owned["pid"], owned["started"], tuple(owned["argv"])
        )
        write_json(
            self.evidence / f"{name}-launch.json",
            {"argv": argv, "cwd": self.workspace, "env": self.env(), "identity": owned},
        )
        self.progress("foreground_host_owned")
        self.host_record, self.host_identity = wait_for(
            lambda: self.foreground_host_record(stopped), "owned foreground host registry", 60
        )

        def online() -> dict | None:
            if self.foreground_host_record() is None:
                raise RuntimeError("owned_foreground_host_registry_disappeared")
            host_id = self.host_record.get("host_id")
            if not isinstance(host_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", host_id):
                raise RuntimeError("owned_foreground_host_id_missing")
            response = self.request_http("GET", f"/v1/hosts/{host_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            snapshot = response.json()
            if snapshot.get("host_id") == host_id and snapshot.get("status") == "online":
                return snapshot
            return None

        snapshot = wait_for(online, "owned foreground host online", 60)
        write_json(
            self.evidence / f"{name}-ready.json",
            {"registry": self.host_record, "qualified": self.host_identity, "api": snapshot},
        )
        self.progress("foreground_host_ready")

    def start(self) -> None:
        self.preflight()
        self.guard("api_launch")
        argv = [
            str(REPO / ".venv/bin/omnigent"),
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            self.url.rsplit(":", 1)[1],
            "--database-uri",
            f"sqlite:///{self.runtime}/data/server.db",
            "--artifact-location",
            str(self.runtime / "artifacts"),
        ]
        self.server = subprocess.Popen(
            argv,
            cwd=self.workspace,
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        identity = self.own(psutil.Process(self.server.pid))

        def drain() -> None:
            try:
                with (self.evidence / "server.log").open("w") as handle:
                    for line in self.server.stdout:
                        handle.write(sanitize(line))
                        handle.flush()
            except FINALIZATION_ERRORS as exc:
                self.server_capture_error = type(exc).__name__ + ": " + sanitize(str(exc))

        self.server_thread = threading.Thread(target=drain, daemon=True)
        self.server_thread.start()
        write_json(
            self.evidence / "api-launch.json",
            {"argv": argv, "cwd": self.workspace, "env": self.env(), "identity": identity},
        )

        def healthy() -> bool:
            if self.server.poll() is not None:
                raise RuntimeError("owned_api_exited")
            try:
                return self.request_http("GET", "/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(healthy, "owned API health", 60)
        self.start_host()
        self.attach()

        def selected_idle() -> dict | None:
            response = self.request_http("GET", f"/v1/sessions/{self.session_id}")
            response.raise_for_status()
            snapshot = response.json()
            if snapshot.get("llm_model") == "xai/grok-4.7" and snapshot.get("status") == "idle":
                return snapshot
            return None

        snapshot = wait_for(selected_idle, "native real model and idle", 180)
        self.external_id = snapshot.get("external_session_id")
        if not self.external_id or not self.config_path:
            raise RuntimeError("native_identity_missing")
        write_json(
            self.evidence / "deployed-extension.json",
            deployed_extension_identity(
                REPO / "omnigent/resources/pi_native/omnigent_pi_native_extension.js",
                self.config_path.parent / "omnigent_pi_native_extension.js",
            ),
        )
        self.capture_runner()
        self.claim(
            "launch",
            "VERIFIED",
            "actual_xai_grok_4.7",
            "artifact.json",
            "deployed-extension.json",
            "runner-config.json",
        )

    def capture_runner(self) -> None:
        host_logs = "\n".join(
            sanitize(path.read_text(errors="replace"))
            for path in (self.runtime / "data/logs").rglob("*.log")
            if not path.is_symlink()
        )
        launch_pattern = (
            r"Launched runner [A-Za-z0-9_-]+ for workspace "
            + re.escape(str(self.workspace))
            + r" \(pid=([0-9]+)\)"
        )
        launched_pids = {int(pid) for pid in re.findall(launch_pattern, host_logs)}
        if len(launched_pids) != 1:
            raise RuntimeError("actual_runner_launch_pid_not_observed")
        candidates = []
        for record in self.census():
            if record["pid"] not in launched_pids:
                continue
            try:
                process = psutil.Process(record["pid"])
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    continue
                if process.create_time() != record["started"] or tuple(process.cmdline()) != tuple(
                    record["argv"]
                ):
                    raise RuntimeError("actual_runner_identity_changed")
                env = process.environ()
                if env.get("OMNIGENT_CONFIG_HOME") == str(self.runtime / "config"):
                    if (
                        "omnigent.runner._entry" in record["argv"]
                        or "omnigent.runner._zygote" in record["argv"]
                    ):
                        candidates.append((process, record, env))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        if len(candidates) != 1:
            raise RuntimeError(f"expected_one_actual_runner {len(candidates)}")
        process, record, env = candidates[0]
        self.runner = ProcessIdentity(record["pid"], record["started"], tuple(record["argv"]))
        path = Path(env["OMNIGENT_CONFIG_HOME"]) / "config.yaml"
        config = path.read_text()
        if (
            yaml.safe_load(config).get("runner", {}).get("idle_timeout_s")
            != self.profile.runner_timeout_s
        ):
            raise RuntimeError("actual_runner_config_mismatch")
        if env.get("OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S") != str(self.profile.pane_timeout_s):
            raise RuntimeError("actual_runner_pane_env_mismatch")
        keys = (
            "PYTHONPATH",
            "OMNIGENT_CONFIG_HOME",
            "OMNIGENT_DATA_DIR",
            "OMNIGENT_RUNNER_ID",
            "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S",
            "PRIME_AGENT_KERNEL_PYTHON",
        )
        write_json(
            self.evidence / "runner-config.json",
            {
                "identity": record,
                "path": path,
                "bytes": config,
                "env": {key: env.get(key) for key in keys},
                "actual_host_launch_log": re.findall(launch_pattern, host_logs),
            },
        )
        timeout = self.profile.pane_timeout_s
        pattern = (
            rf"native pane reaper started \(idle_timeout={timeout}(?:\.0)?s, interval=60(?:\.0)?s"
        )
        logs = wait_for(
            lambda: self.runner_logs() if re.search(pattern, self.runner_logs()) else None,
            "actual reaper startup",
            15,
        )
        if ("; DISABLED" in logs) != (timeout == 0):
            raise RuntimeError("actual_reaper_enabled_state_mismatch")
        write_json(self.evidence / "runner-startup.json", {"pattern": pattern, "logs": logs})
        host = self.foreground_host_record()
        if host is None:
            raise RuntimeError("native_foreground_host_reuse_missing")
        self.host_record, self.host_identity = host
        write_json(
            self.evidence / "owned-host.json",
            {"registry": self.host_record, "qualified": self.host_identity},
        )

    def runner_logs(self) -> str:
        return "\n".join(
            sanitize(path.read_text(errors="replace"))
            for path in sorted((self.runtime / "data/logs/runner").rglob("*.log"))
            if not path.is_symlink()
        )

    def journal(self) -> list[dict]:
        if self.journal_path is None:
            matches = []
            for root in self.bridge_roots:
                for path in root.rglob("*.jsonl"):
                    if self.external_id and self.external_id in path.name:
                        lines = path.read_text().splitlines()
                        if lines and json.loads(lines[0]).get("id") == self.external_id:
                            matches.append(path)
            if len(matches) != 1:
                raise RuntimeError("expected_one_exact_native_journal")
            self.journal_path = matches[0]
        lines = self.journal_path.read_text().splitlines(keepends=True)
        entries = []
        for index, line in enumerate(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                if index != len(lines) - 1 or line.endswith("\n"):
                    raise
            else:
                if not isinstance(entry, dict):
                    raise RuntimeError("native_journal_row_malformed")
                entries.append(entry)
        changes = [entry for entry in entries if entry.get("type") == "model_change"]
        if not changes or any(
            entry.get("provider") != "xai" or entry.get("modelId") != "grok-4.7"
            for entry in changes
        ):
            raise RuntimeError("native_provider_model_mismatch")
        return entries

    def send(self, prompt: str, *, terminal: bool = False, deadline: float | None = None) -> None:
        if deadline is not None or self.scenario_deadline is not None:
            self.remaining(deadline)
        if terminal:
            self.guard("terminal_input")
            if not self.terminal or not self.terminal.isalive():
                raise RuntimeError("attachment_not_alive")
            self.terminal.send(
                (f"\x1b[200~{prompt}\x1b[201~" if "\n" in prompt else prompt) + "\r"
            )
        else:
            response = self.request_http(
                "POST",
                f"/v1/sessions/{self.session_id}/events",
                {
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                },
                deadline=deadline,
            )
            if response.status_code != 202:
                raise RuntimeError(f"message_not_admitted {response.status_code}")
        if deadline is not None or self.scenario_deadline is not None:
            self.remaining(deadline)

    def reply_operation(
        self, prompt: str, literal: str, code: str | None, deadline: float
    ) -> _ReplyOperation:
        self.remaining(deadline)
        session_id, external_id, root = self.session_id, self.external_id, self.seed
        journal_path = self.journal_path
        if root is not None:
            assert_same_selected_root_identity(root, self.selected_root(passive=True))
        native = self.journal()
        if journal_path is None:
            journal_path = self.journal_path
        public = self.items(deadline=deadline)
        if self.journal_path != journal_path or (self.session_id, self.external_id) != (
            session_id,
            external_id,
        ):
            raise RuntimeError("native_journal_identity_changed")
        if root is not None:
            assert_same_selected_root_identity(root, self.selected_root(passive=True))
        self.remaining(deadline)
        operation = _ReplyOperation(
            prompt,
            literal,
            code,
            deadline,
            _reply_rows(native),
            _reply_rows(public),
            _reply_ids(native, "native_entry"),
            _reply_ids(public, "public_item"),
            journal_path,
            session_id,
            external_id,
            tuple(entry["id"] for entry in native if entry.get("type") == "session"),
            root,
        )
        _reply_verdict(operation, native, public)
        return operation

    def completed_reply(self, operation: _ReplyOperation, name: str) -> dict:
        observed = operation.native_baseline
        public_observed = operation.public_baseline
        predicates = {"reason": "native_journal_pending"}
        try:
            while True:
                self.remaining(operation.deadline)
                if self.journal_path != operation.journal_path or (
                    self.session_id,
                    self.external_id,
                ) != (operation.session_id, operation.external_id):
                    raise RuntimeError("native_journal_identity_changed")
                if operation.root is not None:
                    assert_same_selected_root_identity(
                        operation.root, self.selected_root(passive=True)
                    )
                entries = self.journal()
                if _reply_rows(entries[: len(observed)]) != observed:
                    raise RuntimeError("native_observed_prefix_changed")
                observed = _reply_rows(entries)
                verdict = _reply_verdict(operation, entries, None)
                predicates = verdict["predicates"]
                self.remaining(operation.deadline)
                items = self.items(deadline=operation.deadline)
                self.snapshot(deadline=operation.deadline)
                if _reply_rows(items[: len(public_observed)]) != public_observed:
                    raise RuntimeError("public_observed_prefix_changed")
                public_observed = _reply_rows(items)
                entries = self.journal()
                if _reply_rows(entries[: len(observed)]) != observed:
                    raise RuntimeError("native_observed_prefix_changed")
                observed = _reply_rows(entries)
                verdict = _reply_verdict(operation, entries, items)
                predicates = verdict["predicates"]
                if self.journal_path != operation.journal_path or (
                    self.session_id,
                    self.external_id,
                ) != (operation.session_id, operation.external_id):
                    raise RuntimeError("native_journal_identity_changed")
                if operation.root is not None:
                    assert_same_selected_root_identity(
                        operation.root, self.selected_root(passive=True)
                    )
                self.remaining(operation.deadline)
                if verdict["complete"]:
                    write_json(self.evidence / f"{name}-native-predicates.json", predicates)
                    self.remaining(operation.deadline)
                    return verdict
                time.sleep(min(0.1, self.remaining(operation.deadline)))
        except FINALIZATION_ERRORS as exc:
            if isinstance(exc, _NativeReplyFailure):
                observation = asdict(exc.observation)
                observation["operation"] = (
                    name
                    if name
                    in {
                        "kernel-seed",
                        "steering-consumed",
                        "terminal-followup",
                        "memory-read",
                        "before",
                        "outage",
                        "after",
                    }
                    else "unknown"
                )
                observation["diagnostic"]["operation"] = (
                    "kernel_seed" if name == "kernel-seed" else "unknown"
                )
                observation["diagnostic"]["kernel_identity_receipt_present"] = (
                    self.evidence / "kernel-identity.json"
                ).is_file()
                observation["reason"] = "native_assistant_not_successful"
                write_json(self.evidence / f"{name}-native-failure.json", observation)
                predicates = {**predicates, "observation_state": "previous_poll"}
            if isinstance(exc, _PublicPromptError):
                predicates = exc.predicates
            predicates["reason"] = (
                str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            )
            write_json(self.evidence / f"{name}-native-predicates.json", predicates)
            raise

    def message(
        self,
        name: str,
        prompt: str,
        literal: str,
        *,
        terminal: bool = False,
        code: str | None = None,
        timeout: float = 180,
    ) -> dict:
        operation_deadline = time.monotonic() + timeout
        if self.scenario_deadline is not None:
            operation_deadline = min(operation_deadline, self.scenario_deadline)
        self.wait(
            lambda: self.snapshot(deadline=operation_deadline).get("status") == "idle",
            "idle before message",
            min(90, self.remaining(operation_deadline)),
        )
        operation = self.reply_operation(prompt, literal, code, operation_deadline)
        self.send(prompt, terminal=terminal, deadline=operation.deadline)
        payload = self.completed_reply(operation, name)
        if terminal:
            screen = self.tmux(
                "capture-pane", "-p", "-S", "-200", "-t", self.metadata["tmux_target"]
            )
            payload["screen"] = sanitize(screen.stdout)
            if screen.returncode or not any(
                line.strip() == literal for line in screen.stdout.splitlines()
            ):
                raise RuntimeError("terminal_literal_rendering_mismatch")
        write_json(self.evidence / f"{name}.json", payload)
        self.census()
        self.remaining(operation.deadline)
        return payload

    def selected_root(self, *, passive: bool = False) -> SelectedRootIdentity:
        records = self.census()
        if self.census_errors:
            raise RuntimeError("owned_census_evidence_gap")
        if not self.config_path:
            raise RuntimeError("selected_native_config_missing")
        config = self.admit_config(self.config_path)
        if config.get("sessionId") != self.session_id:
            raise RuntimeError("selected_native_config_session_changed")
        controls = Path(config["primeControlsDir"]).resolve()
        binding = json.loads((controls / "binding.json").read_text())
        pid, incarnation = binding["pid"], binding["incarnation"]
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or not isinstance(incarnation, str)
            or not incarnation
        ):
            raise RuntimeError("selected_binding_malformed")
        agent = controls.parent / "agent"
        config_path = self.config_path.resolve()
        prime = self.request.prime_path.resolve()
        socket_dir = controls.parent / "tmp" / f"prime-agent-{os.getuid()}"
        observed = []
        selected = {}
        expected = {record["pid"]: record for record in records}
        for record in records:
            try:
                process = psutil.Process(record["pid"])
                identity = ProcessIdentity(
                    process.pid, process.create_time(), tuple(process.cmdline())
                )
                if identity.started != record["started"] or identity.argv != tuple(record["argv"]):
                    raise RuntimeError("selected_owned_process_identity_changed")
                env = process.environ()
                raw_agent = env.get("PRIME_AGENT_CODING_AGENT_DIR")
                raw_config = env.get("OMNIGENT_EXTENSION_NATIVE_CONFIG")
                actual_agent = Path(raw_agent).resolve() if raw_agent else None
                actual_config = Path(raw_config).resolve() if raw_config else None
                observed.append(
                    {
                        **asdict(identity),
                        "PRIME_AGENT_CODING_AGENT_DIR": raw_agent,
                        "OMNIGENT_EXTENSION_NATIVE_CONFIG": raw_config,
                        "agent_dir": actual_agent,
                        "config_path": actual_config,
                    }
                )
                if (
                    actual_agent == agent
                    and actual_config == config_path
                    and identity.argv
                    and Path(identity.argv[0]).resolve() == prime
                    and Path(process.exe()).resolve() == prime
                ):
                    selected[process.pid] = process
                    for child in process.children(recursive=True):
                        selected[child.pid] = child
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                continue
        identities = []
        workers = []
        for process in selected.values():
            try:
                started = process.create_time()
                if process.uids().real != os.getuid():
                    raise RuntimeError(
                        f"selected_descendant_foreign_uid {process.pid} {process_exe(process)}"
                    )
                try:
                    identity = ProcessIdentity(process.pid, started, tuple(process.cmdline()))
                    env = process.environ()
                except (psutil.AccessDenied, SystemError):
                    identity, env = ProcessIdentity(process.pid, started, ()), None
                record = expected.get(process.pid)
                if (
                    record is None
                    or identity.started != record["started"]
                    or identity.argv != tuple(record["argv"])
                ):
                    # A qualified root started or replaced this descendant after the census.
                    self.owners.setdefault(
                        (process.pid, started),
                        {
                            "pid": process.pid,
                            "started": started,
                            "argv": list(identity.argv),
                            "exe": process_exe(process),
                            "ownership": "selected_descendant",
                        },
                    )
                if env is None:
                    continue
                raw_agent = env.get("PRIME_AGENT_CODING_AGENT_DIR")
                raw_config = env.get("OMNIGENT_EXTENSION_NATIVE_CONFIG")
                if (
                    not raw_agent
                    or Path(raw_agent).resolve() != agent
                    or not raw_config
                    or Path(raw_config).resolve() != config_path
                ):
                    continue
                argv = identity.argv
                if argv == (str(self.requested_kernel.entry), "-m", "rlm.repl"):
                    identities.append(identity)
                    continue
                if (
                    not argv
                    or Path(argv[0]).resolve() != prime
                    or Path(process.exe()).resolve() != prime
                ):
                    continue
                flags = {}
                for flag in ("--extension", "--session-dir", "--mode", "--daemon-socket"):
                    if argv.count(flag) > 1:
                        raise RuntimeError("selected_native_duplicate_flag")
                    if flag in argv:
                        index = argv.index(flag)
                        if index + 1 == len(argv) or argv[index + 1].startswith("--"):
                            raise RuntimeError("selected_native_flag_value_missing")
                        flags[flag] = argv[index + 1]
                if "--daemon-socket" in flags:
                    path = Path(flags["--daemon-socket"]).resolve()
                    if flags.get("--mode") != "daemon" or path.parent != socket_dir:
                        continue
                    if path.name == "daemon.sock":
                        identities.append(identity)
                    elif path.name.startswith("worker-") and path.name.endswith(".sock"):
                        workers.append(identity)
                        identities.append(identity)
                elif (
                    "--mode" not in flags
                    and "--print" not in argv
                    and "--extension" in flags
                    and Path(flags["--extension"]).resolve()
                    == controls.parent / "omnigent_pi_native_extension.js"
                    and "--session-dir" in flags
                    and Path(flags["--session-dir"]).resolve() == controls.parent / "sessions"
                ):
                    identities.append(identity)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        identities.sort(key=lambda identity: identity.pid)
        append_json(
            self.evidence / "root-observations.jsonl",
            {
                "session_id": self.session_id,
                "bridge_root": controls.parent,
                "agent_dir": agent,
                "config_path": config_path,
                "binding": {"pid": pid, "incarnation": incarnation},
                "observed": observed,
                "admitted": [asdict(identity) for identity in identities],
            },
        )
        if len(workers) > 1:
            raise RuntimeError(f"Selected Prime root has {len(workers)} worker roles")
        roles = qualify_selected_root_roles(
            tuple(identities), binding_pid=pid, kernel_entry=str(self.requested_kernel.entry)
        )
        external = self.external_id if passive else self.snapshot().get("external_session_id")
        if external != self.external_id:
            raise RuntimeError("native_session_identity_changed")
        return SelectedRootIdentity(
            self.session_id,
            str(self.config_path),
            str(controls.parent),
            str(agent),
            pid,
            roles.worker.started,
            incarnation,
            external,
            tuple(identities),
            roles,
        )

    def seed_memory(self) -> None:
        self.token = uuid.uuid4().hex
        self.kernel_nonce = uuid.uuid4().hex
        sidecar = self.evidence / "kernel-seed-witness.json"
        self.kernel_expression = (
            "{'nonce': NONCE, 'pid': os.getpid(), 'executable': sys.executable, "
            "'prefix': sys.prefix, 'rlm_module': provider_rlm.__file__, "
            "'rlm_sha256': hashlib.sha256(Path(provider_rlm.__file__).read_bytes()).hexdigest(), "
            "'token': provider_value['token'], 'count': provider_value['count'], "
            "'socket_live': provider_value['socket'].fileno() >= 0}"
        )
        seed_identity = self.kernel_expression.replace("NONCE", repr(self.kernel_nonce))
        code = (
            "import hashlib, importlib, json, os, socket, sys\nfrom pathlib import Path\n"
            f"provider_value = {{'token': {self.token!r}, "
            "'count': 41, 'socket': socket.socket()}\n"
            "provider_rlm = importlib.import_module('rlm')\n"
            f"Path({str(sidecar)!r}).write_text(json.dumps({seed_identity}))\n"
            "print(provider_value['token'], provider_value['count'], "
            "provider_value['socket'].fileno() >= 0)"
        )
        self.message(
            "kernel-seed",
            "Execute this exact code using ipython once. "
            "Then reply with exactly the printed line and nothing else.\n" + code,
            f"{self.token} 41 True",
            code=code,
        )
        self.seed = self.selected_root()
        self.actual_kernel = read_actual_kernel_identity(sidecar)
        self.qualify_kernel(self.actual_kernel, self.kernel_nonce, self.seed)
        payload = json.loads(sidecar.read_text())
        if (
            payload.get("token") != self.token
            or payload.get("count") != 41
            or payload.get("socket_live") is not True
        ):
            raise RuntimeError("memory_seed_sidecar_mismatch")
        write_json(self.evidence / "root-seed.json", selected_root_identity_record(self.seed))
        self.claim(
            "kernel_seed",
            "VERIFIED",
            "native_ipython_and_owned_kernel",
            "kernel-seed.json",
            "kernel-seed-witness.json",
            "root-seed.json",
        )
        response = self.request_http(
            "GET", f"/v1/sessions/{self.session_id}/resources/terminals/terminal_prime-native_main"
        )
        response.raise_for_status()
        self.resource = response.json()
        self.metadata = self.resource["metadata"]
        socket_path = Path(self.metadata["tmux_socket"])
        if socket_path.is_symlink() or not stat.S_ISSOCK(socket_path.stat().st_mode):
            raise RuntimeError("terminal_socket_must_be_actual_socket")
        pane = self.tmux(
            "display-message", "-p", "-t", self.metadata["tmux_target"], "#{pane_pid} #{pid}"
        )
        if pane.returncode:
            raise RuntimeError("actual_tmux_pane_identity_unavailable")
        pane_pid, server_pid = map(int, pane.stdout.split())
        tui = psutil.Process(self.seed.roles.tui.pid)
        if pane_pid not in {tui.pid, *(ancestor.pid for ancestor in tui.parents())}:
            raise RuntimeError("public_tmux_pane_does_not_own_selected_tui")
        server = psutil.Process(server_pid)
        if server.environ().get("OMNIGENT_DATA_DIR") != str(self.runtime / "data"):
            raise RuntimeError("public_tmux_server_is_not_exact_owned")
        self.own(server)
        self.terminal_sockets.add(socket_path)
        write_json(
            self.evidence / "terminal-ownership.json",
            {
                "metadata": self.metadata,
                "pane_pid": pane_pid,
                "tmux_server": self.own(server),
                "selected_tui": asdict(self.seed.roles.tui),
            },
        )
        self.selector()

    def qualify_kernel(self, actual, nonce: str, root: SelectedRootIdentity) -> None:
        kernel = root.roles.kernel
        qualify_kernel_identity(
            self.requested_kernel,
            actual,
            nonce,
            [{"pid": kernel.pid, "started": kernel.started, "argv": list(kernel.argv)}],
        )

    def selector(self) -> None:
        from omnigent.harness_aliases import is_native_harness
        from omnigent.terminals.pane_reaper import NATIVE_PANE_TERMINAL_NAMES

        resource = self.resource
        name = self.metadata.get("terminal_name")
        key = self.metadata.get("session_key")
        launcher = REPO / "omnigent/harnesses/prime_native/main.py"
        launches = [
            node
            for node in ast.walk(ast.parse(launcher.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "launch_required_terminal"
        ]
        if len(launches) != 1:
            raise RuntimeError("required_prime_terminal_launcher_ambiguous")
        keywords = {
            keyword.arg: ast.literal_eval(keyword.value)
            for keyword in launches[0].keywords
            if keyword.arg in ("terminal_name", "session_key", "resource_role")
        }
        role = keywords.get("resource_role")
        if (
            resource.get("id") != "terminal_prime-native_main"
            or name != "prime-native"
            or key != "main"
        ):
            raise RuntimeError("actual_prime_resource_identity_mismatch")
        if role != "prime-native":
            raise RuntimeError(f"actual_prime_resource_role_mismatch {role}")
        eligible = name in NATIVE_PANE_TERMINAL_NAMES and key == "main" and is_native_harness(role)
        write_json(
            self.evidence / "selector.json",
            {
                "resource": resource,
                "terminal_name": name,
                "session_key": key,
                "role": role,
                "role_observation": "inferred_from_exact_launcher_source_and_selected_live_tui",
                "launcher_sha256": sha256(launcher),
                "public_role_field": "not_exposed",
                "selected_root": selected_root_identity_record(self.seed),
                "selector_source": str(REPO / "omnigent/terminals/pane_reaper.py"),
                "selector_sha256": sha256(REPO / "omnigent/terminals/pane_reaper.py"),
                "registry_sha256": sha256(REPO / "omnigent/terminals/registry.py"),
                "eligible": eligible,
                "qualification": "inferred_source_eligibility_on_actual_public_resource",
                "live_registry_census": "NOT VERIFIED no_public_selector_diagnostics",
            },
        )
        self.claim(
            "selector_eligible",
            "VERIFIED" if eligible else "NOT VERIFIED",
            "inferred_source_eligibility" if eligible else "selector_excludes_prime",
            "selector.json",
        )
        if not eligible and self.kind != "selector":
            raise RuntimeError("selector_excludes_prime")

    def tmux(self, *arguments: str, passive: bool = False) -> subprocess.CompletedProcess:
        if not passive:
            self.guard("tmux " + arguments[0])
        elif arguments[0] not in ("display-message", "list-clients", "has-session"):
            raise RuntimeError("nonpassive_tmux_action")
        return subprocess.run(
            ["tmux", "-S", self.metadata["tmux_socket"], *arguments],
            capture_output=True,
            text=True,
            timeout=min(5, self.remaining()) if self.scenario_deadline is not None else 5,
        )

    def clocks(self) -> dict:
        output = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.metadata["tmux_target"],
            "#{window_activity}",
            passive=True,
        )
        inputs = self.tmux(
            "list-clients",
            "-t",
            self.metadata["tmux_target"].split(":", 1)[0],
            "-F",
            "#{client_control_mode} #{client_activity}",
            passive=True,
        )
        values = [
            float(parts[1])
            for line in inputs.stdout.splitlines()
            if len(parts := line.split()) == 2 and parts[0] == "0" and parts[1].isdigit()
        ]
        return {
            "wall": time.time(),
            "monotonic": time.monotonic(),
            "output_at": float(output.stdout.strip())
            if output.returncode == 0 and output.stdout.strip().isdigit()
            else None,
            "input_at": max(values) if values else None,
            "output_exit": output.returncode,
            "input_exit": inputs.returncode,
        }

    def close_attachment(self) -> dict:
        identity = self.attachment_identity
        if identity is None or identity.pid != self.terminal.pid:
            raise RuntimeError("exact_attachment_identity_missing")
        if not self.attachment_drain.wait_eof(
            min(15, self.remaining()) if self.scenario_deadline is not None else 15
        ):
            raise RuntimeError("attachment_pty_eof_missing")
        wait_for(
            lambda: not self.terminal.isalive() and not identity_alive(identity),
            "exact attachment child exit",
            min(15, self.remaining()) if self.scenario_deadline is not None else 15,
        )
        if self.terminal.exitstatus is None and self.terminal.signalstatus is None:
            raise RuntimeError("attachment_child_exit_status_missing")
        self.terminal.close(force=False)
        result = {
            "identity": asdict(identity),
            "pty_eof": True,
            "exact_child_exited": True,
            "exitstatus": self.terminal.exitstatus,
            "signalstatus": self.terminal.signalstatus,
        }
        append_json(self.evidence / "attachment-exits.jsonl", result)
        return result

    def detach(self) -> None:
        write_json(self.evidence / "clocks-before-detach.json", self.clocks())
        result = self.tmux("detach-client", "-s", self.metadata["tmux_target"].split(":", 1)[0])
        if result.returncode:
            raise RuntimeError("owned_tmux_detach_failed")
        attachment = self.close_attachment()
        if not identity_alive(self.seed.roles.tui):
            raise RuntimeError("detach_ended_required_tui")
        write_json(
            self.evidence / "detach.json",
            {
                "argv": result.args,
                "exit": result.returncode,
                "pty_eof": True,
                "attachment_exit": attachment,
                "required_tui_alive": True,
                "monotonic": time.monotonic(),
                "last_route": self.last_route,
            },
        )

    def open_stream(self) -> None:
        self.guard("public_status_stream_open")
        self.stream_client = httpx.Client(timeout=httpx.Timeout(10, read=90))

        def consume() -> None:
            try:
                with self.stream_client.stream(
                    "GET", f"{self.url}/v1/sessions/{self.session_id}/stream"
                ) as response:
                    self.stream_response = response
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if self.stream_stop.is_set():
                            break
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        value = json.loads(data)
                        record = {"monotonic": time.monotonic(), "event": value}
                        append_json(self.evidence / "status-edges.jsonl", record)
                        if value.get("type") == "session.heartbeat":
                            self.stream_ready.set()
                        if value.get("type") == "session.status":
                            self.status_edges.append(record)
                            if value.get("status") == "running":
                                self.stream_running.set()
            except FINALIZATION_ERRORS as exc:
                if not self.stream_stop.is_set():
                    self.stream_errors.append(type(exc).__name__ + ": " + sanitize(str(exc)))

        self.stream_thread = threading.Thread(target=consume, daemon=True)
        self.stream_thread.start()
        if not self.stream_ready.wait(15):
            raise RuntimeError("public_stream_not_ready")

    def close_stream(self) -> None:
        if not self.stream_thread:
            return
        self.guard("public_status_stream_close")
        self.stream_stop.set()
        if self.stream_response:
            transport = self.stream_response.extensions.get("network_stream")
            connection = transport.get_extra_info("socket") if transport else None
            if not self.stream_response.is_closed:
                if connection is None:
                    raise RuntimeError("public_stream_socket_identity_missing")
                connection.shutdown(socket.SHUT_RDWR)
        self.stream_thread.join(timeout=5)
        if self.stream_thread.is_alive():
            raise RuntimeError("public_stream_not_closed")
        if self.stream_response:
            self.stream_response.close()
        if self.stream_client:
            self.stream_client.close()
        if self.stream_errors:
            raise RuntimeError("public_stream_evidence_gap " + str(self.stream_errors))
        write_json(
            self.evidence / "stream-closed.json",
            {"closed": True, "monotonic": time.monotonic(), "edges": self.status_edges},
        )
        self.stream_thread = None

    def idle_control(self) -> None:
        self.detach()
        started = time.monotonic()
        self.quiet = True
        decision = None
        try:
            deadline = started + (37 if self.profile.name == "runner" else 360)
            while time.monotonic() < deadline:
                logs = self.runner_logs()
                if self.profile.name == "runner":
                    matched = re.search(
                        r"runner idle timeout reached after [0-9.]+s "
                        r"with no active work; shutting down",
                        logs,
                    )
                else:
                    matched = re.search(
                        r"reaping idle native pane for conversation "
                        + re.escape(self.session_id)
                        + r" \(prime-native; idle > 30s\)",
                        logs,
                    )
                append_json(
                    self.evidence / "idle-observations.jsonl",
                    {
                        "elapsed": time.monotonic() - started,
                        "runner_alive": identity_alive(self.runner),
                        "tui_alive": identity_alive(self.seed.roles.tui),
                        "clocks": self.clocks(),
                    },
                )
                if matched:
                    decision = {"elapsed": time.monotonic() - started, "log": matched.group(0)}
                    break
                time.sleep(0.5)
            if not decision:
                raise RuntimeError(f"{self.profile.name}_idle_decision_missing")
            write_json(self.evidence / "idle-decision.json", decision)
            if self.profile.name == "runner":
                wait_for(
                    lambda: not identity_alive(self.runner),
                    "runner 15-second drain plus 5-second margin",
                    20,
                )
                self.expired.add((self.runner.pid, self.runner.started))
            else:
                wait_for(
                    lambda: not any(identity_alive(role) for role in as_role_tuple(self.seed)),
                    "reaped exact native root absence",
                    max(1, min(20, started + 360 - time.monotonic())),
                )
                pane = self.tmux(
                    "has-session",
                    "-t",
                    self.metadata["tmux_target"].split(":", 1)[0],
                    passive=True,
                )
                if pane.returncode == 0 or self.private_sockets():
                    raise RuntimeError("idle_reap_pane_or_private_socket_survives")
                self.expired.update((role.pid, role.started) for role in as_role_tuple(self.seed))
            write_json(
                self.evidence / "idle-expiry.json",
                {
                    "elapsed": time.monotonic() - started,
                    "decision": decision,
                    "expired": sorted(self.expired),
                    "private_sockets": self.private_sockets(),
                },
            )
        finally:
            self.quiet = False
        if self.contaminated:
            raise RuntimeError("quiet_contamination")
        self.claim(
            f"{self.profile.name}_idle_negative",
            "VERIFIED",
            "literal_production_decision_and_exact_absence",
            "idle-decision.json",
            "idle-expiry.json",
            "idle-observations.jsonl",
        )

    def remaining(self, deadline: float | None = None) -> float:
        end = deadline if deadline is not None else self.scenario_deadline
        if self.scenario_deadline is not None:
            end = min(end, self.scenario_deadline)
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("wait_scenario_deadline")
        return remaining

    def wait(self, predicate: Callable, description: str, timeout: float):
        deadline = time.monotonic() + timeout
        if self.scenario_deadline is not None:
            deadline = min(deadline, self.scenario_deadline)

        def observed():
            self.remaining(deadline)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for {description}")
            value = predicate()
            self.remaining(deadline)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for {description}")
            return value

        return wait_for(observed, description, self.remaining(deadline))

    def record_failure(self, exc: BaseException, stage: str) -> None:
        record = {
            "reason": sanitize(str(exc)),
            "type": type(exc).__name__,
            "stage": stage,
            "traceback": sanitize(traceback.format_exc()),
            "observed_monotonic": time.monotonic(),
        }
        if self.first_failure is None:
            self.first_failure = record["reason"]
            write_json(self.evidence / "failure.json", record, immutable=True)
            self.claim("failure", "FAILED", self.first_failure, "failure.json")
        append_json(self.evidence / "failures.jsonl", record)

    def observer_owner(self) -> None:
        if (
            self.native_observer
            and self.native_observer.error
            and any(
                reason in self.native_observer.error
                for reason in ("core_role_changed", "binding_or_config_changed", "library_changed")
            )
        ):
            raise RuntimeError(self.native_observer.error)
        if not all(identity_alive(role) for role in as_role_tuple(self.seed)):
            raise RuntimeError("native_observer_core_role_changed")
        binding = json.loads((Path(self.seed.bridge_root) / "controls/binding.json").read_text())
        if (
            binding.get("pid") != self.seed.binding_pid
            or binding.get("incarnation") != self.seed.binding_incarnation
            or sha256(self.config_path) != self.observer_config_sha256
        ):
            raise RuntimeError("native_observer_binding_or_config_changed")
        if sha256(self.requested_kernel.rlm_module) != self.requested_kernel.rlm_sha256:
            raise RuntimeError("native_observer_library_changed")

    def open_native_observer(self, steering: str, end_path: Path, deadline: float) -> None:
        self.guard("native_observer_connect_after_quiet")
        assert_same_selected_root_identity(self.seed, self.selected_root(passive=True))
        assert_same_sources(self.source_before, source_snapshot(REPO))
        self.observer_config_sha256 = sha256(self.config_path)
        supervisor = self.seed.roles.supervisor
        index = supervisor.argv.index("--daemon-socket")
        raw_path = Path(supervisor.argv[index + 1])
        path = raw_path.resolve()
        expected = (
            Path(self.seed.bridge_root) / "tmp" / f"prime-agent-{os.getuid()}" / "daemon.sock"
        )
        if raw_path.is_symlink() or path != expected or not stat.S_ISSOCK(path.stat().st_mode):
            raise RuntimeError("native_observer_socket_not_exact_owned_supervisor")
        self.native_observer = _NativeQueueObserver(
            self.seed,
            self.journal_path,
            end_path,
            steering,
            deadline,
            self.observer_owner,
            lambda record: append_json(self.evidence / "native-observer-records.jsonl", record),
        )
        self.native_observer.open(path)
        assert_same_selected_root_identity(self.seed, self.selected_root(passive=True))
        assert_same_sources(self.source_before, source_snapshot(REPO))
        write_json(
            self.evidence / "native-observer-attached.json", self.native_observer.evidence()
        )

    def close_native_observer(self) -> None:
        if self.native_observer is not None:
            try:
                self.native_observer.close()
            except FINALIZATION_ERRORS as exc:
                if self.native_observer_close_error is None:
                    self.native_observer_close_error = (
                        type(exc).__name__ + ": " + sanitize(str(exc))
                    )
                raise
            write_json(self.evidence / "native-observer.json", self.native_observer.evidence())
            if self.native_observer_close_error:
                raise RuntimeError(self.native_observer_close_error)

    def running_wait(self) -> None:
        started_path = self.evidence / "wait-start.json"
        ended_path = self.evidence / "wait-end.json"
        nonce = uuid.uuid4().hex
        timeline = f"{{'nonce': {nonce!r}, 'pid': os.getpid(), 'monotonic': time.monotonic()}}"
        code = (
            "import time\n"
            f"Path({str(started_path)!r}).write_text(json.dumps({timeline}))\n"
            f"time.sleep({self.profile.wait_s})\n"
            f"Path({str(ended_path)!r}).write_text(json.dumps({timeline}))\n"
            f"print('WAIT_DONE {nonce}')"
        )
        prompt = (
            "Execute this exact code using ipython once. After it returns, reply with exactly "
            "its printed line unless a newer user message asks for a different reply.\n" + code
        )
        operation_deadline = time.monotonic() + 90 + self.profile.deadline_s
        operand = int(uuid.uuid4().hex[:10], 16)
        expected = str(operand + 17)
        steering = (
            f"Calculate {operand} + 17. After the current tool completes, "
            "respond with only the decimal integer result."
        )
        self.open_stream()
        operation = self.reply_operation(steering, expected, None, operation_deadline)
        native_before = set(operation.native_ids)
        submitted_at = time.monotonic()
        self.send(prompt, deadline=operation.deadline)
        wait_for(
            lambda: started_path.exists() and self.stream_running.is_set(),
            "actual wait start and public running edge",
            90,
        )
        start = json.loads(started_path.read_text())
        if not any(
            edge["monotonic"] >= submitted_at and edge["event"].get("status") == "running"
            for edge in self.status_edges
        ):
            raise RuntimeError("fresh_public_running_edge_missing")
        deadline = start["monotonic"] + self.profile.deadline_s
        self.scenario_deadline = deadline
        if start.get("nonce") != nonce or start.get("pid") != self.seed.roles.kernel.pid:
            raise RuntimeError("wait_start_identity_mismatch")
        fresh = [entry for entry in self.journal() if entry.get("id") not in native_before]
        tool_start = native_tool(fresh, code, None)
        if ended_path.exists():
            raise RuntimeError("wait_finished_before_detach")
        write_json(
            self.evidence / "wait-admission.json",
            {"code": code, "start": start, "tool": tool_start, "public_edges": self.status_edges},
        )
        self.close_stream()
        self.detach()
        detached = self.selected_root(passive=True)
        assert_same_selected_root_identity(self.seed, detached)
        write_json(self.evidence / "root-detached.json", selected_root_identity_record(detached))
        quiet_started = time.monotonic()
        samples = []
        self.quiet = True
        try:
            while time.monotonic() - quiet_started <= self.profile.quiet_s:
                if ended_path.exists():
                    raise RuntimeError("wait_ended_before_required_quiet_interval")
                if time.monotonic() >= deadline:
                    raise RuntimeError("wait_scenario_deadline")
                if not identity_alive(self.runner) or not all(
                    identity_alive(role) for role in as_role_tuple(self.seed)
                ):
                    raise RuntimeError("required_owner_exited_during_wait")
                clocks = self.clocks()
                if clocks["output_exit"]:
                    raise RuntimeError("required_pane_absent_during_wait")
                samples.append(clocks)
                append_json(self.evidence / "quiet-observations.jsonl", clocks)
                time.sleep(0.5)
            quiet_ended = time.monotonic()
            if ended_path.exists():
                raise RuntimeError("wait_ended_before_steering")
            assert_same_selected_root_identity(self.seed, self.selected_root(passive=True))
        finally:
            self.quiet = False
        if self.contaminated:
            raise RuntimeError("quiet_contamination")
        write_json(
            self.evidence / "quiet.json",
            {
                "started": quiet_started,
                "ended": quiet_ended,
                "measured_s": quiet_ended - quiet_started,
                "required_s": self.profile.quiet_s,
                "last_route": self.last_route,
                "public_stream_closed": True,
                "contaminated": self.contaminated,
            },
        )
        if any(steering == native_user_text(entry.get("message", {})) for entry in self.journal()):
            raise RuntimeError("steering_baseline_duplicate")
        observer_deadline = min(deadline, start["monotonic"] + self.profile.wait_s)
        try:
            self.open_native_observer(steering, ended_path, observer_deadline)
        except (OSError, ValueError, RuntimeError) as exc:
            self.close_native_observer()
            if self.native_observer:
                self.observer_owner()
            assert_same_selected_root_identity(self.seed, self.selected_root(passive=True))
            assert_same_sources(self.source_before, source_snapshot(REPO))
            write_json(
                self.evidence / "native-observer-unavailable.json",
                {
                    "reason": sanitize(str(exc)),
                    "type": type(exc).__name__,
                    "observed_monotonic": time.monotonic(),
                },
            )
        post_started_at = time.monotonic()
        if self.native_observer and self.native_observer.state == "attached":
            self.native_observer.post_started_at = post_started_at
        dispatched = False
        try:
            self.send(steering, deadline=operation.deadline)
            dispatched = True
            self.claim(
                "http_dispatch", "VERIFIED", "202_framework_admission_only", "http-actions.jsonl"
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            self.record_failure(exc, "http_dispatch")

        def completed_wait() -> tuple[dict, dict, list[dict]] | None:
            if self.native_observer:
                self.observer_owner()
            assert_same_selected_root_identity(self.seed, self.selected_root(passive=True))
            if not ended_path.exists():
                return None
            end = json.loads(ended_path.read_text())
            if (
                end.get("nonce") != nonce
                or end.get("pid") != self.seed.roles.kernel.pid
                or end["monotonic"] - start["monotonic"] < self.profile.wait_s
            ):
                raise RuntimeError("actual_wait_duration_or_identity_mismatch")
            entries = [entry for entry in self.journal() if entry.get("id") not in native_before]
            pending_tool = native_tool(entries, code, None)
            if not pending_tool["results"]:
                return None
            tool = native_tool(entries, code, f"WAIT_DONE {nonce}")
            if tool["call"]["id"] != tool_start["call"]["id"]:
                raise RuntimeError("completed_wait_call_changed")
            return end, tool, entries

        end, tool, fresh_native = self.wait(
            completed_wait,
            "actual wait end and matching native tool result",
            self.remaining(),
        )
        completed = _CompletedWait(
            self.seed,
            nonce,
            end["pid"],
            start["monotonic"],
            end["monotonic"],
            tool["call"]["id"],
            tool["results"][0]["id"],
            time.monotonic(),
        )
        write_json(
            self.evidence / "wait-completed.json",
            {
                "start": start,
                "end": end,
                "tool": tool,
                "native": fresh_native,
                "completed": asdict(completed),
                "steering": steering,
                "expected": expected,
            },
        )
        self.claim(
            f"{self.profile.name}_long_wait",
            "VERIFIED",
            "real_running_wait_exceeded_measured_quiet_window",
            "quiet.json",
            "wait-completed.json",
            "wait-admission.json",
        )
        if not dispatched:
            self.close_native_observer()
            return
        reply = self.completed_reply(operation, "steering-consumed")
        fresh_items = reply["fresh_items"]
        fresh_native = reply["native"]
        native_observed_at = time.monotonic()
        users = [entry for entry in fresh_native if entry.get("id") == reply["native_user_id"]]
        replies = [entry for entry in fresh_native if entry.get("id") == reply["native_reply_id"]]
        if len(users) != 1 or len(replies) != 1:
            raise RuntimeError("steering_fresh_native_user_or_reply_missing")
        positions = {entry["id"]: i for i, entry in enumerate(fresh_native) if entry.get("id")}
        if (
            not positions[completed.result_id]
            < positions[users[0]["id"]]
            < positions[replies[0]["id"]]
        ):
            raise RuntimeError("steering_not_consumed_after_matching_wait_result")
        consumed = _SteeringConsumption(
            self.seed,
            steering,
            users[0]["id"],
            replies[0]["id"],
            native_observed_at,
            native_observed_at,
        )
        self.close_native_observer()
        assert_same_selected_root_identity(self.seed, self.selected_root(passive=True))
        assert_same_sources(self.source_before, source_snapshot(REPO))
        write_json(
            self.evidence / "steering-consumed.json",
            {
                "consumed": asdict(consumed),
                "native_user": users[0],
                "native_reply": replies[0],
                "fresh_items": fresh_items,
                "completed_wait": "wait-completed.json",
            },
        )
        self.claim(
            "native_steering_consumed_after_completion",
            "VERIFIED",
            "fresh_exact_user_after_wait_result_and_exact_native_public_reply",
            "steering-consumed.json",
            "wait-completed.json",
        )
        observer = self.native_observer
        native_verified = bool(
            observer
            and not observer.error
            and observer.receipt
            and _native_acceptance(observer.receipt, completed, consumed)
        )
        self.claim(
            "native_acceptance_while_tool_running",
            "VERIFIED" if native_verified else "NOT VERIFIED",
            "fresh_native_steering_queue_before_actual_end_with_later_consumption"
            if native_verified
            else "native_receipt_missing_invalid_or_observer_failed",
            "native-observer.json" if observer else "native-observer-unavailable.json",
            "steering-consumed.json",
            "wait-completed.json",
        )
        self.claim(
            "http_steering",
            "VERIFIED" if native_verified else "NOT VERIFIED",
            "202_admission_and_timely_native_queue_with_completed_consumption"
            if native_verified
            else "native_queue_acceptance_and_completed_consumption_not_verified",
            "http-actions.jsonl",
            "native-observer.json" if observer else "native-observer-unavailable.json",
            "wait-completed.json",
            "steering-consumed.json",
        )
        if self.profile.name == "pane":
            expired_samples = [
                sample
                for sample in samples
                if sample["output_at"] is not None
                and sample["wall"] - sample["output_at"] > 120
                and (sample["input_at"] is None or sample["wall"] - sample["input_at"] > 120)
            ]
            silent_span = 0.0
            first = None
            for sample in samples:
                if sample in expired_samples:
                    first = sample["monotonic"] if first is None else first
                    silent_span = max(silent_span, sample["monotonic"] - first)
                else:
                    first = None
            write_json(
                self.evidence / "pane-fallback.json",
                {
                    "expired_sample_count": len(expired_samples),
                    "silent_span_s": silent_span,
                    "required_full_opportunity_s": 150,
                    "fallback_contributed": len(expired_samples) != len(samples),
                    "clocks": samples,
                },
            )
            self.claim(
                "pane_status_protection",
                "NOT VERIFIED",
                "fallback_activity" if silent_span < 150 else "unobserved_interaction_clock",
                "pane-fallback.json",
            )
        self.attach(resume=True)
        resumed = self.selected_root()
        assert_same_selected_root_identity(self.seed, resumed)
        write_json(self.evidence / "root-resumed.json", selected_root_identity_record(resumed))
        terminal_operand = int(uuid.uuid4().hex[:10], 16)
        terminal_reply = str(terminal_operand + 23)
        self.message(
            "terminal-followup",
            f"Calculate {terminal_operand} + 23. Respond with only the decimal integer result.",
            terminal_reply,
            terminal=True,
            timeout=self.remaining(),
        )
        self.claim(
            "terminal_followup",
            "VERIFIED",
            "native_user_rendering_and_exact_fresh_assistant",
            "terminal-followup.json",
        )
        read_nonce = uuid.uuid4().hex
        read_path = self.evidence / "kernel-read.json"
        read_identity = self.kernel_expression.replace("NONCE", repr(read_nonce))
        read_code = (
            "provider_value['count'] += 1\n"
            f"Path({str(read_path)!r}).write_text(json.dumps({read_identity}))\n"
            "print(provider_value['token'], provider_value['count'], "
            "provider_value['socket'].fileno() >= 0)"
        )
        literal = f"{self.token} 42 True"
        self.message(
            "memory-read",
            "Execute this exact code using ipython once. Do not reinitialize any variable. "
            "Then reply with exactly the printed line and nothing else.\n" + read_code,
            literal,
            terminal=True,
            code=read_code,
            timeout=self.remaining(),
        )
        actual = read_actual_kernel_identity(read_path)
        final = self.selected_root()
        self.qualify_kernel(actual, read_nonce, final)
        assert_same_selected_root_identity(self.seed, final)
        payload = json.loads(read_path.read_text())
        if (
            actual.pid != self.actual_kernel.pid
            or payload.get("token") != self.token
            or payload.get("count") != 42
            or payload.get("socket_live") is not True
        ):
            raise RuntimeError("actual_memory_continuity_mismatch")
        write_json(self.evidence / "root-final.json", selected_root_identity_record(final))
        self.claim(
            "selected_root_continuity",
            "VERIFIED",
            "same_root_binding_and_all_role_pid_start_identities",
            "root-seed.json",
            "root-detached.json",
            "root-resumed.json",
            "root-final.json",
            "kernel-read.json",
        )
        if time.monotonic() > deadline:
            raise RuntimeError("wait_scenario_deadline")
        verifier_expected = (
            self.request.expected_memory if self.request.expected_memory is not None else literal
        )
        write_json(
            self.evidence / "memory-comparison.json",
            {
                "normal_literal": literal,
                "verifier_expected": verifier_expected,
                "actual_normal_tool_completed": True,
                "matches": literal == verifier_expected,
            },
        )
        if literal != verifier_expected:
            raise RuntimeError("memory_read_mismatch")
        self.claim(
            "memory_read",
            "VERIFIED",
            "exact_normal_literal_after_real_tool_completion",
            "memory-comparison.json",
            "memory-read.json",
            "kernel-read.json",
        )

    def private_sockets(self) -> list[str]:
        if self.bridge_roots:
            self.owned_paths(self.session_id)
        paths = set()
        for root in self.bridge_roots | {self.runtime}:
            if root in self.bridge_roots:
                descriptor = self.credential_directory(root / "agent" / "auth.json")
                if descriptor is not None:
                    os.close(descriptor)
            if root.exists():
                for path in root.rglob("*.sock"):
                    if (
                        path.exists()
                        and not path.is_symlink()
                        and stat.S_ISSOCK(path.stat().st_mode)
                    ):
                        paths.add(str(path))
        paths.update(str(path) for path in self.terminal_sockets if path.exists())
        return sorted(paths)

    @staticmethod
    def _credential_path(path: Path) -> tuple[Path, dict[Path, tuple[int, int]]]:
        directory = Path(os.path.abspath(path.parent))
        aliases = {}
        anchor = Path("/tmp")
        if directory.is_relative_to(anchor):
            metadata = anchor.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                if os.readlink(anchor) != "private/tmp":
                    raise RuntimeError("owned_credential_temp_anchor_untrusted")
                aliases[anchor] = (metadata.st_dev, metadata.st_ino)
                directory = Path("/private/tmp") / directory.relative_to(anchor)
        return directory / path.name, aliases

    def credential_directory(self, path: Path) -> int | None:
        canonical, _ = self._credential_path(path)
        target = self.credential_targets[canonical]
        if self.runtime_owner.removed is not None and canonical.is_relative_to(self.runtime):
            if (
                not target.copy_absence_required
                or target.directories.get(self.runtime) != self.runtime_owner.removed.root_identity
            ):
                raise RuntimeError("runtime_credential_retirement_mismatch")
            self.runtime_owner.check_removed()
            return None
        root = target.path.parent.parent
        receipt = self._retired_trees.get(root)
        if receipt is None:
            try:
                return _open_witnessed_directory(target, target.path.parent)
            except RuntimeError as exc:
                if exc.args != ("owned_credential_directory_missing",):
                    raise
                receipt = self._maintenance_retirement(root)
                if receipt is None:
                    raise
                self._check_retired(target, root, receipt)
                self._retired_trees[root] = receipt
                write_json(self.evidence / "maintenance-retirement.json", asdict(receipt))
                return None
        self._check_retired(target, root, receipt)
        return None

    def _check_retired(
        self, target: _CredentialTarget, root: Path, receipt: _RetiredOwnedTree
    ) -> None:
        if (
            not target.copy_absence_required
            or receipt.session_id != self.session_id
            or target.directories.get(root) != receipt.root_identity
            or target.directories.get(root.parent) != receipt.parent_identity
        ):
            raise RuntimeError("owned_tree_retirement_mismatch")
        descriptor = _open_witnessed_directory(target, root.parent)
        if descriptor is None:
            raise RuntimeError("owned_credential_directory_missing")
        try:
            try:
                os.stat(root.name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise RuntimeError("owned_credential_root_recreated")
        finally:
            os.close(descriptor)

    def _record_root_admission(self, root: Path, descriptor: int) -> None:
        self._root_admissions.pop(root, None)
        try:
            config_fd = os.open(
                "config.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
            )
            with os.fdopen(config_fd) as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    return
                config = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(config, dict):
            return
        admission = config.get("nativeAdmission")
        if not isinstance(admission, dict) or config.get("serverUrl") != self.url:
            return
        session, epoch = admission.get("source_id"), admission.get("epoch")
        if session == self.session_id and type(epoch) is str and epoch:
            self._root_admissions[root] = (session, epoch)

    def _maintenance_retirement(self, root: Path) -> _RetiredOwnedTree | None:
        """Require absence, a maintenance query, and an exact-epoch deletion confirmation."""
        attempt = self._retained_roots.get(root)
        admission = self._root_admissions.get(root)
        if (
            attempt is None
            or attempt.http_status != 200
            or admission is None
            or admission[0] != attempt.session_id
        ):
            return None
        deleted = f"DELETE /v1/sessions/{attempt.session_id}"
        queried = f"POST /v1/sessions/{attempt.session_id}/native-admission/status"
        after_delete = False
        for line in self._owned_server_log().splitlines():
            match = _ACCESS_LINE.search(line)
            if match is None or match["status"] != "200":
                continue
            after_delete = after_delete or match["request"] == deleted
            if after_delete and match["request"] == queried:
                receipt = _RetiredOwnedTree(
                    root,
                    attempt.root_identity,
                    attempt.parent_identity,
                    attempt.session_id,
                    attempt.delete_step,
                    attempt.http_status,
                    attempt.event_flags,
                    "maintenance_retired",
                    sanitize(line),
                    hashlib.sha256(admission[1].encode()).hexdigest(),
                )
                target = self.credential_targets[root / "agent/auth.json"]
                self._check_retired(target, root, receipt)
                try:
                    response = asyncio.run(
                        self._request_http(
                            "POST",
                            queried.removeprefix("POST "),
                            {"epoch": admission[1]},
                            5,
                            time.monotonic() + 5,
                        )
                    )
                    body = response.json()
                except (httpx.HTTPError, ValueError, RuntimeError):
                    return None
                if (
                    response.status_code == 200
                    and type(body) is dict
                    and body.keys() == {"deleted"}
                    and body["deleted"] is True
                ):
                    return replace(receipt, status_response=body)
                return None
        return None

    def _owned_server_log(self) -> str:
        directory = self.runtime / "data/logs/server"
        descriptor = _open_witnessed_directory(self.runtime_owner.target, directory)
        if descriptor is None:
            return ""
        try:
            names = [name for name in os.listdir(descriptor) if name.startswith("server-")]
            if len(names) != 1:
                return ""
            log = os.open(names[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        with os.fdopen(log, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return ""
            return handle.read().decode(errors="replace")

    def register_owned_credential(self, path: Path) -> None:
        canonical, aliases = self._credential_path(path)
        source = self.request.auth_source.resolve()
        if canonical == source:
            raise RuntimeError("owned_credential_is_original_source")
        if canonical not in self.credential_targets:
            if (
                self.runtime_owner.removed is not None and canonical.is_relative_to(self.runtime)
            ) or any(canonical.is_relative_to(root) for root in self._retired_trees):
                raise RuntimeError("owned_credential_new_retired_descendant")
            self.credential_targets[canonical] = _CredentialTarget(canonical, {}, aliases)
        else:
            target = self.credential_targets[canonical]
            for alias, identity in aliases.items():
                if alias in target.aliases and target.aliases[alias] != identity:
                    raise RuntimeError("owned_credential_temp_anchor_replaced")
                target.aliases[alias] = identity
        self.credential_copies.add(path)
        descriptor = self.credential_directory(path)
        if descriptor is not None:
            os.close(descriptor)

    def remove_owned_credentials(self, errors: list[str]) -> list[str]:
        from omnigent.harnesses.prime_native import bridge

        removed = []
        previous = bridge._DATA_ROOT
        try:
            bridge._DATA_ROOT = self.runtime / "data"
            for path in sorted(self.credential_copies):
                descriptor = None
                try:
                    descriptor = self.credential_directory(path)
                    canonical, _ = self._credential_path(path)
                    target = self.credential_targets[canonical]
                    if descriptor is not None:
                        if not target.copy_absence_required and path != self.prime / "auth.json":
                            bridge.PrimeRuntimePaths(path.parent.parent).validate_existing()
                        try:
                            metadata = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            target.copy_absence_required = True
                        else:
                            if target.copy_absence_required:
                                raise RuntimeError("owned_credential_copy_recreated")
                            if stat.S_ISLNK(metadata.st_mode):
                                raise RuntimeError("owned_credential_copy_is_symlink")
                            target.copy_absence_required = True
                            os.unlink(path.name, dir_fd=descriptor)
                        try:
                            os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise RuntimeError("owned_credential_copy_recreated")
                        checked = self.credential_directory(path)
                        if checked is None:
                            raise RuntimeError("owned_credential_directory_missing")
                        os.close(checked)
                    removed.append(str(path))
                except FINALIZATION_ERRORS as exc:
                    errors.append(
                        "remove credential copy: " + type(exc).__name__ + ": " + sanitize(str(exc))
                    )
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
        finally:
            bridge._DATA_ROOT = previous
        return removed

    def _delete_owned_session(self, result: dict, *, unavailable: bool = False) -> None:
        session = self.session_id
        if type(session) is not str or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session
        ):
            session = None
        current = _RetirementAttempt(self.runtime_owner.allocation_id, session)
        attempts = [current]
        observations = {}
        try:
            if self.session_id and session is None:
                raise RuntimeError("owned_tree_session_identity_invalid")
            if not self.session_id:
                current.branch = "no_session"
                return
            if unavailable:
                current.branch = "api_unavailable"
                raise RuntimeError("owned_API_not_alive_for_public_cleanup")
            if len(self.bridge_roots) > 64:
                current.branch = "root_limit"
                raise RuntimeError("owned_tree_retirement_root_limit")
            errors = result["errors"]
            prior_errors = len(errors)
            self.remove_owned_credentials(errors)
            if len(errors) != prior_errors or any(
                "remove credential copy:" in error for error in errors
            ):
                raise RuntimeError("owned_tree_credential_cleanup_failed")
            if sys.platform != "darwin" or os.uname().machine != "arm64":
                raise RuntimeError("owned_tree_deletion_platform_unqualified")
            targets = tuple(self.credential_targets)
            roots = {}
            for path in sorted(self.bridge_roots):
                canonical, _ = self._credential_path(path / "agent" / "auth.json")
                target = self.credential_targets[canonical]
                root = canonical.parent.parent
                current = _RetirementAttempt(
                    self.runtime_owner.allocation_id,
                    session,
                    root_identity=target.directories[root],
                    parent_identity=target.directories[root.parent],
                    external_root=not root.is_relative_to(self.runtime),
                )
                attempts.append(current)
                observations[root] = current
                if self.request.auth_source.resolve().is_relative_to(root):
                    raise RuntimeError("owned_tree_contains_original_source")
                if root in self._retired_trees:
                    self.credential_directory(canonical)
                    current.branch = "previously_retired"
                else:
                    roots[root] = target
            with ExitStack() as handles:
                queue = select.kqueue()
                handles.callback(queue.close)
                pins = {}
                flags = select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE
                for root, target in roots.items():
                    current = observations[root]
                    current.phase = "watch_admission"
                    if any(
                        not member.copy_absence_required
                        for member in self.credential_targets.values()
                        if member.path.is_relative_to(root)
                    ):
                        raise RuntimeError("owned_tree_copy_absence_unproved")
                    parent = _open_witnessed_directory(target, root.parent)
                    if parent is None:
                        raise RuntimeError("owned_credential_directory_missing")
                    handles.callback(os.close, parent)
                    descriptor = _open_witnessed_directory(target, root)
                    if descriptor is None:
                        raise RuntimeError("owned_credential_directory_missing")
                    handles.callback(os.close, descriptor)
                    self._record_root_admission(root, descriptor)
                    pins[root] = (parent, descriptor)
                    queue.control(
                        [
                            select.kevent(
                                descriptor,
                                filter=select.KQ_FILTER_VNODE,
                                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                fflags=flags,
                            )
                        ],
                        0,
                        0,
                    )
                if queue.control([], max(1, len(pins)), 0):
                    raise RuntimeError("owned_tree_event_before_delete")
                for target in roots.values():
                    checked = self.credential_directory(target.path)
                    if checked is None:
                        raise RuntimeError("owned_credential_directory_missing")
                    try:
                        try:
                            os.stat(target.path.name, dir_fd=checked, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise RuntimeError("owned_credential_copy_recreated")
                    finally:
                        os.close(checked)
                for observation in attempts:
                    observation.phase = "delete_request"
                    observation.delete_step = len(result["steps"])
                response = self.request_http(
                    "DELETE", f"/v1/sessions/{self.session_id}", timeout=60
                )
                for observation in attempts:
                    observation.http_status = response.status_code
                    observation.phase = "delete_response"
                step = len(result["steps"])
                result["steps"].append(
                    {"delete_session": self.session_id, "status": response.status_code}
                )
                response.raise_for_status()
                if tuple(self.credential_targets) != targets:
                    raise RuntimeError("owned_tree_admission_changed_during_delete")
                for observation in attempts:
                    observation.phase = "event_validation"
                events = {}
                for event in queue.control([], max(1, len(pins)), 0):
                    if (
                        event.ident not in {descriptor for _, descriptor in pins.values()}
                        or event.filter != select.KQ_FILTER_VNODE
                        or event.flags
                        & ~(select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR)
                        or event.fflags & (select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
                        or event.fflags
                        & ~(
                            flags
                            | select.KQ_NOTE_WRITE
                            | select.KQ_NOTE_EXTEND
                            | select.KQ_NOTE_ATTRIB
                            | select.KQ_NOTE_LINK
                        )
                        or event.data != 0
                        or event.ident in events
                    ):
                        raise RuntimeError("owned_tree_deletion_event_invalid")
                    events[event.ident] = event.fflags
                for root, target in roots.items():
                    current = observations[root]
                    current.phase = "identity_validation"
                    parent, descriptor = pins[root]
                    current.event_flags = events.get(descriptor, 0)
                    checked = _open_witnessed_directory(target, root.parent)
                    if checked is None:
                        raise RuntimeError("owned_credential_directory_missing")
                    try:
                        for fd, name in ((parent, root.parent), (descriptor, root)):
                            metadata = os.fstat(fd)
                            matches = (metadata.st_dev, metadata.st_ino) == target.directories[
                                name
                            ]
                            if name == root:
                                current.held_root_matches = matches
                            else:
                                current.held_parent_matches = matches
                            if not matches:
                                raise RuntimeError("owned_credential_directory_replaced")
                        current.phase = "name_observation"
                        try:
                            metadata = os.stat(root.name, dir_fd=checked, follow_symlinks=False)
                        except FileNotFoundError:
                            current.name_state = "absent"
                            if not events.get(descriptor, 0) & select.KQ_NOTE_DELETE:
                                raise RuntimeError("owned_tree_deletion_event_missing") from None
                            receipt = _RetiredOwnedTree(
                                root,
                                target.directories[root],
                                target.directories[root.parent],
                                self.session_id,
                                step,
                                response.status_code,
                                events[descriptor],
                            )
                            result.setdefault("retired_owned_trees", []).append(asdict(receipt))
                            self._retired_trees[root] = receipt
                            current.phase = "complete"
                            current.branch = "retired"
                        else:
                            current.name_state = (
                                "original"
                                if (metadata.st_dev, metadata.st_ino) == target.directories[root]
                                else "replaced"
                            )
                            if current.name_state == "replaced":
                                raise RuntimeError("owned_credential_directory_replaced")
                            if events.get(descriptor, 0) & select.KQ_NOTE_DELETE:
                                raise RuntimeError("owned_credential_root_recreated")
                            self.register_owned_credential(target.path)
                            credential = self.credential_directory(target.path)
                            if credential is None:
                                raise RuntimeError("owned_credential_directory_missing")
                            try:
                                try:
                                    os.stat(
                                        target.path.name, dir_fd=credential, follow_symlinks=False
                                    )
                                except FileNotFoundError:
                                    pass
                                else:
                                    raise RuntimeError("owned_credential_copy_recreated")
                            finally:
                                os.close(credential)
                            current.phase = "complete"
                            current.branch = "retained_original"
                            self._retained_roots[root] = current
                    finally:
                        os.close(checked)
        except FINALIZATION_ERRORS as exc:
            reason = exc.args[0] if type(exc) is RuntimeError and exc.args else None
            branch = (
                reason
                if type(reason) is str
                and reason
                in {
                    "owned_API_not_alive_for_public_cleanup",
                    "owned_credential_copy_recreated",
                    "owned_credential_directory_missing",
                    "owned_credential_directory_replaced",
                    "owned_credential_root_recreated",
                    "owned_tree_admission_changed_during_delete",
                    "owned_tree_contains_original_source",
                    "owned_tree_copy_absence_unproved",
                    "owned_tree_credential_cleanup_failed",
                    "owned_tree_deletion_event_invalid",
                    "owned_tree_deletion_event_missing",
                    "owned_tree_deletion_platform_unqualified",
                    "owned_tree_event_before_delete",
                    "owned_tree_retirement_root_limit",
                    "owned_tree_session_identity_invalid",
                }
                else "unclassified_failure"
            )
            if isinstance(exc, httpx.HTTPStatusError):
                branch = "delete_http_error"
            elif isinstance(exc, httpx.RequestError):
                branch = "delete_transport_error"
            current.branch = branch
            raise
        finally:
            records = [asdict(item) for item in (attempts[1:] or attempts)]
            result["retirement_attempts"] = records
            write_json(self.evidence / "retirement-attempts.json", records)

    def _capture_owned_logs(self) -> tuple[Path, ...]:
        owner = self.runtime_owner
        captures = []
        logs = self.runtime / "data/logs"
        budget = _TraversalBudget(time.monotonic() + 30)

        def capture(directory: Path, fd: int) -> None:
            owner._check_directory(fd, directory)
            budget.visit(len(directory.relative_to(logs).parts))
            with os.scandir(fd) as children:
                names = []
                for entry in children:
                    budget.visit(len(directory.relative_to(logs).parts) + 1)
                    names.append(entry.name)
            for name in names:
                owner._check_directory(fd, directory)
                metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (
                    metadata.st_uid != os.getuid()
                    or metadata.st_dev != owner.target.directories[self.runtime][0]
                ):
                    owner._rejection(
                        "capture", "runtime_capture_not_owned", directory / name, metadata
                    )
                    raise RuntimeError("runtime_capture_not_owned")
                if directory / name == self.runtime / _CLI_ALIAS:
                    captures.append(owner._alias(fd, directory / name, metadata, "capture"))
                    continue
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if stat.S_ISDIR(metadata.st_mode):
                    flags |= os.O_DIRECTORY
                elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    owner._rejection(
                        "capture", "runtime_capture_unsafe_file", directory / name, metadata
                    )
                    raise RuntimeError("runtime_capture_unsafe_file")
                else:
                    budget.visit(len(directory.relative_to(logs).parts) + 1, metadata.st_size)
                child = os.open(name, flags, dir_fd=fd)
                try:
                    held = os.fstat(child)
                    if _leaf_identity(held) != _leaf_identity(metadata):
                        raise RuntimeError("runtime_capture_replaced")
                    if stat.S_ISDIR(held.st_mode):
                        owner.target.directories.setdefault(
                            directory / name, (held.st_dev, held.st_ino)
                        )
                        capture(directory / name, child)
                    else:
                        owner._check_directory(fd, directory)
                        with os.fdopen(os.dup(child), "rb") as handle:
                            payload = handle.read(metadata.st_size + 1)
                        if len(payload) != metadata.st_size or _leaf_identity(
                            os.fstat(child)
                        ) != _leaf_identity(metadata):
                            raise RuntimeError("runtime_capture_changed")
                        text = sanitize(payload.decode(errors="replace"))
                        destination = (
                            self.evidence / "owned-logs" / (directory / name).relative_to(logs)
                        )
                        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        destination.write_text(text)
                        destination.chmod(0o600)
                        captures.append(destination)
                finally:
                    os.close(child)

        try:
            descriptor = _open_witnessed_directory(owner.target, logs)
            if descriptor is None:
                return ()
            try:
                capture(logs, descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            owner._rejection("capture", "runtime_capture_failed")
            raise
        return tuple(captures)

    def finalize_owned_credentials(self, errors: list[str]) -> None:
        if self.runtime_owner.removed is None and not self.runtime_owner.errors:
            if self.session_id or (self.server and self.server.poll() is None and self.terminal):
                try:
                    self.recover_owned_session()
                except FINALIZATION_ERRORS as exc:
                    errors.append("credential recovery: " + sanitize(str(exc)))
        self.remove_owned_credentials(errors)

    def cleanup(self, external_settlement: _ExternalSettlement) -> Observation:
        if self.runtime_owner.removed is not None or self.runtime_owner.errors:
            errors = list(
                dict.fromkeys([*self.runtime_owner.errors, *(self.cleanup_errors or [])])
            )
            self.finalize_owned_credentials(errors)
            self.cleanup_errors = errors
            return Observation(
                "owned_cleanup",
                "FAILED" if errors else "VERIFIED",
                ("cleanup.json",),
                "cleanup_error" if errors else "exact_owned_absence",
            )
        try:
            result = self._cleanup_live(external_settlement)
        except FINALIZATION_ERRORS as exc:
            self.runtime_owner.errors.append(
                "cleanup failed: " + type(exc).__name__ + ": " + sanitize(str(exc))
            )
            raise
        if result.status != "VERIFIED":
            self.runtime_owner.errors.extend(
                error
                for error in (self.cleanup_errors or [])
                if error not in self.runtime_owner.errors
            )
        return result

    def _cleanup_live(self, external_settlement: _ExternalSettlement) -> Observation:
        self.quiet = False
        self.scenario_deadline = None
        result = {"steps": [], "errors": [], "forced_native_fallback": False}
        errors = self.cleanup_errors = result["errors"]

        def attempt(name, action):
            try:
                return action()
            except FINALIZATION_ERRORS as exc:
                errors.append(name + ": " + type(exc).__name__ + ": " + sanitize(str(exc)))
                return None

        attempt("native observer close", self.close_native_observer)
        removed = self.remove_owned_credentials(errors)
        attempt("cleanup progress", lambda: self.progress("cleanup"))
        attempt("stream close", self.close_stream)
        if self.server and self.server.poll() is None and (self.session_id or self.terminal):
            attempt("recover exact session root", self.recover_owned_session)
        attempt("prior census", self.census)

        def stop_session() -> None:
            idle_claim = next(
                (
                    item
                    for item in self.observations
                    if item.claim == f"{self.profile.name}_idle_negative"
                    and item.status == "VERIFIED"
                ),
                None,
            )
            if self.kind == "idle" and idle_claim and self.expired and self.seed:
                expiry = json.loads((self.evidence / "idle-expiry.json").read_text())
                decision = json.loads((self.evidence / "idle-decision.json").read_text())
                if (
                    expiry["decision"] == decision
                    and {tuple(identity) for identity in expiry["expired"]} == self.expired
                    and not any(identity_alive(role) for role in as_role_tuple(self.seed))
                    and (self.profile.name != "runner" or not identity_alive(self.runner))
                ):
                    result["steps"].append(
                        {
                            "stop_skipped_controlled_idle_expiry": self.session_id,
                            "expired": sorted(self.expired),
                            "evidence": ["idle-decision.json", "idle-expiry.json"],
                            "exact_native_roles_absent": True,
                        }
                    )
                    return
            response = self.request_http(
                "POST",
                f"/v1/sessions/{self.session_id}/events",
                {"type": "stop_session", "data": {}},
                timeout=60,
            )
            result["steps"].append(
                {
                    "stop_session": self.session_id,
                    "status": response.status_code,
                    "response": sanitize(response.text),
                }
            )
            if response.status_code not in (200, 202):
                response.raise_for_status()
                raise RuntimeError(f"public_stop_unexpected_status {response.status_code}")

        def delete_session() -> bool:
            self._delete_owned_session(result)
            return True

        if self.session_id and self.server and self.server.poll() is None:
            if not attempt("public DELETE", delete_session):
                attempt("public stop_session", stop_session)
        else:
            attempt(
                "public DELETE unavailable",
                lambda: self._delete_owned_session(result, unavailable=True),
            )
        if self.terminal:
            attachment = attempt("close attachment", self.close_attachment)
            if attachment:
                result["steps"].append({"attachment_exit": attachment})
            else:
                result["forced_native_fallback"] = True
                errors.append("forced_exact_attachment_cleanup_required")

                def force_attachment() -> None:
                    identity = self.attachment_identity
                    if identity is None or identity.pid != self.terminal.pid:
                        raise RuntimeError("exact_attachment_identity_missing")
                    self.attachment_drain.stop()
                    if identity_alive(identity):
                        self.terminal.close(force=True)

                attempt("force attachment", force_attachment)

        def host_stop() -> None:
            if self.foreground_host:
                identity = self.foreground_host_identity
                if identity is None:
                    raise RuntimeError("exact_foreground_host_identity_missing")
                if self.foreground_host.poll() is not None:
                    result["steps"].append({"foreground_host_already_exited": asdict(identity)})
                    return
                host = self.foreground_host_record()
                if host is None:
                    raise RuntimeError("exact_owned_host_record_missing")
                self.host_record, self.host_identity = host
            if not self.host_identity:
                records = [
                    json.loads(path.read_text())
                    for path in (self.runtime / "data/daemons").glob("*.json")
                    if not path.is_symlink()
                ]
                matches = [record for record in records if record.get("target") == self.url]
                if len(matches) != 1:
                    raise RuntimeError("exact_owned_host_record_missing")
                self.host_record = matches[0]
                self.host_identity = qualify_owned_host_record(
                    matches[0], self.url, self.runtime / "data", psutil.Process(matches[0]["pid"])
                )
            identity = ProcessIdentity(
                self.host_identity["pid"], self.host_identity["started"], ()
            )
            if not identity_alive(identity):
                result["steps"].append(
                    {"host_already_exited": self.host_identity, "independent_exact_absence": True}
                )
                return
            qualified = qualify_owned_host_record(
                self.host_record, self.url, self.runtime / "data", psutil.Process(identity.pid)
            )
            if not self.server or self.server.poll() is not None:
                raise RuntimeError("owned_API_not_alive_for_host_cleanup")
            self.guard("qualified_host_stop")
            argv = [
                str(REPO / ".venv/bin/omnigent"),
                "host",
                "stop",
                "--server",
                self.url,
                "--daemon-only",
            ]
            completed = subprocess.run(
                argv,
                cwd=self.workspace,
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=40,
            )
            result["steps"].append(
                {
                    "host_stop": qualified,
                    "argv": argv,
                    "exit": completed.returncode,
                    "stdout": sanitize(completed.stdout),
                    "stderr": sanitize(completed.stderr),
                }
            )
            if completed.returncode:
                raise RuntimeError("qualified_host_stop_failed")

        if self.server:
            attempt("host stop", host_stop)

        def reap_host() -> None:
            child = self.foreground_host
            if child:
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    result["forced_native_fallback"] = True
                    errors.append("forced_exact_foreground_host_cleanup_required")
                    identity = self.foreground_host_identity
                    if identity is None or identity.pid != child.pid:
                        raise RuntimeError("exact_foreground_host_identity_missing") from None
                    if identity_alive(identity):
                        psutil.Process(identity.pid).terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if identity_alive(identity):
                            psutil.Process(identity.pid).kill()
                        child.wait(timeout=5)
                result["steps"].append(
                    {
                        "foreground_host_exit": child.returncode,
                        "identity": self.foreground_host_state(),
                    }
                )
            if self.host_log_handle and not self.host_log_handle.closed:
                self.host_log_handle.close()
                path = Path(self.host_log_handle.name)
                path.write_text(sanitize(path.read_text(errors="replace")))
                path.chmod(0o600)

        attempt("reap foreground host", reap_host)

        def maintenance_host() -> None:
            roots = sorted(str(root) for root in self._retained_roots if os.path.lexists(root))
            if not roots or not self.server or self.server.poll() is not None:
                return
            # Only host maintenance reclaims a root whose runner exited before DELETE,
            # and a host start is the product event that schedules it.
            restart = result["maintenance_host_restart"] = {
                "reason": "retained_root_after_delete",
                "roots": roots,
                "requested_at": time.time(),
            }
            self.start_host("host-maintenance")
            restart["host_ready_at"] = time.time()
            deadline = time.monotonic() + MAINTENANCE_RECLAIM_TIMEOUT_S
            while any(map(os.path.lexists, roots)) and time.monotonic() < deadline:
                time.sleep(0.2)
            restart["roots_absent"] = not any(map(os.path.lexists, roots))
            restart["finished_at"] = time.time()

        attempt("maintenance host restart", maintenance_host)
        if "maintenance_host_restart" in result:
            attempt("maintenance host stop", host_stop)
            attempt("reap maintenance host", reap_host)
        for root in self._retained_roots:
            if not os.path.lexists(root):
                attempt(
                    "maintenance retirement proof",
                    lambda root=root: self.credential_directory(root / "agent/auth.json"),
                )
        attempt(
            "native absence",
            lambda: wait_for(
                lambda: (
                    not [
                        record
                        for record in self.census()
                        if record["pid"] != (self.server.pid if self.server else None)
                    ]
                ),
                "owned native and host absence",
                20,
            ),
        )
        if self.server and self.server.poll() is None:
            self.guard("owned_api_stop_last")
            self.server.terminate()
            try:
                self.server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                errors.append("owned_api_force_stop_required")
                self.server.kill()
                self.server.wait(timeout=5)
        if self.server_thread:
            self.server_thread.join(timeout=3)
        if self.server_thread and self.server_thread.is_alive():
            errors.append("server_log_reader_survives")
        elif self.server and self.server.stdout:
            attempt("server output close", self.server.stdout.close)
        if self.server_capture_error:
            errors.append("server_capture_failed: " + self.server_capture_error)
        if self.server and not (self.evidence / "server.log").is_file():
            errors.append("server_capture_missing")
        remaining = attempt("final census", self.census)
        if remaining:
            result["forced_native_fallback"] = True
            errors.append("forced_exact_owned_process_cleanup_required")
            for record in remaining:

                def terminate(record=record):
                    identity = ProcessIdentity(
                        record["pid"], record["started"], tuple(record.get("argv", ()))
                    )
                    if identity_alive(identity):
                        psutil.Process(identity.pid).terminate()

                attempt("exact-owned terminate", terminate)
            attempt(
                "forced drain",
                lambda: wait_for(lambda: not self.census(), "forced exact-owned drain", 5),
            )
            for record in self.census():

                def kill(record=record):
                    identity = ProcessIdentity(
                        record["pid"], record["started"], tuple(record.get("argv", ()))
                    )
                    if identity_alive(identity):
                        psutil.Process(identity.pid).kill()

                attempt("exact-owned kill", kill)
        result["final_owned_census"] = attempt("final owned absence", self.census)
        if self.foreground_host and self.foreground_host.poll() is not None:
            attempt("final foreground host reap", reap_host)
        result["foreground_host"] = self.foreground_host_state()
        if self.host_log_handle and not self.host_log_handle.closed:
            errors.append("foreground_host_log_handle_open")
        lingering_sockets = self.private_sockets()
        if lingering_sockets and result["final_owned_census"] == [] and not self.census_errors:
            errors.append("stale_owned_socket_cleanup_required")
            for filename in lingering_sockets:

                def remove_stale_socket(filename=filename):
                    self.guard("owned_socket_absence_check")
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.settimeout(1)
                        try:
                            connection.connect(filename)
                        except OSError as exc:
                            if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                                raise
                        else:
                            raise RuntimeError("owned_socket_still_accepts_connections")
                    Path(filename).unlink(missing_ok=True)
                    result["steps"].append({"removed_refusing_owned_socket": filename})

                attempt("remove stale owned socket", remove_stale_socket)
        result["private_supervisor_sockets"] = self.private_sockets()
        if result["private_supervisor_sockets"]:
            errors.append("private_supervisor_socket_survives")
        if self.session_id:
            attempt("validate exact credential root", lambda: self.owned_paths(self.session_id))
        final_removed = self.remove_owned_credentials(errors)
        removed.extend(final_removed)
        result["credential_copies_removed"] = sorted(set(removed))
        result["credential_copies_absent"] = set(final_removed) == set(
            map(str, self.credential_copies)
        )
        if not result["credential_copies_absent"]:
            errors.append("private_credential_copy_survives")

        def source_integrity() -> dict:
            after = source_snapshot(REPO)
            write_json(self.evidence / "source-after.json", asdict(after))
            if (
                sha256(Path(__file__))
                != json.loads((self.evidence / "artifact.json").read_text())["driver_sha256"]
            ):
                raise RuntimeError("provider_probe_source_drift")
            return assert_same_sources(self.source_before, after)

        if self.source_before:
            result["source_integrity"] = attempt("source integrity", source_integrity)
        if self.auth_signature:

            def source_unchanged() -> bool:
                source = self.request.auth_source
                observed = (
                    source.stat().st_ino,
                    source.stat().st_mode,
                    hashlib.sha256(source.read_bytes()).digest(),
                )
                if observed != self.auth_signature:
                    raise RuntimeError("original_auth_source_changed")
                return True

            result["auth_source_unchanged"] = attempt("auth source unchanged", source_unchanged)

        def baseline_unchanged() -> bool:
            for record in self.baseline:
                if not identity_alive(
                    ProcessIdentity(record["pid"], record["started"], tuple(record["argv"]))
                ):
                    raise RuntimeError("unrelated_user_process_identity_changed")
            return True

        result["unrelated_process_identities_preserved"] = attempt(
            "unrelated process identities", baseline_unchanged
        )
        captures = list(self._capture_owned_logs())
        if self.stream_thread and self.stream_thread.is_alive():
            errors.append("stream_reader_survives")
        errors.extend(sorted(self.census_errors))
        if self.contaminated:
            errors.append("quiet_contamination")
        for root in self.bridge_roots:
            canonical, _ = self._credential_path(root / "agent/auth.json")
            owned_root = canonical.parent.parent
            if (
                not owned_root.is_relative_to(self.runtime)
                and owned_root not in self._retired_trees
            ):
                errors.append("compact_runtime_removal_unproved: " + str(owned_root))
        if (
            not errors
            and result["final_owned_census"] == []
            and not result["forced_native_fallback"]
        ):
            write_json(self.evidence / "runtime-settlement.json", result, immutable=True)
            captures.append(self.evidence / "runtime-settlement.json")
            attempt(
                "runtime removal",
                lambda: self.runtime_owner.finish(external_settlement, tuple(captures)),
            )
        self.finalize_owned_credentials(errors)
        result["runtime_removed"] = self.runtime_owner.removed is not None
        errors.extend(error for error in self.runtime_owner.errors if error not in errors)
        write_json(self.evidence / "cleanup.json", result)
        passed = (
            not errors
            and result["runtime_removed"]
            and result["final_owned_census"] == []
            and not result["forced_native_fallback"]
        )
        return Observation(
            "owned_cleanup",
            "VERIFIED" if passed else "FAILED",
            ("cleanup.json",),
            "exact_owned_absence" if passed else "cleanup_error",
        )

    def qualify(self) -> _CaseDraft:
        required = ["launch", "kernel_seed", "selector_eligible"]
        if self.kind == "idle":
            required.append(f"{self.profile.name}_idle_negative")
        elif self.kind == "positive":
            required += [
                f"{self.profile.name}_long_wait",
                "http_steering",
                "http_dispatch",
                "native_acceptance_while_tool_running",
                "native_steering_consumed_after_completion",
                "terminal_followup",
                "selected_root_continuity",
                "memory_read",
            ]
        failure = None
        try:
            self.start()
            self.seed_memory()
            if self.kind == "idle":
                self.idle_control()
            elif self.kind == "positive":
                self.running_wait()
        except FINALIZATION_ERRORS as exc:
            self.record_failure(exc, "case")
        finally:
            try:
                cleanup = self.cleanup(
                    _ExternalSettlement(self.runtime_owner.allocation_id, "no_fixture", ())
                )
            except FINALIZATION_ERRORS as exc:
                credential_errors = []
                observer_errors = []
                try:
                    self.close_native_observer()
                except FINALIZATION_ERRORS as observer_error:
                    observer_errors.append(
                        type(observer_error).__name__ + ": " + sanitize(str(observer_error))
                    )
                self.finalize_owned_credentials(credential_errors)
                write_json(
                    self.evidence / "cleanup-finalization.json",
                    {
                        "error": sanitize(str(exc)),
                        "traceback": sanitize(traceback.format_exc()),
                        "emergency_credential_removal_errors": credential_errors,
                        "emergency_observer_close_errors": observer_errors,
                        "cleanup_errors": self.cleanup_errors,
                        "native_observer_close_error": self.native_observer_close_error,
                    },
                )
                cleanup = Observation(
                    "owned_cleanup",
                    "FAILED",
                    ("cleanup-finalization.json",),
                    "cleanup_finalization_failed",
                )
        failure = self.first_failure
        if failure:
            required.append("failure")
        if self.kind == "positive":
            for name in (
                "http_dispatch",
                "native_acceptance_while_tool_running",
                "native_steering_consumed_after_completion",
            ):
                if not any(item.claim == name for item in self.observations):
                    self.claim(
                        name,
                        "NOT VERIFIED",
                        "required_steering_prerequisite_not_observed",
                        "failure.json" if failure else "progress.json",
                    )
        self.observations.append(cleanup)
        self.claim(
            "native_waiting",
            "NOT VERIFIED",
            "no_qualified_native_waiting_edge",
            "stream-closed.json"
            if (self.evidence / "stream-closed.json").exists()
            else "progress.json",
        )
        for claim in required:
            if not any(item.claim == claim for item in self.observations):
                self.claim(
                    claim,
                    "NOT VERIFIED",
                    "required_claim_not_observed",
                    "failure.json" if failure else "progress.json",
                )
        self.progress("finished")
        result = CaseResult(
            self.evidence / "result.json", tuple(required), tuple(self.observations), cleanup
        )
        return _CaseDraft(
            result,
            {
                "profile": asdict(self.profile),
                "kind": self.kind,
                "request": asdict(self.request),
                "runtime": self.runtime,
                "required_claims": required,
                "observations": [asdict(item) for item in result.observations],
                "cleanup": asdict(cleanup),
                "failure": failure,
            },
            {
                "driver_sha256": sha256(Path(__file__)),
                "owners": list(self.owners.values()),
                "unowned_unreadable": list(self.unowned_unreadable.values()),
                "foreground_host": self.foreground_host_state(),
                "api_pid": self.server.pid if self.server else None,
                "receipt": result.receipt.name,
            },
        )


def _run_case(request: Request, profile: ClockProfile, kind: str, parent: Path) -> CaseResult:
    evidence = Path(tempfile.mkdtemp(prefix=f"{profile.name}-{kind}-", dir=parent))
    draft = None
    runtime_owner = None
    try:
        evidence.chmod(0o700)
        aggregate = parent / "manifest.json"
        if aggregate.is_file():
            progress = json.loads(aggregate.read_text())
            write_json(
                aggregate,
                {
                    **progress,
                    "active_run": str(evidence),
                    "exact_owners_progress": str(evidence / "progress.json"),
                },
            )
        runtime_owner = _RuntimeOwner.allocate(request, evidence)
        with runtime_owner:
            run = _OwnedRun(request, profile, kind, runtime_owner)
            draft = run.qualify()
    except FINALIZATION_ERRORS as exc:
        failure = sanitize(type(exc).__name__ + ": " + str(exc))
        write_json(
            evidence / "case-finalization.json",
            {
                "error": failure,
                "runtime": runtime_owner.root if runtime_owner else None,
            },
            immutable=True,
        )
        cleanup = Observation("owned_cleanup", "FAILED", ("case-finalization.json",), failure)
        if draft is None:
            result = CaseResult(evidence / "result.json", ("case_finalization",), (), cleanup)
            draft = _CaseDraft(result, {"failure": failure, "request": asdict(request)}, {})
        else:
            result = replace(
                draft.result,
                cleanup=cleanup,
                observations=tuple(
                    cleanup if item.claim == "owned_cleanup" else item
                    for item in draft.result.observations
                ),
            )
            draft = replace(
                draft,
                result=result,
                payload={**draft.payload, "cleanup": asdict(cleanup), "failure": failure},
            )
    return _publish_case(draft)


def as_role_tuple(root: SelectedRootIdentity) -> tuple[ProcessIdentity, ...]:
    return (root.roles.tui, root.roles.supervisor, root.roles.worker, root.roles.kernel)


def qualify(request: Request) -> tuple[bool, Path]:
    request.evidence_parent.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="provider-waits-", dir=request.evidence_parent))
    parent.chmod(0o700)
    write_json(
        parent / "manifest.json", {"status": "interrupted", "request": asdict(request), "runs": []}
    )
    cases = {
        "selector": ((RUNNER, "selector"),),
        "runner": ((RUNNER, "idle"), (RUNNER, "positive")),
        "pane": ((PANE, "idle"), (PANE, "positive")),
        "waits": ((RUNNER, "idle"), (RUNNER, "positive"), (PANE, "idle"), (PANE, "positive")),
    }
    results = []
    for profile, kind in cases[request.case]:
        result = _run_case(request, profile, kind, parent)
        results.append(result)
        write_json(
            parent / "manifest.json",
            {
                "status": "interrupted",
                "request": asdict(request),
                "runs": [str(item.receipt) for item in results],
            },
        )
    matched_deployments = False
    deployment_signatures = []
    for result in results:
        artifact = result.receipt.parent / "artifact.json"
        sources = result.receipt.parent / "source-before.json"
        if artifact.exists() and sources.exists():
            value = json.loads(artifact.read_text())
            deployment_signatures.append(
                {
                    "prime_sha256": value["prime_sha256"],
                    "driver_sha256": value["driver_sha256"],
                    "imports": value["imports"],
                    "sources": json.loads(sources.read_text()),
                }
            )
    if deployment_signatures:
        matched_deployments = len(deployment_signatures) == len(results) and all(
            signature == deployment_signatures[0] for signature in deployment_signatures
        )
    wrong = None
    if request.case == "waits" and request.expected_memory is None and len(results) == 4:
        wrong = _run_case(replace(request, expected_memory="WRONG"), RUNNER, "positive", parent)
        results.append(wrong)
    wrong_verified = False
    if wrong:
        wrong_artifact = (
            json.loads((wrong.receipt.parent / "artifact.json").read_text())
            if (wrong.receipt.parent / "artifact.json").exists()
            else {}
        )
        wrong_sources = wrong.receipt.parent / "source-before.json"
        wrong_signature = {
            "prime_sha256": wrong_artifact.get("prime_sha256"),
            "driver_sha256": wrong_artifact.get("driver_sha256"),
            "imports": wrong_artifact.get("imports"),
            "sources": json.loads(wrong_sources.read_text()) if wrong_sources.exists() else None,
        }
        matched_deployments = (
            matched_deployments
            and bool(deployment_signatures)
            and wrong_signature == deployment_signatures[0]
        )
        receipt = json.loads(wrong.receipt.read_text())
        comparison = wrong.receipt.parent / "memory-comparison.json"
        claims = {item.claim: item for item in wrong.observations}
        wrong_verified = (
            matched_deployments
            and wrong.committed
            and all(result.passed for result in results[:4])
            and receipt["failure"] == "memory_read_mismatch"
            and not wrong.passed
            and wrong.cleanup.status == "VERIFIED"
            and comparison.is_file()
            and json.loads(comparison.read_text()).get("actual_normal_tool_completed") is True
            and all(
                claims.get(name) and claims[name].status == "VERIFIED"
                for name in wrong.required_claims
                if name not in ("memory_read", "failure")
            )
        )
    expected_count = len(cases[request.case])
    passed = (
        matched_deployments
        and len(results) >= expected_count
        and all(result.committed for result in results)
        and all(result.passed for result in results[:expected_count])
    )
    if request.case == "waits" and request.expected_memory is None:
        passed = passed and wrong_verified
    manifest = parent / "manifest.json"
    final_manifest = parent / ".manifest-final.json"
    write_json(
        final_manifest,
        {
            "status": "VERIFIED" if passed else "FAILED",
            "request": asdict(request),
            "required_runs": expected_count,
            "matched_deployments": matched_deployments,
            "runs": [
                {
                    "receipt": str(result.receipt),
                    "sha256": sha256(result.receipt),
                    "manifest_sha256": sha256(result.receipt.parent / "manifest.json"),
                    "completion_sha256": sha256(result.receipt.parent / "completion.json"),
                    "passed": result.passed,
                }
                for result in results
            ],
            "wrong_result_control": asdict(
                Observation(
                    "wrong_result_control",
                    "VERIFIED" if wrong_verified else "NOT VERIFIED",
                    (str(wrong.receipt),) if wrong else (),
                    "fresh_normal_tool_named_mismatch_and_clean_cleanup"
                    if wrong_verified
                    else "not_qualified",
                )
            ),
            "scope": "actual_xai_grok_4.7_running_waits_only",
            "mcp_reconnect": "NOT VERIFIED outside_wait_case",
        },
        immutable=True,
    )
    os.replace(final_manifest, manifest)
    print(
        json.dumps({"result": "VERIFIED" if passed else "FAILED", "manifest": str(manifest)}),
        flush=True,
    )
    return passed, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prime-path", type=Path, required=True)
    parser.add_argument(
        "--kernel-python",
        type=Path,
        required=True,
        help="Keep the environment's bin/python entry, including its symlink.",
    )
    parser.add_argument(
        "--auth-source",
        type=Path,
        required=True,
        help="Actual xai OAuth auth.json. Contents never enter evidence.",
    )
    parser.add_argument("--evidence-parent", type=Path, required=True)
    parser.add_argument("--case", choices=("selector", "runner", "pane", "waits"), default="waits")
    parser.add_argument(
        "--expected-memory",
        help="Change only the final memory verifier literal. "
        "WRONG must exit 1 at memory_read_mismatch.",
    )
    parser.add_argument(
        "--cooperative-cleanup",
        action="store_true",
        required=True,
        help="Admit no hostile concurrent namespace writer during cleanup.",
    )
    args = parser.parse_args(argv)
    for name in ("prime_path", "kernel_python", "auth_source", "evidence_parent"):
        if not getattr(args, name).is_absolute():
            parser.error("--" + name.replace("_", "-") + " must be absolute")
    os.umask(0o077)
    request = Request(
        args.prime_path,
        args.kernel_python,
        args.auth_source,
        args.evidence_parent,
        args.case,
        args.expected_memory,
        args.cooperative_cleanup,
    )
    try:
        passed, _ = qualify(request)
        return 0 if passed else 1
    except FINALIZATION_ERRORS as exc:
        print(
            "FAILED probe_finalization " + sanitize(type(exc).__name__ + ": " + str(exc)),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

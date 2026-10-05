#!/usr/bin/env -S uv run --no-sync python

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import IO, Literal

import httpx
import provider_probe as owner
import psutil
import yaml
from adapter_probe import (
    ProcessIdentity,
    assert_same_selected_root_identity,
    read_actual_kernel_identity,
    selected_root_identity_record,
    sha256,
    unused_port,
)
from verify import wait_for

OWNER_SHA256 = "c4d86b911fd5b793ec24453c666b43c14a6736164478051f3b005085cf2847c3"
ERROR_PREFIX = "Request failed on the runner; see the runner log for details: "
REQUIRED = (
    "actual_provider",
    "declared_tool",
    "generation_1_success",
    "completed_outage_error",
    "generation_2_independent_success",
    "selected_root_continuity",
    "kernel_and_memory_continuity",
    "owned_cleanup",
)


@dataclass(frozen=True)
class Request:
    prime_path: Path
    kernel_entry: Path
    auth_source: Path
    evidence_parent: Path
    cooperative_cleanup: bool = False


@dataclass(frozen=True)
class FixtureDeclaration:
    run_nonce: str
    port: int
    server_name: str
    bare_tool_name: str

    @property
    def tool_name(self) -> str:
        return f"{self.server_name}__{self.bare_tool_name}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


@dataclass
class GenerationHandle:
    generation: Literal[1, 2]
    process: subprocess.Popen
    stdout: IO[str]
    calls_log: Path
    stdout_log: Path
    startup_path: Path
    launched_monotonic: float
    identity: ProcessIdentity | None = None
    startup_nonce: str | None = None

    def record(self) -> dict:
        return {
            "generation": self.generation,
            "process": asdict(self.identity) if self.identity else {"pid": self.process.pid},
            "startup_nonce": self.startup_nonce,
            "calls_log": self.calls_log,
            "stdout_log": self.stdout_log,
            "startup_path": self.startup_path,
            "launched_monotonic": self.launched_monotonic,
            "exit": self.process.poll(),
            "stdout_closed": self.stdout.closed,
        }


@dataclass(frozen=True)
class PhaseWitness:
    call_id: str
    call_nonce: str
    text: str
    provider_request_sequence: int
    invocation_sequence: int | None
    runner_exception: str | None = None


def json_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    lines = path.read_text().splitlines()
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        if not isinstance(record, dict):
            raise RuntimeError("mcp_evidence_record_not_object")
        records.append(record)
    return records


def fixture_schema(schema: dict) -> dict:
    parameters = schema.get("parameters")
    if (
        not isinstance(parameters, dict)
        or parameters.get("type") != "object"
        or parameters.get("required") != ["call_nonce"]
        or set(parameters.get("properties", {})) != {"call_nonce"}
        or parameters["properties"]["call_nonce"].get("type") != "string"
    ):
        raise RuntimeError("mcp_declared_schema_mismatch")
    return parameters


@dataclass(frozen=True)
class _ToolIdentity:
    _parts: tuple[str, ...]

    @classmethod
    def parse(cls, call_id: object, result_id: object, api: object) -> _ToolIdentity:
        if api != "openai-responses":
            raise RuntimeError("mcp_native_api_unsupported")
        if not isinstance(call_id, str) or not isinstance(result_id, str) or call_id != result_id:
            raise RuntimeError("mcp_native_tool_identity_mismatch")
        parts = call_id.split("|")
        if len(parts) not in (1, 2) or any(
            not part or not part.isprintable() or any(char.isspace() for char in part)
            for part in parts
        ):
            raise RuntimeError("mcp_native_tool_identity_malformed")
        return cls(tuple(parts))

    @property
    def native_id(self) -> str:
        return "|".join(self._parts)

    @property
    def wire_id(self) -> str:
        return self._parts[0]

    def pair(self, calls: object, results: object) -> tuple[dict, dict] | None:
        indexes = []
        for records in (calls, results):
            if not isinstance(records, list):
                raise RuntimeError("mcp_provider_pair_malformed")
            index = {}
            for record in records:
                if not isinstance(record, dict):
                    raise RuntimeError("mcp_provider_pair_malformed")
                wire_id = record.get("id")
                parsed = self.parse(wire_id, wire_id, "openai-responses")
                if len(parsed._parts) != 1 or wire_id in index:
                    raise RuntimeError("mcp_provider_pair_ambiguous")
                index[wire_id] = record
            indexes.append(index)
        call_index, result_index = indexes
        if call_index.keys() != result_index.keys():
            raise RuntimeError("mcp_provider_pair_orphan")
        if self.wire_id not in call_index:
            return None
        return call_index[self.wire_id], result_index[self.wire_id]


def _native_fixture_identities(entries: list[dict], declaration: FixtureDeclaration) -> None:
    identities = {}
    for entry in entries:
        message = entry.get("message", {})
        for call in message.get("content", []):
            if not isinstance(call, dict) or call.get("type") != "toolCall":
                continue
            if call.get("name") != declaration.tool_name:
                continue
            if (
                message.get("role") != "assistant"
                or message.get("api") != "openai-responses"
                or message.get("provider") != "xai"
                or message.get("model") != "grok-4.7"
            ):
                raise RuntimeError("mcp_native_api_unsupported")
            identity = _ToolIdentity.parse(call.get("id"), call.get("id"), message["api"])
            prior = identities.get(identity.wire_id)
            if prior is not None and prior != call:
                raise RuntimeError("mcp_native_tool_identity_alias")
            identities[identity.wire_id] = call


def native_call(
    entries: list[dict], declaration: FixtureDeclaration, nonce: str
) -> tuple[dict, dict]:
    _native_fixture_identities(entries, declaration)
    calls = [
        block
        for entry in entries
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]
    if len(calls) != 1 or calls[0].get("name") != declaration.tool_name:
        raise RuntimeError("mcp_expected_one_native_declared_call")
    call = calls[0]
    if not call.get("id") or call.get("arguments") != {"call_nonce": nonce}:
        raise RuntimeError("mcp_call_nonce_mismatch")
    results = [
        entry["message"]
        for entry in entries
        if entry.get("message", {}).get("role") == "toolResult"
        and entry["message"].get("toolCallId") == call["id"]
    ]
    if len(results) != 1 or results[0].get("toolName") != declaration.tool_name:
        raise RuntimeError("mcp_completed_native_result_missing")
    blocks = results[0].get("content")
    if (
        not isinstance(blocks, list)
        or len(blocks) != 1
        or blocks[0].get("type") != "text"
        or not isinstance(blocks[0].get("text"), str)
    ):
        raise RuntimeError("mcp_complete_text_result_required")
    _ToolIdentity.parse(call["id"], results[0].get("toolCallId"), "openai-responses")
    return call, results[0]


def provider_witness(
    projections: list[dict], declaration: FixtureDeclaration, call: dict, result: dict
) -> int:
    declarations = []
    continuations = []
    text = result["content"][0]["text"]
    identity = _ToolIdentity.parse(call.get("id"), result.get("toolCallId"), "openai-responses")
    previous_sequence = 0
    repeated_calls = {}
    repeated_results = {}
    for projection in projections:
        if projection.get("error") or (
            projection.get("api") != "openai-responses"
            or projection.get("transport") != "responses"
            or projection.get("format") != "responses-input"
        ):
            raise RuntimeError("mcp_provider_observer_unsupported_shape")
        sequence = projection.get("sequence")
        if type(sequence) is not int or sequence <= previous_sequence:
            raise RuntimeError("mcp_provider_sequence_invalid")
        previous_sequence = sequence
        if (
            projection.get("provider") != "xai"
            or projection.get("model") != "grok-4.7"
            or projection.get("payload_model") != "grok-4.7"
        ):
            raise RuntimeError("mcp_actual_provider_mismatch")
        schemas = projection.get("schemas", [])
        if len(schemas) != 1 or schemas[0].get("name") != declaration.tool_name:
            raise RuntimeError("mcp_provider_declaration_missing")
        parameters = fixture_schema(schemas[0])
        declarations.append((sequence, parameters))
        calls, results = projection.get("calls"), projection.get("results")
        pair = identity.pair(calls, results)
        for records, repeated in ((calls, repeated_calls), (results, repeated_results)):
            for item in records:
                if item["id"] in repeated and repeated[item["id"]] != item:
                    raise RuntimeError("mcp_provider_history_changed")
                repeated[item["id"]] = item
        for item in calls:
            if (
                isinstance(item.get("arguments"), dict)
                and item["arguments"].get("call_nonce") == call["arguments"]["call_nonce"]
                and item["id"] != identity.wire_id
            ):
                raise RuntimeError("mcp_provider_current_nonce_wrong_id")
        if pair is not None:
            projected_call, projected_result = pair
            item_id = identity._parts[1] if len(identity._parts) == 2 else None
            if (
                type(projected_call.get("item_id_present")) is not bool
                or projected_call["item_id_present"] != (item_id is not None)
                or projected_call.get("item_id") != item_id
            ):
                raise RuntimeError("mcp_provider_item_identity_mismatch")
            if projected_call != {
                "id": identity.wire_id,
                "name": declaration.tool_name,
                "arguments": call["arguments"],
                "item_id_present": item_id is not None,
                "item_id": item_id,
            }:
                raise RuntimeError("mcp_provider_call_mismatch")
            if projected_result != {"id": identity.wire_id, "text": text}:
                raise RuntimeError("mcp_provider_result_mismatch")
            continuations.append(sequence)
    if not continuations or not any(seq < continuations[0] for seq, _ in declarations):
        raise RuntimeError("mcp_provider_continuation_missing")
    if any(parameters != declarations[0][1] for _, parameters in declarations):
        raise RuntimeError("mcp_provider_schema_changed")
    return continuations[0]


def require_mcp_success(
    entries: list[dict],
    projections: list[dict],
    invocations: list[dict],
    declaration: FixtureDeclaration,
    generation: int,
    startup_nonce: str,
    identity: ProcessIdentity,
    call_nonce: str,
) -> PhaseWitness:
    call, result = native_call(entries, declaration, call_nonce)
    if result.get("isError") is not False:
        raise RuntimeError("mcp_native_success_was_error")
    text = result["content"][0]["text"]
    match = re.fullmatch(
        r"MCP_OK run=([0-9a-f]{32}) generation=([12]) server=([0-9a-f]{32}) call=([0-9a-f]{32})",
        text,
    )
    if not match:
        raise RuntimeError("mcp_success_literal_mismatch")
    run, actual_generation, actual_startup, actual_nonce = match.groups()
    if run != declaration.run_nonce or actual_nonce != call_nonce:
        raise RuntimeError("mcp_call_nonce_mismatch")
    if int(actual_generation) != generation:
        raise RuntimeError("mcp_generation_mismatch")
    if actual_startup != startup_nonce:
        raise RuntimeError("mcp_startup_nonce_mismatch")
    sequence = provider_witness(projections, declaration, call, result)
    records = [item for item in invocations if item.get("arguments") == call["arguments"]]
    if not records:
        raise RuntimeError("mcp_server_invocation_missing")
    for item in records:
        if (
            item.get("tool") != declaration.bare_tool_name
            or item.get("result") != text
            or item.get("generation") != generation
            or item.get("startup_nonce") != startup_nonce
            or item.get("pid") != identity.pid
            or item.get("started") != identity.started
            or type(item.get("sequence")) is not int
            or item["sequence"] < 1
        ):
            raise RuntimeError("mcp_server_invocation_mismatch")
    return PhaseWitness(call["id"], call_nonce, text, sequence, records[0]["sequence"])


def require_mcp_outage(
    entries: list[dict],
    projections: list[dict],
    runner_delta: str,
    invocations: list[dict],
    declaration: FixtureDeclaration,
    call_nonce: str,
) -> PhaseWitness:
    call, result = native_call(entries, declaration, call_nonce)
    text = result["content"][0]["text"]
    if result.get("isError") is not True or not text.startswith(ERROR_PREFIX) or "MCP_OK" in text:
        raise RuntimeError("mcp_completed_runner_outage_error_missing")
    reference = text.removeprefix(ERROR_PREFIX).strip()
    if not reference or "\n" in reference:
        raise RuntimeError("mcp_runner_log_reference_invalid")
    marker = f"MCP tool dispatch failed for {declaration.tool_name}"
    start = runner_delta.find(marker)
    if start < 0:
        raise RuntimeError("mcp_matching_runner_exception_missing")
    exception = runner_delta[start:]
    if "Traceback (most recent call last):" not in exception or not re.search(
        r"(?m)^[ |+]*[\w.]*?(?:Error|Exception|ExceptionGroup)(?::|\s|$)", exception
    ):
        raise RuntimeError("mcp_matching_runner_exception_missing")
    if any(item.get("arguments") == {"call_nonce": call_nonce} for item in invocations):
        raise RuntimeError("mcp_outage_server_invocation_present")
    sequence = provider_witness(projections, declaration, call, result)
    return PhaseWitness(call["id"], call_nonce, text, sequence, None, exception)


FIXTURE_SOURCE = """import json, os, re, time, uuid
from pathlib import Path
import psutil
from mcp.server.fastmcp import FastMCP
config = json.loads(CONFIG_JSON)
if not re.fullmatch(r"[0-9a-f]{32}", config["run_nonce"]):
    raise ValueError("invalid_run_nonce")
startup = uuid.uuid4().hex
started = psutil.Process().create_time()
sequence = 0
mcp = FastMCP(config["server_name"], host="127.0.0.1", port=config["port"],
              streamable_http_path="/mcp", json_response=True, stateless_http=False)
@mcp.tool(name=config["bare_tool_name"], description="Return the fixture process witness.")
def echo(call_nonce: str) -> str:
    global sequence
    if not re.fullmatch(r"[0-9a-f]{32}", call_nonce):
        raise ValueError("invalid_call_nonce")
    sequence += 1
    result = (f'MCP_OK run={config["run_nonce"]} generation={config["generation"]} '
              f'server={startup} call={call_nonce}')
    record = {"tool": config["bare_tool_name"], "arguments": {"call_nonce": call_nonce},
              "result": result, "generation": config["generation"], "startup_nonce": startup,
              "pid": os.getpid(), "started": started, "monotonic": time.monotonic(),
              "sequence": sequence}
    with open(config["calls_log"], "a") as handle:
        handle.write(json.dumps(record) + "\\n")
        handle.flush()
        os.fsync(handle.fileno())
    return result
Path(config["startup_path"]).write_text(json.dumps({
    "generation": config["generation"], "startup_nonce": startup,
    "pid": os.getpid(), "started": started, "stateful": True,
    "run_nonce": config["run_nonce"], "tool": config["bare_tool_name"]}))
mcp.run(transport="streamable-http")
"""

OBSERVER_SOURCE = """import fs from "node:fs";
const config = JSON.parse(CONFIG_JSON);
export default function(pi) {
  let sequence = 0;
  pi.on("before_provider_request", (event, ctx) => {
    const record = {sequence: ++sequence, monotonic_ms: performance.now(),
      provider: ctx.model?.provider, model: ctx.model?.id, api: ctx.model?.api,
      transport: "responses", format: "responses-input",
      payload_model: event.payload?.model, schemas: [], calls: [], results: []};
    try {
      const payload = event.payload;
      const items = payload?.input;
      if (record.api !== "openai-responses" || Object.hasOwn(payload || {}, "messages") ||
          !Array.isArray(payload?.tools) || !Array.isArray(items))
        throw new Error("unsupported_provider_shape");
      record.schemas = payload.tools.flatMap(t => {
        if (!t || t.type !== "function" || Object.hasOwn(t, "function"))
          throw new Error("unsupported_tool_declaration");
        return t.name === config.tool_name ? [{name: t.name, parameters: t.parameters}] : [];
      });
      const calls = new Map();
      const outputs = new Map();
      for (const item of items) {
        if (!item || Object.hasOwn(item, "tool_calls") ||
            Object.hasOwn(item, "tool_call_id") || Object.hasOwn(item, "function") ||
            item.role === "tool")
          throw new Error("mixed_provider_shape");
        if (item.type !== "function_call" && item.type !== "function_call_output") continue;
        const id = item.call_id;
        if (typeof id !== "string" || !id || /[\\s|\\x00-\\x1f\\x7f]/u.test(id))
          throw new Error("unsupported_wire_id");
        const records = item.type === "function_call" ? calls : outputs;
        if (records.has(id)) throw new Error("duplicate_wire_id");
        records.set(id, item);
      }
      if (calls.size !== outputs.size || [...calls.keys()].some(id => !outputs.has(id)))
        throw new Error("orphan_wire_record");
      for (const [id, c] of calls) {
        if (c.name !== config.tool_name) continue;
        const value = outputs.get(id).output;
        let text;
        if (typeof value === "string") text = value;
        else if (Array.isArray(value) && value.length === 1 && typeof value[0]?.text === "string")
          text = value[0].text;
        else throw new Error("unsupported_fixture_result_shape");
        record.calls.push({id, name: c.name,
          arguments: typeof c.arguments === "string" ? JSON.parse(c.arguments) : c.arguments,
          item_id_present: c.id !== undefined, item_id: c.id === undefined ? null : c.id});
        record.results.push({id, text});
      }
    } catch {
      record.error = "unsupported_fixture_projection";
    }
    fs.appendFileSync(config.path, JSON.stringify(record) + "\\n", {mode: 0o600});
    return undefined;
  });
}
"""


class OwnedMcpFixture:
    def __init__(self, run: _McpRun, declaration: FixtureDeclaration):
        self.run = run
        self.declaration = declaration
        self.handles: list[GenerationHandle] = []
        self.outputs: list[IO[str]] = []
        self.errors: list[str] = []

    def refused(self) -> bool:
        with socket.socket() as connection:
            connection.settimeout(1)
            try:
                connection.connect(("127.0.0.1", self.declaration.port))
            except ConnectionRefusedError:
                return True
        return False

    def start(self, generation: Literal[1, 2]) -> GenerationHandle:
        try:
            if generation != len(self.handles) + 1 or not self.refused():
                raise RuntimeError("mcp_generation_order_or_endpoint_not_free")
            directory = self.run.evidence / f"generation-{generation}"
            directory.mkdir(mode=0o700)
            calls, startup, log = (
                directory / name for name in ("calls.jsonl", "startup.json", "stdout.log")
            )
            calls.touch(mode=0o600)
            config = {
                **asdict(self.declaration),
                "generation": generation,
                "calls_log": str(calls),
                "startup_path": str(startup),
            }
            source = directory / "fixture.py"
            source.write_text(FIXTURE_SOURCE.replace("CONFIG_JSON", repr(json.dumps(config))))
            source.chmod(0o600)
            stdout = log.open("w")
            self.outputs.append(stdout)
            try:
                process = subprocess.Popen(
                    [str(owner.REPO / ".venv/bin/python"), str(source)],
                    cwd=self.run.workspace,
                    env=self.run.env(),
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                )
            except owner.FINALIZATION_ERRORS:
                stdout.close()
                raise
            handle = GenerationHandle(
                generation, process, stdout, calls, log, startup, time.monotonic()
            )
            self.handles.append(handle)
            record = self.run.own(psutil.Process(process.pid))
            handle.identity = ProcessIdentity(
                record["pid"], record["started"], tuple(record["argv"])
            )
            owner.write_json(directory / "launch.json", handle.record())

            def ready() -> bool:
                if process.poll() is not None:
                    raise RuntimeError("mcp_fixture_exited_before_readiness")
                if not startup.is_file():
                    return False
                witness = json.loads(startup.read_text())
                if (
                    witness.get("pid") != handle.identity.pid
                    or witness.get("started") != handle.identity.started
                    or witness.get("generation") != generation
                    or witness.get("stateful") is not True
                    or witness.get("run_nonce") != self.declaration.run_nonce
                    or witness.get("tool") != self.declaration.bare_tool_name
                    or not re.fullmatch(r"[0-9a-f]{32}", witness.get("startup_nonce", ""))
                ):
                    raise RuntimeError("mcp_fixture_startup_identity_mismatch")
                if not any(
                    item.status == psutil.CONN_LISTEN
                    and item.laddr.ip == "127.0.0.1"
                    and item.laddr.port == self.declaration.port
                    for item in psutil.Process(process.pid).net_connections(kind="tcp")
                ):
                    return False
                handle.startup_nonce = witness["startup_nonce"]
                return True

            wait_for(ready, "exact-owned stateful MCP listener", 30)
            if generation == 2 and handle.startup_nonce == self.handles[0].startup_nonce:
                raise RuntimeError("mcp_restart_startup_nonce_not_fresh")
            owner.write_json(directory / "ready.json", handle.record())
            return handle
        except owner.FINALIZATION_ERRORS as exc:
            self.errors.append("fixture start: " + owner.sanitize(str(exc)))
            raise

    def stop(self, handle: GenerationHandle) -> None:
        try:
            if handle.process.poll() is None:
                if handle.identity is None or not owner.identity_alive(handle.identity):
                    raise RuntimeError("mcp_exact_generation_identity_missing")
                handle.process.terminate()
                handle.process.wait(timeout=10)
            handle.stdout.close()
            if handle.identity and owner.identity_alive(handle.identity):
                raise RuntimeError("mcp_generation_survives_stop")
            owner.write_json(handle.startup_path.parent / "stopped.json", handle.record())
        except owner.FINALIZATION_ERRORS as exc:
            self.errors.append("generation stop: " + owner.sanitize(str(exc)))
            raise

    def close(self) -> owner._ExternalSettlement:
        records = []
        for handle in self.handles:
            try:
                self.stop(handle)
            except owner.FINALIZATION_ERRORS as exc:
                self.errors.append("fixture stop: " + owner.sanitize(str(exc)))
                try:
                    if handle.identity and owner.identity_alive(handle.identity):
                        handle.process.kill()
                        handle.process.wait(timeout=5)
                except owner.FINALIZATION_ERRORS as kill_error:
                    self.errors.append("fixture kill: " + owner.sanitize(str(kill_error)))
            try:
                streams = (handle.process.stdin, handle.process.stdout, handle.process.stderr)
                if any(stream is not None for stream in streams):
                    raise RuntimeError("mcp_unexpected_process_pipe")
                if handle.process.poll() is None or (
                    handle.identity and owner.identity_alive(handle.identity)
                ):
                    raise RuntimeError("mcp_generation_survives_close")
                records.append(handle.record())
            except owner.FINALIZATION_ERRORS as exc:
                self.errors.append("fixture process check: " + owner.sanitize(str(exc)))
        for output in self.outputs:
            try:
                output.close()
                if not output.closed:
                    raise RuntimeError("mcp_fixture_output_still_open")
            except owner.FINALIZATION_ERRORS as exc:
                self.errors.append("fixture output close: " + owner.sanitize(str(exc)))
        refused = False
        try:
            refused = self.refused()
            if not refused:
                raise RuntimeError("mcp_fixture_endpoint_still_available")
        except owner.FINALIZATION_ERRORS as exc:
            self.errors.append("fixture endpoint check: " + owner.sanitize(str(exc)))
        evidence = ()
        try:
            owner.write_json(
                self.run.evidence / "fixture-cleanup.json",
                {
                    "allocation_id": self.run.runtime_owner.allocation_id,
                    "errors": self.errors,
                    "endpoint_refused": refused,
                    "generations": records,
                    "outputs_closed": [output.closed for output in self.outputs],
                    "capture_mode": "child_direct_to_file",
                    "parent_readers": [],
                    "parent_threads": [],
                    "passed": not self.errors,
                },
            )
            evidence = ("fixture-cleanup.json",)
        except owner.FINALIZATION_ERRORS as exc:
            self.errors.append("fixture cleanup receipt: " + owner.sanitize(str(exc)))
        return owner._ExternalSettlement(
            self.run.runtime_owner.allocation_id,
            "failed_fixture" if self.errors else "settled_fixture",
            evidence,
            tuple(self.errors),
        )


class _McpRun(owner._OwnedRun):
    def __init__(
        self, request: Request, provider_request: owner.Request, runtime_owner: owner._RuntimeOwner
    ):
        super().__init__(
            provider_request, owner.ClockProfile("runner", 0, 0, 0, 0, 180), "mcp", runtime_owner
        )
        nonce = uuid.uuid4().hex
        self.declaration = FixtureDeclaration(
            nonce, unused_port(), "prime_reconnect_fixture", f"echo_{nonce}"
        )
        self.fixture = OwnedMcpFixture(self, self.declaration)
        self.provider_log = self.evidence / "provider-projections.jsonl"
        self.driver_hash = sha256(Path(__file__))
        self.continuity_roots = []
        self.scenario_request = request

    def cleanup(self, external_settlement: owner._ExternalSettlement) -> owner.Observation:
        if external_settlement.status == "no_fixture":
            raise RuntimeError("mcp_fixture_settlement_required")
        return super().cleanup(external_settlement)

    def preflight(self) -> None:
        if sha256(Path(owner.__file__)) != OWNER_SHA256:
            raise RuntimeError("mcp_provider_owner_not_admitted")
        super().preflight()
        extension = self.evidence / "observer.mjs"
        extension.write_text(
            OBSERVER_SOURCE.replace(
                "CONFIG_JSON",
                repr(
                    json.dumps(
                        {
                            "tool_name": self.declaration.tool_name,
                            "path": str(self.provider_log),
                        }
                    )
                ),
            )
        )
        extension.chmod(0o600)
        settings_path = self.prime / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["extensions"] = [str(extension)]
        owner.write_json(settings_path, settings)
        owner.write_json(
            self.evidence / "mcp-artifact.json",
            {
                "driver_sha256": self.driver_hash,
                "provider_owner_sha256": OWNER_SHA256,
                "observer_sha256": sha256(extension),
                "declaration": asdict(self.declaration),
                "profile": asdict(self.profile),
            },
        )

    def attach(self, *, resume: bool = False) -> None:
        if not resume:
            from omnigent.chat import _bundle_agent
            from omnigent.native.native_coding_agents import native_shell_terminal_spec

            self.fixture.start(1)
            agent = self.evidence / "declared-agent"
            mcp = agent / "tools/mcp"
            mcp.mkdir(parents=True, mode=0o700)
            config = {
                "spec_version": 1,
                "name": "prime-native-mcp-proof",
                "prompt": "Use the requested real native tools exactly once per request.",
                "executor": {"type": "omnigent", "config": {"harness": "prime-native"}},
                "spawn": True,
                "os_env": {
                    "type": "caller_process",
                    "cwd": str(self.workspace),
                    "sandbox": {"type": "none"},
                },
                "terminals": native_shell_terminal_spec(),
            }
            (agent / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
            declaration = {
                "name": self.declaration.server_name,
                "transport": "http",
                "url": self.declaration.url,
                "tools": [self.declaration.bare_tool_name],
                "timeout": 3,
                "retry": {
                    "max_retries": 2,
                    "backoff_base_s": 0.1,
                    "backoff_max_s": 0.2,
                    "jitter": False,
                },
            }
            (mcp / "reconnect.yaml").write_text(yaml.safe_dump(declaration, sort_keys=False))
            metadata = {
                "labels": {"omnigent.wrapper": "prime-native-ui", "omnigent.ui": "terminal"},
                "terminal_launch_args": [
                    "--provider",
                    "xai",
                    "--model",
                    "grok-4.7",
                    "--no-context-files",
                ],
            }
            bundle = _bundle_agent(agent)
            self.guard("public_declared_bundle_create")
            with httpx.Client(base_url=self.url, timeout=60) as client:
                response = client.post(
                    "/v1/sessions",
                    data={"metadata": json.dumps(metadata)},
                    files={"bundle": ("mcp-proof.tar.gz", bundle, "application/gzip")},
                )
            if response.is_success:
                self.own_session(response.json()["session_id"])
            owner.write_json(
                self.evidence / "bundle-create.json",
                {
                    "metadata": metadata,
                    "declaration": declaration,
                    "bundle_bytes": len(bundle),
                    "status": response.status_code,
                    "response": owner.sanitize(response.text),
                    "session_id": self.session_id,
                },
            )
            response.raise_for_status()
        super().attach(resume=True)

    def deployed_schema(self) -> dict:
        config = self.admit_config(self.config_path)
        tools = [
            item
            for item in config.get("tools", [])
            if item.get("name") == self.declaration.tool_name
        ]
        if len(tools) != 1:
            raise RuntimeError("mcp_deployed_declaration_missing")
        fixture_schema(tools[0])
        return tools[0]

    def turn(self, phase: str, nonce: str) -> tuple[list[dict], list[dict], str]:
        deadline = time.monotonic() + 180
        self.wait(
            lambda: self.snapshot(deadline=deadline).get("status") == "idle",
            "idle before MCP turn",
            min(30, self.remaining(deadline)),
        )
        projection_before = len(json_lines(self.provider_log))
        runner_before = self.runner_logs()
        acknowledgement = f"MCP_PHASE_DONE {nonce}"
        prompt = (
            f"Call the native registered tool {self.declaration.tool_name} exactly once with "
            f'arguments {{"call_nonce": "{nonce}"}}. Do not use ipython or another tool. '
            "After the tool returns, including if it returns an error, do not retry. "
            f"Reply with exactly {acknowledgement} and nothing else."
        )
        operation = self.reply_operation(prompt, acknowledgement, None, deadline)
        native_before = set(operation.native_ids)
        started = time.monotonic()
        fresh = []
        failure = None
        try:
            self.send(prompt, deadline=operation.deadline)
            reply = self.completed_reply(operation, phase)
            fresh = reply["fresh_items"]
        except owner.FINALIZATION_ERRORS as exc:
            failure = owner.sanitize(str(exc))
            raise
        finally:
            captures = {}
            capture_errors = {}
            for name, read in (
                ("native", self.journal),
                ("projections", lambda: json_lines(self.provider_log)[projection_before:]),
                ("runner", self.runner_logs),
            ):
                try:
                    captures[name] = read()
                except owner.FINALIZATION_ERRORS as exc:
                    capture_errors[name] = owner.sanitize(str(exc))
            native_all = captures.get("native", [])
            entries = [entry for entry in native_all if entry.get("id") not in native_before]
            projections = captures.get("projections", [])
            runner = captures.get("runner", "")
            if capture_errors:
                failure = failure or "mcp_partial_turn_capture_failed"
            if not runner.startswith(runner_before):
                runner_delta = ""
                failure = failure or "mcp_runner_log_changed_nonappend"
            else:
                runner_delta = runner[len(runner_before) :]
            owner.write_json(
                self.evidence / f"{phase}.json",
                {
                    "prompt": prompt,
                    "native": entries,
                    "projections": projections,
                    "runner_delta": runner_delta,
                    "fresh_items": fresh,
                    "failure": failure,
                    "capture_errors": capture_errors,
                    "started": started,
                    "completed": time.monotonic(),
                    "nonce": nonce,
                },
            )
        if failure:
            raise RuntimeError(failure)
        _native_fixture_identities(native_all, self.declaration)
        proof = owner._reply_verdict(
            operation, native_all, [json.loads(item) for item in operation.public_baseline] + fresh
        )
        if not proof["complete"]:
            raise RuntimeError("mcp_completed_reply_changed")
        if self.journal_path != operation.journal_path or (self.session_id, self.external_id) != (
            operation.session_id,
            operation.external_id,
        ):
            raise RuntimeError("native_journal_identity_changed")
        assert_same_selected_root_identity(operation.root, self.selected_root(passive=True))
        replies = [entry for entry in entries if entry.get("id") == proof["native_reply_id"]]
        users = [entry for entry in entries if entry.get("id") == proof["native_user_id"]]
        if len(replies) != 1 or len(users) != 1:
            raise RuntimeError("mcp_fresh_native_completed_reply_missing")
        self.remaining(deadline)
        return entries, projections, runner_delta

    def root_witness(self, name: str) -> None:
        root = self.selected_root()
        owner.write_json(self.evidence / name, selected_root_identity_record(root))
        assert_same_selected_root_identity(self.seed, root)
        self.continuity_roots.append(name)

    def memory_read(self) -> None:
        nonce = uuid.uuid4().hex
        path = self.evidence / "kernel-read.json"
        expression = self.kernel_expression.replace("NONCE", repr(nonce))
        code = (
            "provider_value['count'] += 1\n"
            f"Path({str(path)!r}).write_text(json.dumps({expression}))\n"
            "print(provider_value['token'], provider_value['count'], "
            "provider_value['socket'].fileno() >= 0)"
        )
        self.message(
            "memory-read",
            "Execute this exact code using ipython once. "
            "Do not reinitialize any variable. Then reply with exactly the printed line.\n" + code,
            f"{self.token} 42 True",
            code=code,
        )
        actual = read_actual_kernel_identity(path)
        root = self.selected_root()
        self.qualify_kernel(actual, nonce, root)
        assert_same_selected_root_identity(self.seed, root)
        payload = json.loads(path.read_text())
        if (
            actual.pid != self.actual_kernel.pid
            or payload.get("token") != self.token
            or payload.get("count") != 42
            or payload.get("socket_live") is not True
        ):
            raise RuntimeError("mcp_kernel_or_memory_continuity_mismatch")
        owner.write_json(self.evidence / "root-final.json", selected_root_identity_record(root))
        self.claim(
            "kernel_and_memory_continuity",
            "VERIFIED",
            "same_owned_kernel_and_live_object",
            "kernel-seed.json",
            "kernel-read.json",
            "memory-read.json",
        )

    def qualify_mcp(self) -> owner._CaseDraft:
        failure = None
        cleanup_errors = []
        try:
            self.start()
            self.seed_memory()
            schema = self.deployed_schema()
            owner.write_json(self.evidence / "declared-schema.json", schema)
            nonces = {phase: uuid.uuid4().hex for phase in ("before", "outage", "after")}
            if len(set(nonces.values())) != 3:
                raise RuntimeError("mcp_phase_nonces_not_independent")
            owner.write_json(self.evidence / "phase-nonces.json", nonces)
            first = self.fixture.handles[0]
            native, projections, _ = self.turn("before", nonces["before"])
            witness = require_mcp_success(
                native,
                projections,
                json_lines(first.calls_log),
                self.declaration,
                1,
                first.startup_nonce,
                first.identity,
                nonces["before"],
            )
            if any(
                item["schemas"][0]["parameters"] != schema["parameters"] for item in projections
            ):
                raise RuntimeError("mcp_provider_deployed_schema_mismatch")
            owner.write_json(self.evidence / "before-witness.json", asdict(witness))
            self.claim(
                "actual_provider",
                "VERIFIED",
                "native_actual_grok_and_outgoing_request",
                "before.json",
            )
            self.claim(
                "declared_tool",
                "VERIFIED",
                "bundled_deployed_and_provider_schema",
                "bundle-create.json",
                "declared-schema.json",
                "before.json",
            )
            self.claim(
                "generation_1_success",
                "VERIFIED",
                "exact_native_provider_and_server_literal",
                "before.json",
                "before-witness.json",
            )
            self.fixture.stop(first)
            if not self.fixture.refused():
                raise RuntimeError("mcp_outage_endpoint_not_refused")
            owner.write_json(
                self.evidence / "outage-start.json",
                {
                    "generation": first.record(),
                    "endpoint_refused": True,
                    "monotonic": time.monotonic(),
                },
            )
            native, projections, runner_delta = self.turn("outage", nonces["outage"])
            if not self.fixture.refused():
                raise RuntimeError("mcp_outage_endpoint_became_available")
            witness = require_mcp_outage(
                native,
                projections,
                runner_delta,
                json_lines(first.calls_log),
                self.declaration,
                nonces["outage"],
            )
            referenced_name = Path(witness.text.removeprefix(ERROR_PREFIX).strip()).name
            referenced_log = self.runtime / "data/logs/runner" / referenced_name
            if (
                referenced_log.is_symlink()
                or not referenced_log.is_file()
                or f"MCP tool dispatch failed for {self.declaration.tool_name}"
                not in owner.sanitize(referenced_log.read_text(errors="replace"))
            ):
                raise RuntimeError("mcp_outage_runner_log_reference_mismatch")
            owner.write_json(self.evidence / "outage-witness.json", asdict(witness))
            self.claim(
                "completed_outage_error",
                "VERIFIED",
                "completed_native_runner_error_before_restart",
                "outage.json",
                "outage-witness.json",
                "outage-start.json",
            )
            self.root_witness("root-outage.json")
            second = self.fixture.start(2)
            outage_completed = json.loads((self.evidence / "outage.json").read_text())["completed"]
            if second.launched_monotonic <= outage_completed:
                raise RuntimeError("mcp_restart_before_completed_outage")
            if second.identity == first.identity or owner.identity_alive(first.identity):
                raise RuntimeError("mcp_old_generation_identity_survives")
            native, projections, _ = self.turn("after", nonces["after"])
            witness = require_mcp_success(
                native,
                projections,
                json_lines(second.calls_log),
                self.declaration,
                2,
                second.startup_nonce,
                second.identity,
                nonces["after"],
            )
            owner.write_json(self.evidence / "after-witness.json", asdict(witness))
            if self.deployed_schema() != schema:
                raise RuntimeError("mcp_deployed_schema_changed")
            self.claim(
                "generation_2_independent_success",
                "VERIFIED",
                "fresh_generation_independent_native_call",
                "after.json",
                "after-witness.json",
                "generation-2/ready.json",
            )
        except owner.FINALIZATION_ERRORS as exc:
            failure = owner.sanitize(str(exc))
            try:
                owner.write_json(
                    self.evidence / "failure.json",
                    {
                        "reason": failure,
                        "type": type(exc).__name__,
                        "traceback": owner.sanitize(traceback.format_exc()),
                    },
                )
            except owner.FINALIZATION_ERRORS as write_error:
                cleanup_errors.append("failure receipt: " + owner.sanitize(str(write_error)))
        finally:
            settlement = owner._ExternalSettlement(
                self.runtime_owner.allocation_id, "failed_fixture", (), ("fixture_not_settled",)
            )
            cleanup = owner.Observation(
                "owned_cleanup", "FAILED", (), "cleanup_finalization_failed"
            )
            try:
                qualified_seed = any(
                    item.claim == "kernel_seed" and item.status == "VERIFIED"
                    for item in self.observations
                )
                if qualified_seed:
                    continuity_errors = []
                    for action in (
                        lambda: self.root_witness("root-recovery-attempt.json"),
                        self.memory_read,
                    ):
                        try:
                            action()
                        except owner.FINALIZATION_ERRORS as exc:
                            continuity_errors.append(owner.sanitize(str(exc)))
                    cleanup_errors.extend(continuity_errors)
                    owner.write_json(
                        self.evidence / "continuity.json",
                        {"errors": continuity_errors, "roots": self.continuity_roots},
                    )
                    if not continuity_errors:
                        self.claim(
                            "selected_root_continuity",
                            "VERIFIED",
                            "same_root_after_outage_and_recovery_attempt",
                            "root-seed.json",
                            "root-recovery-attempt.json",
                            "root-final.json",
                            "continuity.json",
                        )
            except owner.FINALIZATION_ERRORS as exc:
                cleanup_errors.append("continuity finalization: " + owner.sanitize(str(exc)))
            finally:
                try:
                    settlement = self.fixture.close()
                    if (
                        settlement.allocation_id != self.runtime_owner.allocation_id
                        or settlement.status != "settled_fixture"
                        or not settlement.evidence
                        or settlement.errors
                    ):
                        raise RuntimeError("mcp_fixture_settlement_failed: " + repr(settlement))
                except owner.FINALIZATION_ERRORS as exc:
                    error = "fixture cleanup: " + owner.sanitize(str(exc))
                    cleanup_errors.append(error)
                    settlement = owner._ExternalSettlement(
                        self.runtime_owner.allocation_id, "failed_fixture", (), (error,)
                    )
                finally:
                    try:
                        cleanup = self.cleanup(settlement)
                    except owner.FINALIZATION_ERRORS as exc:
                        cleanup_errors.append("owner cleanup: " + owner.sanitize(str(exc)))
                    finally:
                        try:
                            self.finalize_owned_credentials(cleanup_errors)
                        except owner.FINALIZATION_ERRORS as exc:
                            cleanup_errors.append(
                                "finalize owned credentials: " + owner.sanitize(str(exc))
                            )
            if cleanup_errors:
                try:
                    owner.write_json(
                        self.evidence / "cleanup-finalization.json", {"errors": cleanup_errors}
                    )
                except owner.FINALIZATION_ERRORS as exc:
                    cleanup_errors.append(
                        "cleanup finalization receipt: " + owner.sanitize(str(exc))
                    )
                cleanup = owner.Observation(
                    "owned_cleanup",
                    "FAILED",
                    (*cleanup.evidence, "cleanup-finalization.json"),
                    "cleanup_finalization_failed",
                )
            if (
                settlement.status != "settled_fixture"
                or sha256(Path(__file__)) != self.driver_hash
                or sha256(Path(owner.__file__)) != OWNER_SHA256
            ):
                cleanup = owner.Observation(
                    "owned_cleanup",
                    "FAILED",
                    (*cleanup.evidence, "fixture-cleanup.json"),
                    "fixture_cleanup_or_probe_source_drift",
                )
            self.observations.append(cleanup)
        for name in REQUIRED:
            if not any(item.claim == name for item in self.observations):
                self.claim(
                    name,
                    "NOT VERIFIED",
                    "required_claim_not_observed",
                    "failure.json" if failure else "progress.json",
                )
        if failure:
            self.claim("failure", "FAILED", failure, "failure.json")
        required = (*REQUIRED, "failure") if failure else REQUIRED
        result = owner.CaseResult(
            self.evidence / "result.json", required, tuple(self.observations), cleanup
        )
        return owner._CaseDraft(
            result,
            {
                "request": asdict(self.scenario_request),
                "declaration": asdict(self.declaration),
                "required_claims": required,
                "observations": [asdict(item) for item in self.observations],
                "generations": [handle.record() for handle in self.fixture.handles],
                "cleanup": asdict(cleanup),
                "failure": failure,
                "finalization_errors": cleanup_errors,
            },
            {
                "receipt": result.receipt.name,
                "driver_sha256": self.driver_hash,
                "provider_owner_sha256": OWNER_SHA256,
            },
        )


def qualify(request: Request) -> tuple[bool, Path]:
    if request.evidence_parent.resolve().is_relative_to(owner.REPO):
        raise RuntimeError("mcp_evidence_parent_must_be_outside_checkout")
    request.evidence_parent.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="provider-mcp-", dir=request.evidence_parent))
    evidence.chmod(0o700)
    draft = None
    runtime_owner = None
    try:
        if sha256(Path(owner.__file__)) != OWNER_SHA256:
            raise RuntimeError("mcp_provider_owner_not_admitted")
        provider_request = owner.Request(
            request.prime_path,
            request.kernel_entry,
            request.auth_source,
            request.evidence_parent,
            "selector",
            cooperative_cleanup=request.cooperative_cleanup,
        )
        runtime_owner = owner._RuntimeOwner.allocate(provider_request, evidence)
        with runtime_owner:
            run = _McpRun(request, provider_request, runtime_owner)
            draft = run.qualify_mcp()
    except owner.FINALIZATION_ERRORS as exc:
        failure = owner.sanitize(type(exc).__name__ + ": " + str(exc))
        owner.write_json(
            evidence / "case-finalization.json",
            {"error": failure, "runtime": runtime_owner.root if runtime_owner else None},
            immutable=True,
        )
        cleanup = owner.Observation(
            "owned_cleanup", "FAILED", ("case-finalization.json",), failure
        )
        if draft is None:
            observations = tuple(
                cleanup
                if name == "owned_cleanup"
                else owner.Observation(name, "NOT VERIFIED", (), "case_finalization_failed")
                for name in REQUIRED
            )
            result = owner.CaseResult(evidence / "result.json", REQUIRED, observations, cleanup)
            draft = owner._CaseDraft(
                result,
                {
                    "failure": failure,
                    "finalization_errors": [failure],
                    "request": asdict(request),
                    "required_claims": REQUIRED,
                    "observations": [asdict(item) for item in observations],
                    "cleanup": asdict(cleanup),
                },
                {"driver_sha256": sha256(Path(__file__)), "provider_owner_sha256": OWNER_SHA256},
            )
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
                payload={
                    **draft.payload,
                    "observations": [asdict(item) for item in result.observations],
                    "cleanup": asdict(cleanup),
                    "failure": draft.payload.get("failure") or failure,
                    "finalization_errors": [
                        *draft.payload.get("finalization_errors", []),
                        failure,
                    ],
                },
            )
    result = owner._publish_case(draft)
    passed = result.passed
    if not result.committed:
        raise RuntimeError("mcp_case_completion_not_committed")
    print(
        json.dumps({"result": "VERIFIED" if passed else "FAILED", "receipt": str(result.receipt)}),
        flush=True,
    )
    return passed, result.receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prime-path", type=Path, required=True)
    parser.add_argument(
        "--kernel-python",
        type=Path,
        required=True,
        help="Keep the kernel environment's bin/python entry, including its symlink.",
    )
    parser.add_argument(
        "--auth-source",
        type=Path,
        required=True,
        help="Actual xai OAuth auth.json. Contents never enter evidence.",
    )
    parser.add_argument("--evidence-parent", type=Path, required=True)
    parser.add_argument(
        "--cooperative-cleanup",
        action="store_true",
        required=True,
        help="Acknowledge a cooperative namespace with no hostile same-user writers.",
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
        cooperative_cleanup=args.cooperative_cleanup,
    )
    try:
        passed, _ = qualify(request)
        return 0 if passed else 1
    except owner.FINALIZATION_ERRORS as exc:
        print(
            "FAILED probe_finalization " + owner.sanitize(type(exc).__name__ + ": " + str(exc)),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

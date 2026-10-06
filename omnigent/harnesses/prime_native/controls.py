from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from omnigent.harnesses.pi_native.bridge import _atomic_text
from omnigent.harnesses.prime_native.catalog import PrimeModelRef
from omnigent.util.reasoning_effort import PI_EFFORTS, to_pi_thinking_level


@dataclass(frozen=True)
class SetModel:
    model: PrimeModelRef


@dataclass(frozen=True)
class SetEffort:
    effort: str


@dataclass(frozen=True)
class Compact:
    custom_instructions: str | None = None


@dataclass(frozen=True)
class Interrupt:
    pass


Control = SetModel | SetEffort | Compact | Interrupt


class ControlStatus(StrEnum):
    APPLIED = "applied"
    INTERRUPT_ACCEPTED = "interrupt_accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ControlOutcome:
    status: ControlStatus
    detail: str = ""
    model: PrimeModelRef | None = None
    effort: str | None = None

    @property
    def http_status(self) -> int:
        return {
            ControlStatus.APPLIED: 200,
            ControlStatus.INTERRUPT_ACCEPTED: 202,
            ControlStatus.REJECTED: 409,
            ControlStatus.UNAVAILABLE: 503,
            ControlStatus.UNKNOWN: 504,
        }[self.status]

    def response_body(self) -> dict[str, object]:
        body: dict[str, object] = {"status": self.status.value, "detail": self.detail}
        if self.status not in {ControlStatus.APPLIED, ControlStatus.INTERRUPT_ACCEPTED}:
            body["error"] = f"prime_control_{self.status.value}"
        if self.model is not None:
            body["model"] = self.model.selector
        if self.effort is not None:
            body["effort"] = self.effort
        return body


class PrimeExtensionBinding:
    def __init__(self, bridge_dir: Path) -> None:
        self._root = bridge_dir / "controls"

    def _incarnation(self) -> str:
        record = json.loads((self._root / "binding.json").read_text())
        incarnation, pid, heartbeat = record["incarnation"], record["pid"], record["heartbeat"]
        if (
            not isinstance(incarnation, str)
            or not incarnation
            or type(pid) is not int
            or pid <= 0
            or not isinstance(heartbeat, (int, float))
            or not 0 <= time.time() * 1000 - heartbeat < 3000
        ):
            raise ValueError("Prime extension is not live")
        os.kill(pid, 0)
        return incarnation

    async def wait_until_ready(self, *, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._incarnation()
                return True
            except (OSError, ValueError, KeyError, TypeError):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.05, remaining))

    async def execute(self, control: Control, *, timeout_s: float = 18.0) -> ControlOutcome:
        payload: dict[str, object]
        match control:
            case SetModel(model):
                payload = {"type": "model", "provider": model.provider, "modelId": model.model_id}
            case SetEffort(effort):
                if effort not in PI_EFFORTS:
                    return ControlOutcome(ControlStatus.REJECTED, "Unsupported Prime effort")
                payload = {"type": "effort", "level": to_pi_thinking_level(effort)}
            case Compact(instructions):
                payload = {"type": "compact", "customInstructions": instructions}
            case Interrupt():
                payload = {"type": "interrupt"}
        try:
            incarnation = self._incarnation()
        except (OSError, ValueError, KeyError, TypeError):
            return ControlOutcome(ControlStatus.UNAVAILABLE, "No live Prime extension binding")
        if timeout_s <= 0:
            return ControlOutcome(ControlStatus.UNAVAILABLE, "Control forwarding budget expired")
        request_id = uuid.uuid4().hex
        request = self._root / "requests" / f"{request_id}.json"
        result = self._root / "results" / f"{request_id}.json"
        payload.update(
            id=request_id, incarnation=incarnation, expiresAt=time.time() * 1000 + timeout_s * 1000
        )
        deadline = time.monotonic() + timeout_s
        dispatched = False
        try:
            _atomic_text(request, json.dumps(payload))
            dispatched = True
            while time.monotonic() < deadline:
                try:
                    record = json.loads(result.read_text())
                except FileNotFoundError:
                    await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
                    continue
                if not isinstance(record, dict):
                    raise ValueError("Malformed Prime control result")
                if record.get("id") != request_id or record.get("incarnation") != incarnation:
                    return ControlOutcome(ControlStatus.UNKNOWN, "Mismatched Prime control result")
                status = ControlStatus(record["status"])
                model = None
                effort = record.get("effort")
                if effort is not None and not isinstance(effort, str):
                    raise ValueError("Malformed Prime effort result")
                if isinstance(control, SetModel) and status == ControlStatus.APPLIED:
                    selector = record["model"]
                    if not isinstance(selector, str):
                        raise ValueError("Malformed Prime model result")
                    model = PrimeModelRef.parse(selector)
                    if model != control.model:
                        raise ValueError("Prime applied a different model")
                if isinstance(control, SetEffort) and status == ControlStatus.APPLIED:
                    if effort not in PI_EFFORTS:
                        raise ValueError("Prime returned no effective effort")
                if status == ControlStatus.INTERRUPT_ACCEPTED and not isinstance(
                    control, Interrupt
                ):
                    raise ValueError("Unexpected abort acceptance")
                if status == ControlStatus.APPLIED and isinstance(control, Interrupt):
                    raise ValueError("Abort acceptance is not completion")
                return ControlOutcome(status, str(record.get("detail", "")), model, effort)
            return ControlOutcome(
                ControlStatus.UNKNOWN, "Prime control outcome unknown; it will not be retried"
            )
        except (OSError, ValueError, KeyError, TypeError):
            return ControlOutcome(
                ControlStatus.UNKNOWN if dispatched else ControlStatus.UNAVAILABLE,
                "Prime control acknowledgment unavailable",
            )
        finally:
            for path in (request, result):
                with contextlib.suppress(OSError):
                    path.unlink()

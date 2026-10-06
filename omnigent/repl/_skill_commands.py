from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from omnigent_ui_sdk import state_dir
from pydantic import BaseModel, Field

from omnigent.entities.conversation import SkillCommandDelivery


class SkillCommandInput(BaseModel):
    kind: Literal["skill"]
    name: str
    arguments: str
    stable_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class SkillCommandEvent(BaseModel):
    type: Literal["slash_command"]
    data: SkillCommandInput
    model_override: str | None = None


class SkillCommandSubmission(BaseModel):
    event: SkillCommandEvent
    delivery: SkillCommandDelivery | None = None


class SkillCommandJournal:
    def __init__(self, server: str, conversation_id: str):
        scope = hashlib.sha256(json.dumps([server, conversation_id]).encode()).hexdigest()
        self.directory = state_dir() / "skill-submissions" / scope

    def submissions(self) -> list[SkillCommandSubmission]:
        return [
            SkillCommandSubmission.model_validate_json(path.read_text())
            for path in sorted(self.directory.glob("*.json"))
        ]

    def save(self, event: dict[str, object]) -> None:
        submission = SkillCommandSubmission(event=SkillCommandEvent.model_validate(event))
        self._write(submission, new=True)

    def record(self, delivery: SkillCommandDelivery) -> None:
        if delivery.historical:
            return
        for submission in self.submissions():
            if submission.event.data.stable_id != delivery.invocation_id:
                continue
            if submission.delivery and submission.delivery.status != "unknown":
                return
            submission.delivery = delivery
            self._write(submission, new=False)
            return

    def _write(self, submission: SkillCommandSubmission, *, new: bool) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.directory / f"{submission.event.data.stable_id}.json"
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(submission.model_dump_json(exclude_none=True))
                handle.flush()
                os.fsync(handle.fileno())
                if new:
                    os.link(temporary, destination)
                else:
                    os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)


def admission_label(delivery: SkillCommandDelivery) -> str:
    if delivery.historical:
        return "Historical copy"
    return {
        "unknown": "Admission unknown",
        "accepted": "Admitted; completion not confirmed",
        "rejected": "Rejected before admission",
    }[delivery.status]

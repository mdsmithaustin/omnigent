from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

NATIVE_TOMBSTONE_TTL_SECONDS = 30 * 24 * 60 * 60


class NativeOwner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    environment: str
    runtime: str
    coordinator: str = ""


class NativeAdmission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    epoch: str
    owner: NativeOwner


class NativeAdmissionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: NativeOwner
    expected_epoch: str | None = None


class NativeAdmissionStatusRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    epoch: str


class NativeStopOutcome(StrEnum):
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    FAILED = "failed"


class NativeStop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    admission: NativeAdmission
    operation_id: str
    unresolved_owners: tuple[NativeOwner, ...] = ()


class NativeStopResult(BaseModel):
    outcome: NativeStopOutcome
    detail: str = ""


class NativeStopReceipt(BaseModel):
    stop: NativeStop
    result: NativeStopResult


class NativeSource(BaseModel):
    admission: NativeAdmission
    phase: Literal["admitted", "open", "stopping", "closed"] = "open"
    stop: NativeStop | None = None
    result: NativeStopResult | None = None
    unresolved_owners: tuple[NativeOwner, ...] = ()
    # Set on deletion. The tombstone stays "closed" so releases without this
    # field read it as a revoked source.
    deleted_at: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _expire_deleted_phase(cls, data: Any) -> Any:
        # Unreleased builds tombstoned with phase "deleted" and no deletion time.
        if isinstance(data, dict) and data.get("phase") == "deleted":
            return {**data, "phase": "closed", "deleted_at": 0}
        return data

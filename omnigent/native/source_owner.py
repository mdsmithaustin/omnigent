from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

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


class _TombstoneDeletionTime(BaseModel):
    deleted_at: int | None = None


def tombstone_expired(state: str, now: int) -> bool:
    try:
        deleted_at = _TombstoneDeletionTime.model_validate_json(state).deleted_at
    except ValidationError:
        deleted_at = None
    return now - (deleted_at or 0) > NATIVE_TOMBSTONE_TTL_SECONDS

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


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

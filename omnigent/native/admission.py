"""Validate source admission at native creation and deferred input boundaries."""

from __future__ import annotations

import hashlib
import os
import platform
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path

import httpx
import psutil

from omnigent.native.native_bridge_common import async_bridge_dir_preparation_lock
from omnigent.native.native_dispatch import resolve_hook_for_key
from omnigent.native.source_owner import (
    NativeAdmission,
    NativeAdmissionRequest,
    NativeOwner,
    NativeStop,
)

_current_admission: ContextVar[NativeAdmission | None] = ContextVar(
    "native_admission", default=None
)


def native_owner(source_id: str, provider: str) -> NativeOwner:
    bridge = resolve_hook_for_key(provider, "bridge_dir")
    runtime = (
        bridge(source_id)
        if bridge
        else Path.home() / ".omnigent" / "native-owners" / provider / source_id
    )
    identity = f"{platform.node()}:{psutil.boot_time()}:{getattr(os, 'getuid', lambda: -1)()}"
    return NativeOwner(
        provider=provider,
        environment=hashlib.sha256(identity.encode()).hexdigest(),
        runtime=str(Path(runtime).absolute()),
        coordinator=f"{os.getpid()}:{psutil.Process().create_time()}",
    )


def current_native_admission(source_id: str) -> NativeAdmission | None:
    admission = _current_admission.get()
    return admission if admission is not None and admission.source_id == source_id else None


@contextmanager
def bind_native_admission(admission: NativeAdmission) -> Iterator[None]:
    token = _current_admission.set(admission)
    try:
        yield
    finally:
        _current_admission.reset(token)


async def admit_native(
    client: httpx.AsyncClient, source_id: str, provider: str
) -> NativeAdmission:
    retained = current_native_admission(source_id)
    response = await client.post(
        f"/v1/sessions/{source_id}/native-admission",
        json=NativeAdmissionRequest(
            owner=native_owner(source_id, provider),
            expected_epoch=retained.epoch if retained is not None else None,
        ).model_dump(),
    )
    response.raise_for_status()
    admission = NativeAdmission.model_validate(response.json())
    if admission.source_id != source_id or admission.owner != native_owner(source_id, provider):
        raise RuntimeError("Native admission names a different source owner.")
    return admission


async def validate_native(client: httpx.AsyncClient, admission: NativeAdmission) -> None:
    response = await client.post(
        f"/v1/sessions/{admission.source_id}/native-admission/validate",
        json=admission.model_dump(),
    )
    response.raise_for_status()
    if response.json() != {"current": True}:
        raise RuntimeError("Native admission validation did not confirm the current source owner.")


@asynccontextmanager
async def native_operation(
    client: httpx.AsyncClient | None, source_id: str, provider: str
) -> AsyncIterator[NativeAdmission]:
    if client is None:
        raise RuntimeError("Native ownership requires the source admission server.")
    admission = await admit_native(client, source_id, provider)
    with bind_native_admission(admission):
        yield admission


def validate_native_sync(
    admission: NativeAdmission | NativeStop, *, server_url: str, headers: dict[str, str]
) -> None:
    source_id = (
        admission.source_id
        if isinstance(admission, NativeAdmission)
        else admission.admission.source_id
    )
    with httpx.Client(base_url=server_url, headers=headers, timeout=5.0) as client:
        response = client.post(
            f"/v1/sessions/{source_id}/native-admission/validate", json=admission.model_dump()
        )
        response.raise_for_status()
        if response.json() != {"current": True}:
            raise RuntimeError(
                "Native admission validation did not confirm the current source owner."
            )


@asynccontextmanager
async def native_spawn(
    client: httpx.AsyncClient | None, source_id: str, provider: str
) -> AsyncIterator[NativeAdmission]:
    async with native_operation(client, source_id, provider) as admission:
        if provider == "prime-native":
            # Prime's wrapper consumes its reservation under this lock after tmux launch.
            yield admission
            return
        async with async_bridge_dir_preparation_lock(Path(admission.owner.runtime)):
            assert client is not None
            await validate_native(client, admission)
            yield admission

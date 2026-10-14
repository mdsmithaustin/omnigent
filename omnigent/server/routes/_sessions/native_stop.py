from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import ValidationError

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native.source_owner import NativeStopOutcome, NativeStopReceipt, NativeStopResult
from omnigent.server.routes._sessions.helpers import (
    _get_runner_client,
    _query_host_runner_status,
    _stop_session_via_runner,
)
from omnigent.server.routes._sessions.orchestration import _stop_host_runner_intentionally
from omnigent.stores.conversation_store import ConversationStore


async def stop_native_source(
    conversation: Conversation,
    conversation_store: ConversationStore,
    runner_router: Any,
    host_registry: Any,
) -> NativeStopResult:
    source_id = conversation.id
    state = await asyncio.to_thread(conversation_store.get_native_source, source_id)
    pending = state is not None and state.phase == "stopping"
    client = await _get_runner_client(source_id, runner_router)
    host_stopped: bool | None = None
    if client is None and not pending and conversation.host_id and conversation.runner_id:
        # Stop cannot reach the owner, so it keeps running unless the host ends its runner.
        host_stopped = await _stop_host_runner_intentionally(
            source_id,
            conversation.host_id,
            conversation.runner_id,
            host_registry,
            conversation_store,
        )
        if not host_stopped:
            host_conn = host_registry.get(conversation.host_id) if host_registry else None
            runner_status = (
                await _query_host_runner_status(host_conn, host_registry, conversation.runner_id)
                if host_conn is not None
                else None
            )
            if runner_status not in {"dead", "unknown"}:
                raise OmnigentError(
                    "Couldn't confirm the session's runner stopped. Please try again.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
    stop = (
        state.stop
        if pending and state is not None
        else await asyncio.to_thread(conversation_store.seal_native_stop, source_id)
    )
    if stop is None:
        await _stop_session_via_runner(source_id, runner_router)
        if host_stopped is None and conversation.host_id and conversation.runner_id:
            await _stop_host_runner_intentionally(
                source_id,
                conversation.host_id,
                conversation.runner_id,
                host_registry,
                conversation_store,
            )
        return NativeStopResult(
            outcome=NativeStopOutcome.UNKNOWN,
            detail="The current native owner has no recorded admission.",
        )
    result = NativeStopResult(
        outcome=NativeStopOutcome.UNKNOWN, detail="The current native owner could not be reached."
    )
    actor_pending = pending
    if client is not None:
        try:
            response = (
                await client.get(f"/v1/sessions/{source_id}", timeout=5.0)
                if pending
                else await client.post(
                    f"/v1/sessions/{source_id}/events",
                    json={"type": "stop_session", "native_stop": stop.model_dump()},
                    timeout=20.0,
                )
            )
            if response.status_code >= 400:
                actor_pending = True
                result = NativeStopResult(
                    outcome=NativeStopOutcome.UNKNOWN,
                    detail="The runner could not confirm native owner shutdown.",
                )
            else:
                payload = response.json()
                receipt_payload = payload.get("native_stop") if pending else payload
                receipt = NativeStopReceipt.model_validate(receipt_payload)
                if receipt.stop == stop:
                    result = receipt.result
                    actor_pending = False
                else:
                    actor_pending = True
        except (httpx.HTTPError, ConnectionError, ValueError, ValidationError):
            actor_pending = True
            result = NativeStopResult(
                outcome=NativeStopOutcome.UNKNOWN,
                detail="Native Stop has no matching closure acknowledgement.",
            )
    if not actor_pending and conversation.host_id and conversation.runner_id:
        if host_stopped is None:
            host_stopped = await _stop_host_runner_intentionally(
                source_id,
                conversation.host_id,
                conversation.runner_id,
                host_registry,
                conversation_store,
            )
        if not host_stopped:
            result = NativeStopResult(
                outcome=NativeStopOutcome.UNKNOWN,
                detail="The dedicated runner's exit could not be observed.",
            )
            actor_pending = True
    return await asyncio.to_thread(
        conversation_store.finish_native_stop,
        stop,
        result.outcome,
        pending=actor_pending,
        detail=result.detail,
    )

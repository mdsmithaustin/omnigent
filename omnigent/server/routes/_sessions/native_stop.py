from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import ValidationError

from omnigent.entities import Conversation
from omnigent.native.source_owner import NativeStopOutcome, NativeStopReceipt, NativeStopResult
from omnigent.server.routes._sessions.helpers import _get_runner_client
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
    stop = (
        state.stop
        if pending and state is not None
        else await asyncio.to_thread(conversation_store.seal_native_stop, source_id)
    )
    if stop is None:
        return NativeStopResult(
            outcome=NativeStopOutcome.UNKNOWN,
            detail="The current native owner has no recorded admission.",
        )
    client = await _get_runner_client(source_id, runner_router)
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

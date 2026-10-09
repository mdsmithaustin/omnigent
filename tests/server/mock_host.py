"""Teardown shared by the mock-host tunnel fixtures."""

from __future__ import annotations

import asyncio
import contextlib

from asgiref.testing import ApplicationCommunicator


async def close_mock_host(comm: ApplicationCommunicator, drain: asyncio.Task[None]) -> None:
    """Stop the reply drain, then wait for the tunnel to take the host offline."""
    drain.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drain
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    await comm.future

"""
Integration tests for ``POST /v1/hosts/{id}/directories``.

Wires up a real host tunnel + REST router pair, drives a fake host
that auto-replies to ``host.create_dir`` frames, and exercises the
endpoint's contract end-to-end. Mirrors the structure of
``test_hosts_filesystem.py`` (the browse endpoint) — the create-folder
action shares the same owner-scoped, host-forwarded design.
"""

from __future__ import annotations

import asyncio
from builtins import ExceptionGroup
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostHelloFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

# Same liveness-race flake guard as test_hosts_filesystem.py: the mock
# WS host can be starved + deregistered under parallel CI load. Tests
# are sub-second; rerun rather than fail.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "f28abbfd54a8b22204184b69d9bfd49f"
_HOST_NAME = "mkdir-test-laptop"


def _websocket_scope(path: str) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope.

    :param path: WebSocket path, e.g. ``"/v1/hosts/X/tunnel"``.
    :returns: ASGI scope dict.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def _hello_text(name: str = _HOST_NAME) -> str:
    """Encode a hello frame for tests.

    :param name: Host name reported in the hello frame.
    :returns: JSON-encoded hello frame.
    """
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
        )
    )


@pytest.fixture()
def mkdir_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """
    App with host tunnel + REST routes for create-directory tests.

    :param db_uri: SQLite URI fixture.
    :returns: (app, registry, host_store, conv_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, host_store),
        prefix="/v1",
    )
    app.include_router(
        create_hosts_router(registry, host_store, conv_store),
        prefix="/v1",
    )
    return app, registry, host_store, conv_store


@pytest.fixture()
async def mkdir_setup(
    mkdir_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> AsyncIterator[
    tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ]
]:
    """
    Connect a mock host and start an auto-replier for create_dir frames.

    Tests register fake replies in ``replies`` (path → reply dict)
    before calling the REST endpoint. The auto-replier consumes the
    ``host.create_dir`` frames the route pushes through the registry,
    decodes them, and feeds the configured result back — mirroring what
    ``host_tunnel.py`` does in production. An unregistered path defaults
    to a successful create echoing the requested path.

    :param mkdir_app: The fixture above.
    :returns: Async iterator yielding the wired-up state.
    """
    app, registry, _hs, _cs = mkdir_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    drain_task: asyncio.Task[None] | None = None
    setup_error: Exception | asyncio.CancelledError | None = None
    try:
        await comm.send_input({"type": "websocket.connect"})
        accepted = await comm.receive_output(timeout=1.0)
        assert accepted["type"] == "websocket.accept"
        await comm.send_input({"type": "websocket.receive", "text": _hello_text()})
        while registry.get(_HOST_ID) is None:
            await asyncio.sleep(0.01)

        conn = registry.get(_HOST_ID)
        assert conn is not None
        replies: dict[str, dict[str, Any]] = {}

        async def _drain() -> None:
            """Reply to directory requests until the fixture stops the drain."""
            while True:
                output = await comm.receive_output(timeout=None)
                if output.get("type") != "websocket.send":
                    continue
                text = output.get("text")
                if not isinstance(text, str):
                    continue
                frame = decode_host_frame(text)
                if not isinstance(frame, HostCreateDirFrame):
                    continue
                reply = replies.get(frame.path)
                if reply is None:
                    reply_frame = HostCreateDirResultFrame(
                        request_id=frame.request_id,
                        status="ok",
                        path=frame.path,
                    )
                else:
                    reply_frame = HostCreateDirResultFrame(
                        request_id=frame.request_id,
                        status=reply.get("status", "ok"),
                        path=reply.get("path"),
                        error=reply.get("error"),
                    )
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(reply_frame),
                    }
                )

        drain_task = asyncio.create_task(_drain())
        yield app, registry, comm, replies, drain_task
    except (Exception, asyncio.CancelledError) as exc:
        setup_error = exc
        raise
    finally:

        async def _finish_mock_host() -> list[Exception]:
            errors: list[Exception] = []
            if drain_task is not None:
                already_cancelled = drain_task.cancelled() or drain_task.cancelling() > 0
                if not drain_task.done() and not already_cancelled:
                    drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError as exc:
                    if already_cancelled:
                        error = RuntimeError("Mock-host drain was unexpectedly cancelled")
                        error.__cause__ = exc
                        errors.append(error)
                except Exception as exc:
                    errors.append(exc)

            app_cancelled = False
            if not comm.future.done():
                try:
                    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
                except asyncio.CancelledError as exc:
                    error = RuntimeError("Mock-host application was unexpectedly cancelled")
                    error.__cause__ = exc
                    errors.append(error)
                    app_cancelled = True
                except Exception as exc:
                    if all(exc is not error for error in errors):
                        errors.append(exc)

            stopped_app = False
            if not comm.future.done():
                done, _ = await asyncio.wait({comm.future}, timeout=5.0)
                if not done:
                    errors.append(
                        TimeoutError("Mock-host application did not disconnect within 5 seconds")
                    )
                    stopped_app = comm.future.cancelling() == 0
                    if stopped_app:
                        comm.future.cancel()
            try:
                await comm.future
            except asyncio.CancelledError as exc:
                if not stopped_app and not app_cancelled:
                    error = RuntimeError("Mock-host application was unexpectedly cancelled")
                    error.__cause__ = exc
                    errors.append(error)
            except Exception as exc:
                if all(exc is not error for error in errors):
                    errors.append(exc)
            return errors

        cleanup_task = asyncio.create_task(_finish_mock_host())
        finalizer_cancel: asyncio.CancelledError | None = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                if finalizer_cancel is None:
                    finalizer_cancel = exc
        errors = cleanup_task.result()
        if finalizer_cancel is not None:
            error = RuntimeError("Mock-host fixture teardown was externally cancelled")
            error.__cause__ = finalizer_cancel
            errors.append(error)
        if setup_error is not None and errors:
            if isinstance(setup_error, asyncio.CancelledError):
                raise setup_error from ExceptionGroup("Mock-host cleanup failed", errors)
            if all(setup_error is not error for error in errors):
                errors.insert(0, setup_error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Mock-host fixture failed", errors)


# ── Happy path ──────────────────────────────────────────


async def test_create_directory_returns_created_path(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    A valid create request returns the created absolute path.

    This is what the picker navigates into after creating the folder,
    so the path must round-trip through the endpoint intact.
    """
    app, _reg, _comm, replies, _drain = mkdir_setup
    target = "/Users/corey/projects/new-app"
    replies[target] = {"status": "ok", "path": target}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": target},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "directory"
    assert body["path"] == target


async def test_create_directory_already_exists_returns_409(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    An "already exists" host result maps to 409 with the message.

    The picker shows this inline so the user knows the name is taken
    rather than seeing a generic failure.
    """
    app, _reg, _comm, replies, _drain = mkdir_setup
    target = "/Users/corey/projects/dup"
    replies[target] = {"status": "ok", "error": "directory already exists"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": target},
        )

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


async def test_create_directory_relative_path_rejected(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    A relative path is rejected with 400 before reaching the host.

    The host needs a path it can resolve on its own; a relative path
    has no stable meaning across host process cwds.
    """
    app, _reg, _comm, _replies, _drain = mkdir_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": "relative/dir"},
        )

    assert resp.status_code == 400


async def test_create_directory_unknown_host_returns_404(
    mkdir_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Creating under an unknown host returns 404 (don't leak existence).
    """
    app, _reg, _hs, _cs = mkdir_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/7139b7e896ef9478abca6480107d1677/directories",
            json={"path": "/tmp/x"},
        )

    assert resp.status_code == 404


async def test_create_directory_windows_drive_path_accepted(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """A Windows drive path is absolute and must not 400 as relative."""
    app, _reg, _comm, _replies, _drain = mkdir_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": r"C:\Users\alice\work\fresh"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == r"C:\Users\alice\work\fresh"

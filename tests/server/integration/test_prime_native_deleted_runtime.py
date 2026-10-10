from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from omnigent.harnesses.prime_native import bridge
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.harnesses.prime_native.process import build_prime_launch
from omnigent.native.admission import native_owner
from omnigent.native.source_owner import NativeAdmission
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.integration.test_native_source_stop import source_and_target
from tests.test_prime_native import private_prime_roots as private_prime_roots

pytestmark = pytest.mark.asyncio

OWNER_HEADERS = {"X-Forwarded-Email": "owner@example.com"}


@pytest.fixture
def header_auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")


@pytest.fixture
def server_url(auth_app: FastAPI) -> Iterator[str]:
    config = uvicorn.Config(
        auth_app, host="127.0.0.1", port=0, lifespan="off", ws="none", log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=10)


def _retired_runtime(
    session_id: str, epoch: str, server_url: str, headers: dict[str, str]
) -> PrimeRuntimePaths:
    paths = bridge.runtime_paths(session_id)
    paths.prepare()
    (paths.session_dir / "saved.jsonl").write_text("saved transcript")
    (paths.agent_dir / "auth.json").write_text('{"provider": "fixture"}')
    admission = NativeAdmission(
        source_id=session_id, epoch=epoch, owner=native_owner(session_id, "prime-native")
    )
    (paths.root / "config.json").write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "serverUrl": server_url,
                "authHeaders": headers,
                "nativeAdmission": admission.model_dump(),
            }
        )
    )
    return paths


async def _session(auth_client, store: SqlAlchemyConversationStore) -> tuple[str, str]:
    session_id, _ = await source_and_target(auth_client, user="owner@example.com")
    return session_id, store.admit_native(
        session_id, native_owner(session_id, "prime-native")
    ).epoch


async def test_maintenance_removes_only_a_server_confirmed_deleted_runtime(
    header_auth_required, auth_client, db_uri, server_url, tmp_path: Path
):
    store = SqlAlchemyConversationStore(db_uri)
    deleted_id, deleted_epoch = await _session(auth_client, store)
    stale_id, _ = await _session(auth_client, store)
    live_id, live_epoch = await _session(auth_client, store)
    for session_id in (deleted_id, stale_id):
        response = await auth_client.delete(f"/v1/sessions/{session_id}", headers=OWNER_HEADERS)
        assert response.status_code == 200, response.text

    deleted = _retired_runtime(deleted_id, deleted_epoch, server_url, OWNER_HEADERS)
    stale = _retired_runtime(stale_id, uuid.uuid4().hex, server_url, OWNER_HEADERS)
    live = _retired_runtime(live_id, live_epoch, server_url, OWNER_HEADERS)
    foreign = _retired_runtime(uuid.uuid4().hex, deleted_epoch, server_url, OWNER_HEADERS)
    assert (deleted.agent_dir / "auth.json").exists()

    assert await asyncio.to_thread(bridge.prune_orphaned_bridge_dirs) == 1

    assert not deleted.root.exists()
    for kept in (stale, live, foreign):
        assert (kept.session_dir / "saved.jsonl").read_text() == "saved transcript"
    assert await asyncio.to_thread(bridge.prune_orphaned_bridge_dirs) == 0

    resumed = store.admit_native(
        live_id, native_owner(live_id, "prime-native"), expected_epoch=live_epoch
    )
    assert resumed.epoch == live_epoch
    store.validate_native_admission(resumed)
    source = tmp_path / "user-agent"
    source.mkdir()
    (source / "auth.json").write_text('{"provider": "fixture"}')
    launch = build_prime_launch(
        live,
        executable="/opt/prime-agent",
        extension=live.root / "extension.js",
        config=live.root / "config.json",
        external_session_id="saved-prime-id",
        environ={"PRIME_AGENT_CODING_AGENT_DIR": str(source)},
    )
    assert launch.argv == (
        "--extension",
        str(live.root / "extension.js"),
        "--session-dir",
        str(live.session_dir),
        "--resume",
        "saved-prime-id",
    )
    assert (live.session_dir / "saved.jsonl").read_text() == "saved transcript"


async def test_maintenance_keeps_a_deleted_runtime_without_server_credentials(
    header_auth_required, auth_client, db_uri, server_url
):
    store = SqlAlchemyConversationStore(db_uri)
    session_id, epoch = await _session(auth_client, store)
    response = await auth_client.delete(f"/v1/sessions/{session_id}", headers=OWNER_HEADERS)
    assert response.status_code == 200, response.text
    paths = _retired_runtime(session_id, epoch, server_url, {})

    assert await asyncio.to_thread(bridge.prune_orphaned_bridge_dirs) == 0
    assert (paths.session_dir / "saved.jsonl").read_text() == "saved transcript"

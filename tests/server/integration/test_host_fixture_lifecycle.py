"""Exercise the real mock-host fixtures across idle and shutdown boundaries."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from tests.server.integration import test_hosts_create_directory as directory
from tests.server.integration import test_hosts_worktrees as worktrees

mkdir_app = directory.mkdir_app
mkdir_setup = directory.mkdir_setup
wt_app = worktrees.wt_app
wt_setup = worktrees.wt_setup
MKDIR_HOST_ID = directory._HOST_ID
WT_HOST_ID = worktrees._HOST_ID

pytestmark = pytest.mark.asyncio


async def test_directory_reply_after_idle(mkdir_setup) -> None:
    """A directory request still reaches the same host after an idle interval."""
    app, registry, comm, _replies, drain = mkdir_setup
    connection = registry.get(MKDIR_HOST_ID)
    await asyncio.sleep(0.8)
    assert registry.get(MKDIR_HOST_ID) is connection
    assert not comm.future.done()
    assert not drain.done()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/v1/hosts/{MKDIR_HOST_ID}/directories", json={"path": "/workspace/new"}
        )
    assert response.status_code == 200
    assert response.json() == {"object": "directory", "path": "/workspace/new"}


async def test_worktree_reply_after_idle(wt_setup) -> None:
    """A worktree request still reaches the same host after an idle interval."""
    app, registry, comm, replies = wt_setup
    connection = registry.get(WT_HOST_ID)
    replies["/workspace/repo"] = {
        "worktrees": [
            {"path": "/workspace/repo", "branch": "main", "is_main": True, "detached": False}
        ]
    }
    await asyncio.sleep(0.8)
    assert registry.get(WT_HOST_ID) is connection
    assert not comm.future.done()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{WT_HOST_ID}/worktrees", params={"path": "/workspace/repo"}
        )
    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {"path": "/workspace/repo", "branch": "main", "is_main": True, "detached": False}
        ],
    }


@pytest.mark.parametrize("kind", ["mkdir", "wt"])
async def test_fixture_teardown_takes_host_offline(
    kind: str, request: pytest.FixtureRequest
) -> None:
    """Fixture teardown returns only after the tunnel has taken the host offline."""
    module = directory if kind == "mkdir" else worktrees
    app_state = request.getfixturevalue(f"{kind}_app")
    generator = getattr(module, f"{kind}_setup").__wrapped__(app_state)
    comm = (await anext(generator))[2]
    await generator.aclose()
    assert comm.future.done()
    assert app_state[1].get(module._HOST_ID) is None
    assert app_state[2].get_host(module._HOST_ID).status == "offline"

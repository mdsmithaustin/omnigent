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
@pytest.mark.parametrize(
    "outcome",
    ["normal", "disconnected", "drain-pending", "drain-terminal", "app-cancel", "decoder"],
)
async def test_actual_fixture_settles_owners(
    kind: str, outcome: str, request: pytest.FixtureRequest
) -> None:
    """Every shutdown path joins its tasks and persists the disconnected host."""
    module = directory if kind == "mkdir" else worktrees
    app_state = request.getfixturevalue(f"{kind}_app")
    generator = getattr(module, f"{kind}_setup").__wrapped__(app_state)
    value = await anext(generator)
    comm = value[2]
    drain = generator.ag_frame.f_locals["drain_task"]
    children = [task for task in asyncio.all_tasks() if task.get_name().endswith(module._HOST_ID)]
    assert len(children) == 3
    if outcome == "disconnected":
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait({comm.future}, timeout=2)
        assert comm.future.done()
    elif outcome.startswith("drain-"):
        drain.cancel("unexpected drain cancellation")
        if outcome == "drain-terminal":
            await asyncio.wait({drain}, timeout=2)
            assert drain.cancelled()
        else:
            assert not drain.done()
            assert drain.cancelling() == 1
    elif outcome == "app-cancel":
        comm.future.cancel("unexpected app cancellation")
    elif outcome == "decoder":
        await comm.output_queue.put({"type": "websocket.send", "text": "{"})
        await asyncio.wait({drain}, timeout=2)
        assert drain.done()
    if outcome in ("normal", "disconnected"):
        await generator.aclose()
    elif outcome == "decoder":
        original = drain.exception()
        with pytest.raises(ValueError, match="frame is not valid JSON") as caught:
            await generator.aclose()
        assert caught.value is original
    else:
        with pytest.raises(RuntimeError, match="unexpectedly cancelled") as caught:
            await generator.aclose()
        assert isinstance(caught.value.__cause__, asyncio.CancelledError)
    assert drain.done()
    assert comm.future.done()
    assert all(task.done() for task in children)
    assert app_state[1].get(module._HOST_ID) is None
    assert app_state[2].get_host(module._HOST_ID).status == "offline"
    assert not [task for task in asyncio.all_tasks() if task.get_name().endswith(module._HOST_ID)]


@pytest.mark.parametrize("kind", ["mkdir", "wt"])
@pytest.mark.parametrize("outcome", ["setup-error", "setup-cancel", "grace", "finalizer-cancel"])
async def test_actual_fixture_acquisition_and_finalizer(
    kind: str, outcome: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup and finalizer failures escape only after acquired owners settle."""
    from asgiref.testing import ApplicationCommunicator

    module = directory if kind == "mkdir" else worktrees
    disconnected = asyncio.Event()
    release = asyncio.Event()
    communicators = []
    original_error = ValueError("handshake observation failed")
    original_cancel = asyncio.CancelledError("setup cancellation")

    class ObservedCommunicator(ApplicationCommunicator):
        def __init__(self, app, scope):
            async def observed(scope, receive, send):
                await app(scope, receive, send)
                disconnected.set()
                if outcome in ("grace", "finalizer-cancel"):
                    await release.wait()

            super().__init__(observed, scope)
            communicators.append(self)

        async def receive_output(self, timeout=1):
            output = await super().receive_output(timeout)
            if output["type"] == "websocket.accept":
                if outcome == "setup-error":
                    raise original_error
                if outcome == "setup-cancel":
                    raise original_cancel
            return output

    monkeypatch.setattr(module, "ApplicationCommunicator", ObservedCommunicator)
    app_state = request.getfixturevalue(f"{kind}_app")
    generator = getattr(module, f"{kind}_setup").__wrapped__(app_state)
    if outcome.startswith("setup-"):
        expected = ValueError if outcome == "setup-error" else asyncio.CancelledError
        with pytest.raises(expected) as caught:
            await anext(generator)
        assert caught.value is (original_error if outcome == "setup-error" else original_cancel)
        assert len(communicators) == 1
        assert communicators[0].future.done()
        assert app_state[1].get(module._HOST_ID) is None
        assert app_state[2].get_host(module._HOST_ID) is None
        return
    value = await anext(generator)
    comm = value[2]
    drain = generator.ag_frame.f_locals["drain_task"]
    children = [task for task in asyncio.all_tasks() if task.get_name().endswith(module._HOST_ID)]
    closing = asyncio.create_task(generator.aclose())
    await asyncio.wait_for(disconnected.wait(), timeout=2)
    cleanup = generator.ag_frame.f_locals["cleanup_task"]
    if outcome == "grace":
        with pytest.raises(TimeoutError, match="did not disconnect within 5 seconds"):
            await closing
    else:
        closing.cancel("first cancellation")
        await asyncio.sleep(0)
        closing.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not closing.done()
        assert generator.ag_frame.f_locals["cleanup_task"] is cleanup
        first_cancel = generator.ag_frame.f_locals["finalizer_cancel"]
        release.set()
        with pytest.raises(RuntimeError, match="teardown was externally cancelled") as caught:
            await closing
        assert caught.value.__cause__ is first_cancel
        assert first_cancel.args == ("first cancellation",)
    assert cleanup.done()
    assert cleanup.result() == [closing.exception()]
    assert drain.done() and comm.future.done()
    assert len(children) == 3 and all(task.done() for task in children)
    assert app_state[1].get(module._HOST_ID) is None
    assert app_state[2].get_host(module._HOST_ID).status == "offline"


@pytest.mark.parametrize("kind", ["mkdir", "wt"])
@pytest.mark.parametrize("outcome", ["app-error", "setup-and-app", "drain-and-app"])
async def test_distinct_fixture_errors_preserve_identity(
    kind: str, outcome: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup preserves each independent error without duplicating its observations."""
    from builtins import ExceptionGroup

    from asgiref.testing import ApplicationCommunicator

    module = directory if kind == "mkdir" else worktrees
    app_error = LookupError("application completion failed")
    setup_error = ValueError("handshake observation failed")
    communicators = []

    class FailingCommunicator(ApplicationCommunicator):
        def __init__(self, app, scope):
            async def failing(scope, receive, send):
                await app(scope, receive, send)
                raise app_error

            super().__init__(failing, scope)
            communicators.append(self)

        async def receive_output(self, timeout=1):
            output = await super().receive_output(timeout)
            if output["type"] == "websocket.accept" and outcome == "setup-and-app":
                raise setup_error
            return output

    monkeypatch.setattr(module, "ApplicationCommunicator", FailingCommunicator)
    app_state = request.getfixturevalue(f"{kind}_app")
    generator = getattr(module, f"{kind}_setup").__wrapped__(app_state)
    if outcome == "setup-and-app":
        with pytest.raises(ExceptionGroup) as caught:
            await anext(generator)
        assert caught.value.exceptions == (setup_error, app_error)
    else:
        value = await anext(generator)
        comm = value[2]
        drain = generator.ag_frame.f_locals["drain_task"]
        if outcome == "drain-and-app":
            await comm.output_queue.put({"type": "websocket.send", "text": "{"})
            await asyncio.wait({drain}, timeout=2)
            assert drain.done()
            drain_error = drain.exception()
            with pytest.raises(ExceptionGroup) as caught:
                await generator.aclose()
            assert caught.value.exceptions == (drain_error, app_error)
        else:
            with pytest.raises(LookupError) as caught:
                await generator.aclose()
            assert caught.value is app_error
        assert drain.done()
        assert app_state[2].get_host(module._HOST_ID).status == "offline"
    assert len(communicators) == 1
    assert communicators[0].future.done()
    assert communicators[0].future.exception() is app_error
    assert app_state[1].get(module._HOST_ID) is None

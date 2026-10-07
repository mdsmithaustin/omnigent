from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.cli_auth import open_server_client
from omnigent.harnesses.codex_native import app_server as codex
from omnigent.harnesses.codex_native import bridge
from omnigent.native.admission import native_operation, validate_native
from omnigent.native.source_owner import NativeAdmission
from omnigent.runner._entry import _RunnerDatabricksAuth
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import SessionResourceRegistry
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _test_app_server,
)
from tests.native_source_helpers import NativeSourceServer


class _StartupFinished(Exception):
    pass


class _AuthenticatedSource(NativeSourceServer):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.bearer = "initial-token"
        self.requests: list[tuple[str, str | None, str | None, int]] = []

    def respond(
        self, request: httpx.Request, snapshot: dict[str, object] | None = None
    ) -> httpx.Response:
        bearer = request.headers.get("Authorization")
        binding = request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
        if bearer != f"Bearer {self.bearer}" or binding != "current-runner-secret":
            response = httpx.Response(
                403,
                json={"error": "Native ownership requires the session's authenticated runner."},
            )
        else:
            response = super().respond(request)
        self.requests.append((request.url.path, bearer, binding, response.status_code))
        if response.status_code == 200 and not request.url.path.endswith("/validate"):
            self.bearer = f"refreshed-token-{len(self.requests)}"
        return response


@pytest.fixture
def authenticated_source(tmp_path: Path) -> Iterator[_AuthenticatedSource]:
    source = _AuthenticatedSource(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            response = source.respond(
                httpx.Request(
                    "POST",
                    f"{source.url}{self.path}",
                    headers=dict(self.headers),
                    content=self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(httpx.Response(200, json={"workspace": str(tmp_path)}).content)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    source.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield source
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("failure", [None, "admit", "validate", "spawn"])
async def test_runner_codex_startup_uses_current_runner_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authenticated_source: _AuthenticatedSource,
    failure: str | None,
) -> None:
    source = authenticated_source
    session_id = "codex-startup-auth"
    admission_path = f"/v1/sessions/{session_id}/native-admission"
    monkeypatch.setenv("RUNNER_SERVER_URL", source.url)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "shared-codex-home"))
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setattr(
        "omnigent.runner._entry._make_auth_token_factory", lambda: lambda: "policy-token"
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._find_codex_cli", lambda: sys.executable)
    monkeypatch.setattr(codex, "_find_codex_cli", lambda: sys.executable)
    monkeypatch.setattr(
        codex,
        "resolve_native_codex_launch",
        lambda **kwargs: codex.NativeCodexLaunch([], "test-model", None),
    )
    monkeypatch.setattr(codex, "fresh_codex_launch_catalog", lambda **kwargs: [])
    monkeypatch.setattr(codex, "read_codex_model_catalog", lambda *args, **kwargs: [])
    monkeypatch.setattr(codex, "_codex_cli_version", AsyncMock(return_value=(0, 129, 0)))
    owner_lock = Mock() if failure == "spawn" else None
    monkeypatch.setattr(codex, "acquire_codex_native_process_owner_lock", lambda: owner_lock)
    _disable_codex_startup_rpc(monkeypatch)
    built_servers: list[codex.CodexNativeAppServer] = []
    build_server = codex.build_codex_native_server

    def capture_server(**kwargs: object) -> codex.CodexNativeAppServer:
        server = build_server(**kwargs)
        built_servers.append(server)
        return server

    monkeypatch.setattr(codex, "build_codex_native_server", capture_server)

    def stop_after_startup(**kwargs: object) -> None:
        raise _StartupFinished

    monkeypatch.setattr(codex, "CodexAppServerClient", stop_after_startup)
    spawned: list[tuple[str, ...]] = []

    async def spawn(*args: str, **kwargs: object) -> AsyncMock:
        assert [request[3] for request in source.requests] == [200, 200, 200]
        spawned.append(args)
        if failure == "spawn":
            raise OSError("process startup failed")
        return AsyncMock(pid=12345, returncode=0, stderr=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    original_bearer = source.bearer
    async with open_server_client(
        source.url,
        auth=_RunnerDatabricksAuth(lambda: source.bearer, source.url),
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "current-runner-secret"},
    ) as client:
        async with native_operation(client, session_id, "codex"):
            assert source.requests == [
                (admission_path, f"Bearer {original_bearer}", "current-runner-secret", 200)
            ]
            if failure == "admit":
                client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "different-runner-secret"
            if failure == "validate":

                async def change_binding_after_admit(response: httpx.Response) -> None:
                    if response.url.path == admission_path:
                        client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "different-runner-secret"

                client.event_hooks["response"].append(change_binding_after_admit)
            expected = (
                httpx.HTTPStatusError
                if failure in {"admit", "validate"}
                else OSError
                if failure == "spawn"
                else _StartupFinished
            )
            try:
                with pytest.raises(expected) as caught:
                    await orchestration._auto_create_codex_terminal(
                        session_id,
                        SessionResourceRegistry(),
                        lambda *_args: None,
                        server_client=client,
                    )
                if failure in {"admit", "validate"}:
                    assert caught.value.response.status_code == 403
                    assert source.requests[-1][2:] == ("different-runner-secret", 403)
                    assert source.requests[-1][0] == (
                        f"{admission_path}/validate" if failure == "validate" else admission_path
                    )
                    assert spawned == []
                else:
                    assert len(spawned) == 1
                    assert source.requests == [
                        (admission_path, "Bearer initial-token", "current-runner-secret", 200),
                        (admission_path, "Bearer refreshed-token-1", "current-runner-secret", 200),
                        (
                            f"{admission_path}/validate",
                            "Bearer refreshed-token-2",
                            "current-runner-secret",
                            200,
                        ),
                    ]
                    config = bridge.read_policy_hook_config(
                        bridge.bridge_dir_for_bridge_id(session_id)
                    )
                    assert config is not None
                    assert config["ap_auth_headers"] == {"Authorization": "Bearer policy-token"}
            finally:
                app_server = orchestration._AUTO_CODEX_APP_SERVERS.pop(session_id, None)
                if app_server is not None:
                    await app_server.close()
            assert len(built_servers) == 1
            assert built_servers[0].proc is None
            assert built_servers[0].stderr_task is None
            assert built_servers[0].process_owner_lock is None
            if owner_lock is not None:
                owner_lock.close.assert_called_once_with()
            assert not client.is_closed
            response = await client.get(f"/v1/sessions/{session_id}")
            assert response.json() == {"workspace": str(tmp_path)}


async def test_codex_standalone_startup_rejects_missing_runner_proof_and_closes_owned_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authenticated_source: _AuthenticatedSource,
) -> None:
    source = authenticated_source
    owned_clients: list[httpx.AsyncClient] = []

    def capture_client(server_url: str, *, headers: dict[str, str] | None) -> httpx.AsyncClient:
        client = open_server_client(server_url, headers=headers)
        owned_clients.append(client)
        return client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", capture_client)
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", tmp_path)
    server.session_id = "standalone-without-runner-proof"
    server.ap_server_url = source.url
    server.ap_auth_headers = {"Authorization": "Bearer initial-token"}

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await server.start()

    assert caught.value.response.status_code == 403
    assert source.requests == [
        (
            "/v1/sessions/standalone-without-runner-proof/native-admission",
            "Bearer initial-token",
            None,
            403,
        )
    ]
    assert len(owned_clients) == 1
    assert owned_clients[0].is_closed
    assert server.proc is None
    assert server.process_owner_lock is None


async def test_codex_startup_preserves_borrowed_admission_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authenticated_source: _AuthenticatedSource,
) -> None:
    source = authenticated_source

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "runner-tunnel.test"
        return source.respond(request)

    async def finish_startup(
        self: codex.CodexNativeAppServer,
        admission_client: httpx.AsyncClient,
        admission: NativeAdmission,
    ) -> None:
        await validate_native(admission_client, admission)
        raise _StartupFinished

    monkeypatch.setattr(codex.CodexNativeAppServer, "_start_admitted", finish_startup)
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", tmp_path)
    server.session_id = "borrowed-transport"
    server.ap_server_url = source.url
    server.ap_auth_headers = {"Authorization": "Bearer policy-token"}
    async with open_server_client(
        "http://runner-tunnel.test",
        auth=_RunnerDatabricksAuth(lambda: source.bearer, "http://runner-tunnel.test"),
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "current-runner-secret"},
        transport=httpx.MockTransport(respond),
    ) as client:
        server.server_client = client
        with pytest.raises(_StartupFinished):
            await server.start()
        await server.close()
        assert not client.is_closed
    assert source.requests == [
        (
            "/v1/sessions/borrowed-transport/native-admission",
            "Bearer initial-token",
            "current-runner-secret",
            200,
        ),
        (
            "/v1/sessions/borrowed-transport/native-admission/validate",
            "Bearer refreshed-token-1",
            "current-runner-secret",
            200,
        ),
    ]

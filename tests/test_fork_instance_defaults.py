"""Fork instance identities at service, secrets, cookie, and stop boundaries."""

from __future__ import annotations

import importlib
import json
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from omnigent import install_ledger
from omnigent.cli import _HostDaemonRecord
from omnigent.host import local_server
from omnigent.onboarding import secrets
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.passwords import hash_password
from omnigent.server.routes.accounts_auth import create_accounts_auth_router
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def test_instance_root_selectors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    assert install_ledger.ledger_path() == tmp_path / ".omnigent-mdsmithaustin/install_ledger.json"
    assert Path(secrets._secrets_path()) == tmp_path / ".omnigent-mdsmithaustin/secrets.json"

    monkeypatch.setenv("OMNIGENT_DATA_DIR", "~/chosen-data")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "relative-config")
    assert install_ledger.ledger_path() == tmp_path / "chosen-data/install_ledger.json"
    assert secrets._secrets_path() == "relative-config/secrets.json"
    assert local_server._local_data_dir() == tmp_path / "chosen-data"


def test_preferred_port_and_explicit_selection() -> None:
    with patch("socket.socket") as socket:
        assert local_server.pick_local_port() == 6768
        socket.return_value.__enter__.return_value.bind.assert_called_once_with(
            ("127.0.0.1", 6768)
        )
    with patch("socket.socket") as socket:
        assert local_server.pick_local_port(19090) == 19090
        socket.return_value.__enter__.return_value.bind.assert_called_once_with(
            ("127.0.0.1", 19090)
        )


def test_mixed_service_discovery_backfills_exact_fork_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    files = [
        "Library/LaunchAgents/ai.omnigent.host.plist",
        "Library/LaunchAgents/com.mdsmithaustin.omni.host.plist",
        "Library/LaunchAgents/com.mdsmithaustin.omni.host.extra.plist",
        "xdg/systemd/user/omnigent-host.service",
        "xdg/systemd/user/omni-mdsmithaustin-host.service",
        "xdg/systemd/user/omni-mdsmithaustin-host-extra.service",
    ]
    for filename in files:
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(filename)

    ledger = install_ledger.backfill_install_ledger(deep=True, apply=False)
    assert [(entry.kind, entry.label, entry.path) for entry in ledger.entries.launch_agents] == [
        (
            "launchd",
            "com.mdsmithaustin.omni.host",
            str(tmp_path / "Library/LaunchAgents/com.mdsmithaustin.omni.host.plist"),
        ),
        (
            "systemd_user",
            "omni-mdsmithaustin-host.service",
            str(tmp_path / "xdg/systemd/user/omni-mdsmithaustin-host.service"),
        ),
    ]
    assert ledger.entries.state_paths.omnigent_home == str(tmp_path / ".omnigent-mdsmithaustin")
    assert install_ledger.observed_launch_agents(deep=False) == []
    assert [(tmp_path / filename).read_text() for filename in files] == files


def test_keyring_operations_keep_upstream_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING", raising=False)
    entries = {
        ("omnigent", "anthropic"): "upstream",
        ("mdsmithaustin-omni", "anthropic"): "fork",
    }
    monkeypatch.setattr(
        secrets.keyring, "get_password", lambda service, name: entries.get((service, name))
    )
    monkeypatch.setattr(
        secrets.keyring,
        "set_password",
        lambda service, name, value: entries.__setitem__((service, name), value),
    )
    monkeypatch.setattr(
        secrets.keyring, "delete_password", lambda service, name: entries.pop((service, name))
    )

    assert secrets.load_secret("anthropic") == "fork"
    secrets.store_secret("anthropic", "replacement")
    assert secrets.load_secret("anthropic") == "replacement"
    secrets.delete_secret("anthropic")
    assert entries == {("omnigent", "anthropic"): "upstream"}
    assert secrets.load_secret("anthropic") is None


@pytest.mark.parametrize("disabled", ["1", "true", "YES"])
def test_disabled_keyring_uses_only_fork_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, disabled: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", disabled)
    upstream = tmp_path / ".omnigent/secrets.json"
    upstream.parent.mkdir()
    upstream.write_text('{"anthropic": "upstream"}')

    def forbidden(*args: object) -> None:
        pytest.fail("disabled keyring was accessed")

    for operation in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(secrets.keyring, operation, forbidden)
    secrets.store_secret("anthropic", "fork")
    assert secrets.load_secret("anthropic") == "fork"
    fork = tmp_path / ".omnigent-mdsmithaustin/secrets.json"
    assert json.loads(fork.read_text()) == {"anthropic": "fork"}
    assert fork.stat().st_mode & 0o777 == 0o600
    secrets.delete_secret("anthropic")
    assert json.loads(fork.read_text()) == {}
    assert secrets.load_secret("anthropic") is None
    assert upstream.read_text() == '{"anthropic": "upstream"}'


@pytest.mark.parametrize("command", [["stop"], ["server", "stop"]])
@pytest.mark.parametrize("recorded", [False, True])
def test_stop_selects_only_fork_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: list[str], recorded: bool
) -> None:
    cli_module = importlib.import_module("omnigent.cli")
    fork = tmp_path / ".omnigent-mdsmithaustin"
    upstream = tmp_path / ".omnigent"
    fork.mkdir()
    upstream.mkdir()
    upstream_record = upstream / "local_server.pid"
    upstream_record.write_text("2001\n6767\n")
    monkeypatch.setattr(cli_module, "_HOST_PID_PATH", fork / "host.pid")
    for attribute, filename in (
        ("_LOCAL_SERVER_PID_PATH", "local_server.pid"),
        ("_LOCAL_SERVER_SIG_PATH", "local_server.sig"),
        ("_LOCAL_SERVER_BASE_PATH_PATH", "local_server.basepath"),
        ("_LOCAL_SERVER_LOG_REF_PATH", "local_server.logpath"),
    ):
        monkeypatch.setattr(local_server, attribute, fork / filename)
    alive = {1001, 1002, 2001, 2002}
    terminated: list[int] = []
    probes: list[str] = []
    listener_inspections: list[object] = []

    def terminate(pid: int) -> None:
        terminated.append(pid)
        alive.remove(pid)

    def health(url: str, **kwargs: object) -> httpx.Response:
        probes.append(url)
        return httpx.Response(200, json={"status": "ok"})

    def inspect_listener(*args: object, **kwargs: object) -> SimpleNamespace:
        listener_inspections.append(args)
        return SimpleNamespace(stdout="2002\n")

    monkeypatch.setattr(local_server, "_pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(local_server, "_terminate_pid", terminate)
    monkeypatch.setattr(httpx, "get", health)
    monkeypatch.setattr(
        local_server,
        "subprocess",
        SimpleNamespace(run=inspect_listener),
    )

    def terminate_daemon(record: _HostDaemonRecord, *, force: bool) -> None:
        terminate(record.pid)
        cli_module._delete_daemon_record(record)

    monkeypatch.setattr(cli_module, "_terminate_daemon", terminate_daemon)
    if recorded:
        (fork / "local_server.pid").write_text("1001\n19191\n")
        cli_module._write_daemon_record(
            cli_module._HostDaemonRecord(
                pid=1002,
                target="local",
                mode="local",
                server_url=None,
                log_path=None,
                started_at=0,
            )
        )
    result = CliRunner().invoke(cli_module.cli, command)

    assert result.exit_code == 0, result.output
    assert terminated == ([1002, 1001] if recorded else [])
    assert alive == ({2001, 2002} if recorded else {1001, 1002, 2001, 2002})
    assert probes == (["http://127.0.0.1:19191/health"] if recorded else [])
    assert listener_inspections == []
    assert not (fork / "local_server.pid").exists()
    assert cli_module._list_daemon_records() == []
    assert upstream_record.read_text() == "2001\n6767\n"
    assert result.output.strip() == (
        ("Stopped 1 daemon(s) and the background server." if recorded else "Nothing to stop.")
        if command == ["stop"]
        else ("Stopped the background server." if recorded else "No background server is running.")
    )


def _assert_cookie(
    response: httpx.Response, name: str, *, secure: bool, deleted: bool = False
) -> None:
    parsed = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        parsed.load(header)
    cookie = parsed[name]
    assert cookie["path"] == "/"
    assert cookie["httponly"] is True
    assert bool(cookie["secure"]) is secure
    assert cookie["samesite"] == "lax"
    assert cookie["domain"] == ""
    assert int(cookie["max-age"]) == (0 if deleted else (300 if "auth_state" in name else 28800))


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("provider_name", ["accounts", "oidc"])
def test_cookie_login_read_and_logout_preserve_upstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_uri: str, scheme: str, provider_name: str
) -> None:
    base_url = f"{scheme}://localhost:6768"
    secure = scheme == "https"
    session_name = "__Host-mdsmithaustin_ap_session" if secure else "mdsmithaustin_ap_session"
    state_name = "__Host-mdsmithaustin_ap_auth_state" if secure else "mdsmithaustin_ap_auth_state"
    secret = bytes.fromhex("ab" * 32)
    admins_path = tmp_path / "admins"
    admins_path.write_text("")
    admins = AdminList(admins_path)
    app = FastAPI()
    if provider_name == "accounts":
        config = AccountsConfig(
            cookie_secret=secret,
            session_ttl_hours=8,
            base_url=base_url,
            init_admin_password=None,
            invite_ttl_seconds=300,
            magic_ttl_seconds=300,
        )
        store = SqlAlchemyAccountStore(db_uri)
        store.create_user_with_password("alice", hash_password("password-12345"))
        provider = UnifiedAuthProvider(source="accounts", accounts_config=config)
        app.include_router(create_accounts_auth_router(provider, store, admins), prefix="/auth")
        expected_user = "alice"
    else:
        config = OIDCConfig(
            issuer="https://github.com",
            client_id="client",
            client_secret="secret",
            redirect_uri=f"{base_url}/auth/callback",
            cookie_secret=secret,
            scopes="read:user user:email",
            session_ttl_hours=8,
            logout_redirect_uri=None,
            allowed_domains=None,
            provider_type="github",
            authorization_endpoint="https://github.com/login/oauth/authorize",
            token_endpoint="https://github.com/login/oauth/access_token",
            jwks_uri=None,
            userinfo_endpoint="https://api.github.com/user",
            allow_invites=False,
        )
        provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
        app.include_router(
            create_auth_router(provider, SqlAlchemyPermissionStore(db_uri), admins), prefix="/auth"
        )
        expected_user = "alice@example.com"

        async def token_response(*args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "idp-token"})

        async def email_response(*args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(
                200, json=[{"email": "alice@example.com", "primary": True, "verified": True}]
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", token_response)
        monkeypatch.setattr(httpx.AsyncClient, "get", email_response)

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, str | None]:
        return {"user": provider.get_user_id(request)}

    with TestClient(app, base_url=base_url) as client:
        sentinels = {
            "ap_session": "upstream-session",
            "ap_auth_state": "upstream-state",
            "__Host-ap_session": "upstream-secure-session",
            "__Host-ap_auth_state": "upstream-secure-state",
        }
        for name, value in sentinels.items():
            client.cookies.set(name, value, domain="localhost.local", path="/")
        assert client.get("/whoami").json() == {"user": None}
        if provider_name == "accounts":
            login = client.post(
                "/auth/login", json={"username": "alice", "password": "password-12345"}
            )
            assert login.status_code == 200, login.text
        else:
            start = client.get("/auth/login", follow_redirects=False)
            assert start.status_code == 302
            _assert_cookie(start, state_name, secure=secure)
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            login = client.get(
                "/auth/callback",
                params={"code": "idp-code", "state": state},
                follow_redirects=False,
            )
            assert login.status_code == 302, login.text
            _assert_cookie(login, state_name, secure=secure, deleted=True)
            assert client.cookies.get(state_name) is None
        _assert_cookie(login, session_name, secure=secure)
        assert client.get("/whoami").json() == {"user": expected_user}
        logout = (
            client.post("/auth/logout")
            if provider_name == "accounts"
            else client.get("/auth/logout", follow_redirects=False)
        )
        assert logout.status_code == (204 if provider_name == "accounts" else 302)
        _assert_cookie(logout, session_name, secure=secure, deleted=True)
        assert client.cookies.get(session_name) is None
        assert client.get("/whoami").json() == {"user": None}
        assert {name: client.cookies.get(name) for name in sentinels} == sentinels

"""CockroachDB startup migrations retry from the last durable revision."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text

from omnigent.db.cockroachdb import (
    _CRDB_BOOTSTRAP_MARKER_TABLE,
    _CRDB_BOOTSTRAP_MARKER_TOKEN,
    CRDB_BASELINE_REVISION,
    _crdb_server_version,
    _prepare_crdb_schema_transaction,
)
from omnigent.db.utils import (
    _build_alembic_config,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
)
from tests.db.test_cockroachdb import _crdb_engine


def _assert_head_schema(engine: Engine, db_uri: str) -> None:
    from sqlalchemy import LargeBinary

    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)
    inspector = inspect(engine)
    for table in ("users", "account_tokens", "device_grants", "scheduled_tasks", "hosts"):
        assert "account_generation" in {c["name"] for c in inspector.get_columns(table)}
    assert "deleted_at" in {c["name"] for c in inspector.get_columns("users")}
    columns = {
        c["name"]: c["type"] for c in inspector.get_columns("omnigent_conversation_metadata")
    }
    assert isinstance(columns["inference_snapshot"], LargeBinary)


@pytest.mark.parametrize("failures", [0, 1, 4])
def test_account_schema_publication_retries_from_durable_revision(
    db_uri: str, failures: int
) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.device_grant_store import DeviceGrantStore

    engine = _crdb_engine(db_uri)
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("publication-user", "retained-password-hash")
    grants = DeviceGrantStore(db_uri)
    grants.create_redeemed_grant(
        "publication-grant",
        user_id="publication-user",
        client_id="cli",
        refresh_token_hash="retained-refresh-hash",
        created_at=100,
    )
    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        command.downgrade(config, CRDB_BASELINE_REVISION)
        connection.commit()
    assert "account_generation" not in {c["name"] for c in inspect(engine).get_columns("users")}
    assert "inference_snapshot" not in {
        c["name"] for c in inspect(engine).get_columns("omnigent_conversation_metadata")
    }
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        connection.execute(text("CREATE TABLE migration_conflict (id INT PRIMARY KEY, value INT)"))
        connection.commit()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO migration_conflict VALUES (1, 0), (2, 0)"))
    attempts = 0

    def mark_publication(conn, cursor, statement, parameters, context, executemany):
        nonlocal attempts
        if statement.startswith("ALTER TABLE users ADD COLUMN deleted_at"):
            if failures == 0:
                attempts += 1
                conn.exec_driver_sql("SELECT 1 / 0")
            conn.info["account_publication"] = True

    def fail_publication(conn):
        nonlocal attempts
        if conn.info.pop("account_publication", False):
            attempts += 1
            if attempts <= failures:
                conn.execute(
                    text("SELECT value FROM migration_conflict WHERE id = 1")
                ).scalar_one()
                with engine.connect() as competing:
                    competing.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    competing.execute(
                        text("SELECT value FROM migration_conflict WHERE id = 2")
                    ).scalar_one()
                    competing.execute(
                        text("UPDATE migration_conflict SET value = value + 1 WHERE id = 1")
                    )
                    competing.commit()
                conn.execute(text("UPDATE migration_conflict SET value = value + 1 WHERE id = 2"))

    event.listen(engine, "before_cursor_execute", mark_publication)
    event.listen(engine, "commit", fail_publication)
    try:
        if failures in (0, 4):
            with pytest.raises(RuntimeError, match="schema migration failed") as caught:
                _initialize_or_verify_schema(engine, db_uri)
            assert isinstance(caught.value.__cause__, DBAPIError)
            assert caught.value.__cause__.orig.sqlstate == ("40001" if failures == 4 else "22012")
            assert attempts == (4 if failures == 4 else 1)
            assert _get_current_db_revision(engine) == CRDB_BASELINE_REVISION
            assert "account_generation" not in {
                c["name"] for c in inspect(engine).get_columns("users")
            }
        else:
            _initialize_or_verify_schema(engine, db_uri)
            assert attempts == 2
    finally:
        event.remove(engine, "before_cursor_execute", mark_publication)
        event.remove(engine, "commit", fail_publication)
    _initialize_or_verify_schema(engine, db_uri)
    _assert_head_schema(engine, db_uri)
    account = accounts.get_user("publication-user")
    assert account is not None and len(account.account_generation) == 32
    assert accounts.get_password_hash("publication-user") == "retained-password-hash"
    grant = grants.authorize_access("publication-grant")
    assert grant is not None and grant.account_generation == account.account_generation


def test_bootstrap_marker_cannot_stamp_missing_columns(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    head = _get_head_db_revision(db_uri)
    version = _crdb_server_version(engine)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, version)
        connection.execute(text("ALTER TABLE users DROP COLUMN account_generation"))
        connection.execute(
            text(
                f"CREATE TABLE {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                "(token STRING PRIMARY KEY, target_revision STRING NOT NULL)"
            )
        )
        connection.commit()
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                "(token, target_revision) VALUES (:token, :head)"
            ),
            {"token": _CRDB_BOOTSTRAP_MARKER_TOKEN, "head": head},
        )
        connection.execute(text("DELETE FROM alembic_version"))
    with pytest.raises(RuntimeError, match=r"missing columns.*users\.account_generation"):
        _initialize_or_verify_schema(engine, db_uri)
    assert _get_current_db_revision(engine) is None
    assert _CRDB_BOOTSTRAP_MARKER_TABLE in inspect(engine).get_table_names()


@pytest.mark.parametrize("boundary", ["account_backfill", "snapshot_rename"])
def test_migration_retry_resumes_published_work(db_uri: str, boundary: str) -> None:
    import sqlalchemy as sa

    from omnigent.db.compression import decode, encode
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    engine = _crdb_engine(db_uri)
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("resume-user", "retained-password")
    if boundary == "snapshot_rename":
        metadata = sa.Table("omnigent_conversation_metadata", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                metadata.insert().values(
                    workspace_id=0,
                    id=b"s" * 16,
                    kind=1,
                    inference_snapshot=encode('{"saved":"profile"}'),
                    task_summary="retained summary",
                )
            )
    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        command.downgrade(
            config, CRDB_BASELINE_REVISION if boundary == "account_backfill" else "kk1a2b3c4d5e"
        )
        connection.commit()
    attempts = 0
    durable_revision = None

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        nonlocal attempts, durable_revision
        matches = (
            statement.startswith("UPDATE users SET account_generation")
            if boundary == "account_backfill"
            else statement.startswith(
                "ALTER TABLE omnigent_conversation_metadata RENAME _inference_snapshot_blob"
            )
        )
        if matches:
            attempts += 1
            if attempts == 1:
                durable_revision = _get_current_db_revision(engine)
                conn.execute(text("SELECT id FROM users WHERE id = 'resume-user'")).scalar_one()
                conn.exec_driver_sql("SELECT crdb_internal.force_retry('10s')")

    sa.event.listen(engine, "before_cursor_execute", interrupt)
    try:
        _initialize_or_verify_schema(engine, db_uri)
    finally:
        sa.event.remove(engine, "before_cursor_execute", interrupt)
    assert attempts == 2
    assert durable_revision == (
        "hh1b2c3d4e5f" if boundary == "account_backfill" else "kk1a2b3c4d5e"
    )
    _assert_head_schema(engine, db_uri)
    account = accounts.get_user("resume-user")
    assert account is not None and len(account.account_generation) == 32
    assert accounts.get_password_hash("resume-user") == "retained-password"
    if boundary == "snapshot_rename":
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT inference_snapshot, task_summary FROM omnigent_conversation_metadata")
            ).one()
        assert decode(row.inference_snapshot) == '{"saved":"profile"}'
        assert row.task_summary == "retained summary"

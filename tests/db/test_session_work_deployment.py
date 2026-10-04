"""Exercise deployment against independent, initially unmigrated databases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

from omnigent.db.utils import _ensure_conversation_tables, normalize_database_url

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy_session_work_schema.py"
SESSION = bytes.fromhex("12345678123456781234567812345678")
OPERATION = bytes.fromhex("23456781234567812345678123456781")
INCARNATION = bytes.fromhex("34567812345678123456781234567812")
CLAIM = bytes.fromhex("45678123456781234567812345678123")
AGENT = bytes.fromhex("56781234567812345678123456781234")
RESERVATION = bytes.fromhex("67812345678123456781234567812345")


@pytest.fixture
def uninitialized_database(tmp_path, record_property):
    configured = os.environ.get("OMNIGENT_TEST_DB_URI")
    if not configured:
        uri = f"sqlite:///{tmp_path / 'deployment.db'}"
        engine = sa.create_engine(uri)
        try:
            assert sa.inspect(engine).get_table_names() == []
            yield uri, engine
        finally:
            engine.dispose()
        return

    url = sa.make_url(normalize_database_url(configured))
    name = "session_work_test_" + uuid.uuid4().hex
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    quoted = admin.dialect.identifier_preparer.quote(name)
    with admin.connect() as connection:
        connection.execute(sa.text(f"CREATE DATABASE {quoted}"))
    uri = url.set(database=name).render_as_string(hide_password=False)
    engine = sa.create_engine(uri)
    try:
        assert engine.dialect.name == url.get_backend_name()
        with engine.connect() as connection:
            server = str(connection.execute(sa.text("SELECT version()")).scalar_one())
        record_property("database_server", server)
        if engine.dialect.name == "cockroachdb":
            assert "CockroachDB" in server
            expected = os.environ.get("OMNIGENT_TEST_CRDB_VERSION")
            if expected:
                assert f"v{expected}" in server
        elif engine.dialect.name == "postgresql":
            assert "PostgreSQL" in server and "CockroachDB" not in server
        assert sa.inspect(engine).get_table_names() == []
        yield uri, engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            suffix = " CASCADE" if admin.dialect.name == "cockroachdb" else ""
            connection.execute(sa.text(f"DROP DATABASE {quoted}{suffix}"))
        admin.dispose()


def operator(uri, role="split-ap", *, code=None):
    args = ["--role", role, "--target", "mm1a2b3c4d5e", "--uri-env", "SCHEMA_TEST_URI"]
    command = [sys.executable, str(SCRIPT), *args]
    if code:
        command = [sys.executable, "-c", code, str(SCRIPT), *args]
    return subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "SCHEMA_TEST_URI": uri},
        text=True,
        capture_output=True,
        timeout=180,
    )


@contextmanager
def schema_connection(engine):
    from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction

    version = _crdb_server_version(engine) if engine.dialect.name == "cockroachdb" else None
    with engine.connect() as connection:
        if version is not None:
            _prepare_crdb_schema_transaction(connection, version)
        yield connection
        connection.commit()


def rows(engine, table):
    with engine.connect() as connection:
        return [
            tuple(bytes(v) if isinstance(v, memoryview) else v for v in row)
            for row in connection.execute(sa.text(f"SELECT * FROM {table}"))
        ]


def catalog(engine, tables):
    inspector = sa.inspect(engine)
    return {
        table: {
            "columns": [
                {**column, "type": str(column["type"])} for column in inspector.get_columns(table)
            ],
            "pk": inspector.get_pk_constraint(table),
            "checks": inspector.get_check_constraints(table),
            "indexes": inspector.get_indexes(table),
            "fk": inspector.get_foreign_keys(table),
        }
        for table in tables
    }


def seed_ap(engine):
    from omnigent.db.utils import _create_engine

    managed = _create_engine(engine.url.render_as_string(hide_password=False))
    try:
        _ensure_conversation_tables(managed)
    finally:
        managed.dispose()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO conversations (workspace_id, id, created_at, updated_at, title, "
                "root_conversation_id, next_position, agent_id, session_overrides, archived) "
                "VALUES (7, :id, 123, 456, 'old conversation', :id, 3, :agent, NULL, false)"
            ),
            {"id": SESSION, "agent": AGENT},
        )
        connection.execute(
            sa.text(
                "INSERT INTO conversation_items (workspace_id, conversation_id, id, response_id, "
                "created_at, status, position, type, data, search_text, created_by) "
                "VALUES (7, :session, :id, 'old response', 123, 1, 2, 1, "
                "'{}', 'old item', 'old user')"
            ),
            {"session": SESSION, "id": OPERATION},
        )
        connection.execute(
            sa.text(
                "INSERT INTO conversation_labels "
                "(workspace_id, conversation_id, key, value, updated_at) "
                "VALUES (7, :id, 'integrity', 'old label', 456)"
            ),
            {"id": SESSION},
        )


def test_operator_deploys_independent_ap(uninitialized_database):
    uri, engine = uninitialized_database
    seed_ap(engine)
    existing = set(sa.inspect(engine).get_table_names())
    assert {"conversations", "conversation_items", "conversation_labels"} <= existing
    assert not {"session_work", "session_work_admission", "alembic_version"} & existing
    old_tables = ("conversations", "conversation_items", "conversation_labels")
    before_rows = {table: rows(engine, table) for table in old_tables}
    assert before_rows["conversations"][0][4] == "old conversation"
    assert before_rows["conversation_items"][0][9] == "old item"
    assert before_rows["conversation_labels"][0][3] == "old label"
    before_catalog = catalog(engine, old_tables)

    result = operator(uri)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "role": "split-ap",
        "target": "mm1a2b3c4d5e",
        "backend": engine.dialect.name,
        "verified_objects": [
            "session_work_admission",
            "session_work",
            "ck_session_work_claim",
            "ix_session_work_unfinished",
        ],
    }
    assert set(sa.inspect(engine).get_table_names()) == existing | {
        "session_work",
        "session_work_admission",
    }
    assert {table: rows(engine, table) for table in old_tables} == before_rows
    assert catalog(engine, old_tables) == before_catalog


def test_fresh_shared_crdb_requires_ownership_before_stamp(uninitialized_database):
    uri, engine = uninitialized_database
    if engine.dialect.name != "cockroachdb":
        pytest.skip("requires configured CockroachDB; SQLite does not qualify bootstrap")
    from alembic import command

    from omnigent.db.utils import _create_engine, _initialize_or_verify_schema

    original = command.stamp
    observed = []

    def checked_stamp(config, target, **kwargs):
        tables = set(sa.inspect(engine).get_table_names())
        assert {"session_work_admission", "session_work"} <= tables
        assert sa.inspect(engine).get_indexes("session_work")[0]["column_names"] == [
            "workspace_id",
            "session_id",
            "state",
            "operation_id",
        ]
        observed.append(target)
        return original(config, target, **kwargs)

    managed = _create_engine(uri)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(command, "stamp", checked_stamp)
            _initialize_or_verify_schema(managed, uri)
    finally:
        managed.dispose()
    assert observed == ["mm1a2b3c4d5e"]
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "mm1a2b3c4d5e"
        )
    assert "omnigent_crdb_bootstrap" not in sa.inspect(engine).get_table_names()

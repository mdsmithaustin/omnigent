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
    snapshot = {
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
    return json.loads(json.dumps(snapshot, default=str))


def seed_ap(engine):
    from omnigent.db.utils import _create_engine

    managed = _create_engine(engine.url.render_as_string(hide_password=False))
    try:
        _ensure_conversation_tables(managed)
    finally:
        managed.dispose()
    key = engine.dialect.identifier_preparer.quote("key")
    value = engine.dialect.identifier_preparer.quote("value")
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
                f"(workspace_id, conversation_id, {key}, {value}, updated_at) "
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
        assert next(
            i
            for i in sa.inspect(engine).get_indexes("session_work")
            if i["name"] == "ix_session_work_unfinished"
        )["column_names"] == [
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


def seed_ownership(engine):
    params = {
        "session": SESSION,
        "operation": OPERATION,
        "agent": AGENT,
        "incarnation": INCARNATION,
        "claim": CLAIM,
        "reservation": RESERVATION,
    }
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO session_work_admission "
                "(workspace_id, session_id, agent_id, runner_id, incarnation, reservation_id) "
                "VALUES (7, :session, :agent, 'old runner', :incarnation, :reservation)"
            ),
            params,
        )
        connection.execute(
            sa.text(
                "INSERT INTO session_work "
                "(workspace_id, session_id, operation_id, agent_id, runner_id, incarnation, "
                "kind, state, claim_id) "
                "VALUES (7, :session, :operation, :agent, 'old runner', "
                ":incarnation, 1, 2, :claim)"
            ),
            params,
        )


def assert_ownership(engine):
    assert rows(engine, "session_work_admission") == [
        (7, SESSION, AGENT, "old runner", INCARNATION, RESERVATION)
    ]
    assert rows(engine, "session_work") == [
        (7, SESSION, OPERATION, AGENT, "old runner", INCARNATION, 1, 2, CLAIM)
    ]


def test_ap_repeat_preserves_ownership(uninitialized_database):
    uri, engine = uninitialized_database
    result = operator(uri)
    assert result.returncode == 0, result.stderr
    seed_ownership(engine)
    for _ in range(2):
        result = operator(uri)
        assert result.returncode == 0, result.stderr
        assert_ownership(engine)
    assert set(sa.inspect(engine).get_table_names()) == {"session_work_admission", "session_work"}


@pytest.mark.parametrize("missing", ["session_work", "session_work_admission", "index"])
def test_ap_partial_apply_recovers(uninitialized_database, missing):
    uri, engine = uninitialized_database
    assert operator(uri).returncode == 0
    seed_ownership(engine)
    with schema_connection(engine) as connection:
        if missing == "index":
            sql = "DROP INDEX ix_session_work_unfinished"
            if engine.dialect.name == "mysql":
                sql += " ON session_work"
            connection.execute(sa.text(sql))
        else:
            connection.execute(sa.text(f"DROP TABLE {missing}"))
    existing = set(sa.inspect(engine).get_table_names())
    before = {table: rows(engine, table) for table in existing}
    result = operator(uri)
    assert result.returncode == 0, result.stderr
    assert {table: rows(engine, table) for table in existing} == before
    assert set(sa.inspect(engine).get_table_names()) == {"session_work_admission", "session_work"}
    found = next(
        i
        for i in sa.inspect(engine).get_indexes("session_work")
        if i["name"] == "ix_session_work_unfinished"
    )
    assert found["column_names"] == ["workspace_id", "session_id", "state", "operation_id"]


def ownership_ddl(engine, *, admission=False):
    binary = {
        "sqlite": "BLOB",
        "postgresql": "BYTEA",
        "cockroachdb": "BYTES",
        "mysql": "BINARY(16)",
    }[engine.dialect.name]
    if admission:
        return (
            "CREATE TABLE session_work_admission (workspace_id BIGINT NOT NULL, "
            f"session_id {binary} NOT NULL, agent_id {binary}, runner_id VARCHAR(64) NOT NULL, "
            f"incarnation {binary} NOT NULL, reservation_id {binary}, "
            "PRIMARY KEY (workspace_id, session_id))"
        )
    return (
        f"CREATE TABLE session_work (workspace_id BIGINT NOT NULL, session_id {binary} NOT NULL, "
        f"operation_id {binary} NOT NULL, agent_id {binary}, runner_id VARCHAR(64) NOT NULL, "
        f"incarnation {binary} NOT NULL, kind SMALLINT NOT NULL, state SMALLINT NOT NULL, "
        f"claim_id {binary}, PRIMARY KEY (workspace_id, session_id, operation_id), "
        "CONSTRAINT ck_session_work_claim CHECK "
        "((state IN (1, 5) AND claim_id IS NULL) OR "
        "(state IN (2, 3, 4) AND claim_id IS NOT NULL)))"
    )


@pytest.mark.parametrize(
    "drift",
    [
        "column_type",
        "runner_width",
        "numeric_width",
        "extra_column",
        "missing_check",
        "nullable",
        "default",
        "pk_order",
        "check",
        "check_name",
        "extra_check",
        "extra_unique",
        "foreign_key",
        "unique_index",
        "wrong_index_order",
        "descending_index",
        "partial_index",
        "expression_index",
        "view_collision",
        "index_collision",
        "expiry",
    ],
)
def test_ap_drift_refuses_without_changes(uninitialized_database, drift):
    uri, engine = uninitialized_database
    if drift == "partial_index" and engine.dialect.name == "mysql":
        pytest.skip("MySQL has no partial indexes")
    if drift == "expiry" and engine.dialect.name != "cockroachdb":
        pytest.skip("TTL storage parameters require CockroachDB")
    ddl = ownership_ddl(engine)
    changes = {
        "column_type": ("runner_id VARCHAR(64)", "runner_id TEXT"),
        "runner_width": ("VARCHAR(64)", "VARCHAR(65)"),
        "numeric_width": ("kind SMALLINT", "kind BIGINT"),
        "nullable": ("runner_id VARCHAR(64) NOT NULL", "runner_id VARCHAR(64)"),
        "default": ("kind SMALLINT NOT NULL", "kind SMALLINT NOT NULL DEFAULT 1"),
        "pk_order": (
            "PRIMARY KEY (workspace_id, session_id, operation_id)",
            "PRIMARY KEY (session_id, workspace_id, operation_id)",
        ),
        "check": ("state IN (1, 5)", "state IN (1, 5, 6)"),
        "check_name": ("ck_session_work_claim", "different_claim_check"),
    }
    if drift in changes:
        ddl = ddl.replace(*changes[drift])
    if drift == "missing_check":
        ddl = ddl[: ddl.index(", CONSTRAINT ck_session_work_claim")] + ")"
    if drift == "extra_column":
        ddl = ddl.replace(", PRIMARY KEY", ", extra INTEGER, PRIMARY KEY", 1)
    if drift == "extra_check":
        ddl = ddl[:-1] + ", CONSTRAINT extra_kind CHECK (kind = 1))"
    if drift == "extra_unique":
        ddl = ddl[:-1] + ", CONSTRAINT extra_unique UNIQUE (runner_id))"
    if drift == "foreign_key":
        ddl = ddl[:-1] + ", FOREIGN KEY (workspace_id) REFERENCES other_workspaces(id))"
    if drift == "expiry":
        ddl += " WITH (ttl_expire_after = '1 hour')"
    with schema_connection(engine) as connection:
        if drift == "foreign_key":
            connection.execute(sa.text("CREATE TABLE other_workspaces (id BIGINT PRIMARY KEY)"))
            connection.execute(sa.text("INSERT INTO other_workspaces VALUES (7)"))
        if drift == "view_collision":
            connection.execute(sa.text("CREATE VIEW session_work AS SELECT 7 AS workspace_id"))
        else:
            connection.execute(sa.text(ddl))
        if drift == "index_collision":
            connection.execute(sa.text("CREATE TABLE other_workspaces (id BIGINT PRIMARY KEY)"))
            connection.execute(
                sa.text("CREATE INDEX ix_session_work_unfinished ON other_workspaces (id)")
            )
        if drift in {
            "unique_index",
            "wrong_index_order",
            "descending_index",
            "partial_index",
            "expression_index",
        }:
            columns = "workspace_id, session_id, state, operation_id"
            if drift == "wrong_index_order":
                columns = "workspace_id, state, session_id, operation_id"
            if drift == "descending_index":
                columns = "workspace_id, session_id, state DESC, operation_id"
            if drift == "expression_index":
                columns = "workspace_id, session_id, (state + 1), operation_id"
            unique = "UNIQUE " if drift == "unique_index" else ""
            predicate = " WHERE state = 1" if drift == "partial_index" else ""
            connection.execute(
                sa.text(
                    f"CREATE {unique}INDEX ix_session_work_unfinished "
                    f"ON session_work ({columns}){predicate}"
                )
            )
    if drift != "view_collision":
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO session_work (workspace_id, session_id, operation_id, agent_id, "
                    "runner_id, incarnation, kind, state, claim_id) "
                    "VALUES (7, :session, :operation, :agent, 'old runner', "
                    ":incarnation, 1, 2, :claim)"
                ),
                {
                    "session": SESSION,
                    "operation": OPERATION,
                    "agent": AGENT,
                    "incarnation": INCARNATION,
                    "claim": CLAIM,
                },
            )
    tables = set(sa.inspect(engine).get_table_names())
    before_rows = {table: rows(engine, table) for table in tables}
    before_catalog = catalog(engine, tables)
    result = operator(uri)
    assert result.returncode == 1, result.stdout
    assert "Incompatible" in json.loads(result.stderr)["error"]
    assert set(sa.inspect(engine).get_table_names()) == tables
    assert {table: rows(engine, table) for table in tables} == before_rows
    assert catalog(engine, tables) == before_catalog


def test_backend_schema_semantics_and_lookup(uninitialized_database):
    uri, engine = uninitialized_database
    result = operator(uri)
    assert result.returncode == 0, result.stderr
    inspector = sa.inspect(engine)
    assert inspector.get_pk_constraint("session_work")["constrained_columns"] == [
        "workspace_id",
        "session_id",
        "operation_id",
    ]
    assert [(c["name"], c["nullable"]) for c in inspector.get_columns("session_work")] == [
        ("workspace_id", False),
        ("session_id", False),
        ("operation_id", False),
        ("agent_id", True),
        ("runner_id", False),
        ("incarnation", False),
        ("kind", False),
        ("state", False),
        ("claim_id", True),
    ]
    binary = {
        "sqlite": "BLOB",
        "postgresql": "BYTEA",
        "cockroachdb": "BLOB",
        "mysql": "BINARY(16)",
    }[engine.dialect.name]
    integer = "INTEGER" if engine.dialect.name == "cockroachdb" else "BIGINT"
    smallint = "INTEGER" if engine.dialect.name == "cockroachdb" else "SMALLINT"
    assert [str(c["type"]) for c in inspector.get_columns("session_work")] == [
        integer,
        binary,
        binary,
        binary,
        "VARCHAR(64)",
        binary,
        smallint,
        smallint,
        binary,
    ]
    insert = sa.text(
        "INSERT INTO session_work (workspace_id, session_id, operation_id, runner_id, "
        "incarnation, kind, state, claim_id) VALUES "
        "(7, :session, :operation, 'lookup runner', :incarnation, 1, :state, :claim)"
    )
    rejected_rows = []
    for state in (1, 2, 3, 4, 5):
        params = {
            "session": SESSION,
            "operation": state.to_bytes(16, "big"),
            "incarnation": INCARNATION,
            "state": state,
            "claim": CLAIM if state in (2, 3, 4) else None,
        }
        with engine.begin() as connection:
            connection.execute(insert, params)
        rejected_rows.append(
            {
                **params,
                "operation": (state + 10).to_bytes(16, "big"),
                "claim": None if params["claim"] is not None else CLAIM,
            }
        )
    for state in (0, 6):
        rejected_rows.append(
            {
                "session": SESSION,
                "operation": state.to_bytes(16, "big"),
                "incarnation": INCARNATION,
                "state": state,
                "claim": CLAIM,
            }
        )
    expected_error = sa.exc.DBAPIError if engine.dialect.name == "mysql" else sa.exc.IntegrityError
    for invalid in rejected_rows:
        with pytest.raises(expected_error) as rejected, engine.begin() as connection:
            connection.execute(insert, invalid)
        if engine.dialect.name == "mysql":
            assert rejected.value.orig.args[0] == 3819
            assert "'ck_session_work_claim'" in str(rejected.value.orig)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text(
                        "SELECT count(*) FROM session_work WHERE workspace_id = 7 "
                        "AND session_id = :session AND operation_id = :operation"
                    ),
                    invalid,
                ).scalar_one()
                == 0
            )
    with engine.begin() as connection:
        connection.execute(
            insert,
            [
                {
                    "session": number.to_bytes(16, "big"),
                    "operation": OPERATION,
                    "incarnation": INCARNATION,
                    "state": 4,
                    "claim": CLAIM,
                }
                for number in range(1000)
            ],
        )
    query = (
        "SELECT operation_id FROM session_work WHERE workspace_id = 7 "
        "AND session_id = :session AND state IN (1, 2, 3) LIMIT 1"
    )
    prefix = "EXPLAIN QUERY PLAN " if engine.dialect.name == "sqlite" else "EXPLAIN "
    with engine.connect() as connection:
        values = connection.execute(sa.text(query), {"session": SESSION}).all()
        assert len(values) == 1 and bytes(values[0][0]) in {
            n.to_bytes(16, "big") for n in (1, 2, 3)
        }
        plan = str(connection.execute(sa.text(prefix + query), {"session": SESSION}).all())
    assert "ix_session_work_unfinished" in plan, plan


@pytest.mark.parametrize("drift", ["invisible_index", "prefix_index", "unenforced_check"])
def test_mysql_native_drift_refuses_without_changes(uninitialized_database, drift):
    uri, engine = uninitialized_database
    if engine.dialect.name != "mysql":
        pytest.skip("requires MySQL native index and check metadata")
    ddl = ownership_ddl(engine)
    if drift == "unenforced_check":
        ddl = ddl[:-1] + " NOT ENFORCED)"
    with schema_connection(engine) as connection:
        connection.execute(sa.text(ddl))
        columns = "workspace_id, session_id, state, operation_id"
        if drift == "prefix_index":
            columns = "workspace_id, session_id, state, operation_id(8)"
        visibility = " INVISIBLE" if drift == "invisible_index" else ""
        connection.execute(
            sa.text(
                f"CREATE INDEX ix_session_work_unfinished ON session_work ({columns}){visibility}"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO session_work (workspace_id, session_id, operation_id, runner_id, "
                "incarnation, kind, state, claim_id) "
                "VALUES (7, :session, :operation, 'old runner', :incarnation, 1, 2, :claim)"
            ),
            {
                "session": SESSION,
                "operation": OPERATION,
                "incarnation": INCARNATION,
                "claim": CLAIM,
            },
        )
    before = rows(engine, "session_work")
    with engine.connect() as connection:
        before_ddl = connection.execute(sa.text("SHOW CREATE TABLE session_work")).one()[1]
    result = operator(uri)
    assert result.returncode == 1, result.stdout
    assert "Incompatible" in json.loads(result.stderr)["error"]
    assert set(sa.inspect(engine).get_table_names()) == {"session_work"}
    assert rows(engine, "session_work") == before
    with engine.connect() as connection:
        assert connection.execute(sa.text("SHOW CREATE TABLE session_work")).one()[1] == before_ddl


def seed_shared_ll(uri, engine):
    from alembic import command

    from omnigent.db.db_models import ConversationBase, OmnigentBase
    from omnigent.db.utils import (
        _build_alembic_config,
    )

    config = _build_alembic_config(uri)
    with schema_connection(engine) as connection:
        config.attributes["connection"] = connection
        if engine.dialect.name == "cockroachdb":
            OmnigentBase.metadata.create_all(connection)
            connection.commit()
            from omnigent.db.cockroachdb import (
                _crdb_server_version,
                _prepare_crdb_schema_transaction,
            )

            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            ConversationBase.metadata.create_all(connection)
            connection.commit()
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            command.stamp(config, "ll1a2b3c4d5e")
        else:
            command.upgrade(config, "ll1a2b3c4d5e")


def test_shared_ll_upgrade_and_repeat(uninitialized_database):
    from omnigent.db.utils import _create_engine, _initialize_or_verify_schema

    uri, engine = uninitialized_database
    seed_shared_ll(uri, engine)
    seed_ap(engine)
    old_tables = ("conversations", "conversation_items", "conversation_labels")
    before = {table: rows(engine, table) for table in old_tables}
    result = operator(uri, "shared")
    assert result.returncode == 0, result.stderr
    seed_ownership(engine)
    assert operator(uri, "shared").returncode == 0
    managed = _create_engine(uri)
    try:
        _initialize_or_verify_schema(managed, uri)
    finally:
        managed.dispose()
    assert {table: rows(engine, table) for table in old_tables} == before
    assert_ownership(engine)
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "mm1a2b3c4d5e"
        )


@pytest.mark.parametrize("history", ["alembic_version", "omnigent_crdb_bootstrap"])
def test_ap_refuses_shared_history(uninitialized_database, history):
    uri, engine = uninitialized_database
    with schema_connection(engine) as connection:
        connection.execute(sa.text(f"CREATE TABLE {history} (value VARCHAR(64))"))
    result = operator(uri)
    assert result.returncode == 1
    assert json.loads(result.stderr) == {"error": "Split AP target contains shared history."}
    assert set(sa.inspect(engine).get_table_names()) == {history}


def test_operator_uses_managed_engine_and_redacts_secrets(tmp_path, monkeypatch):
    from omnigent.db import utils
    from omnigent.db.query_context import current_query_name

    uri = f"sqlite:///{tmp_path / 'owned.db'}"
    engine = utils._create_engine(uri)
    disposed = []
    query_names = []
    sa.event.listen(engine, "engine_disposed", lambda _: disposed.append(True))
    sa.event.listen(
        engine, "before_cursor_execute", lambda *args: query_names.append(current_query_name())
    )
    monkeypatch.setitem(utils._engine_cache, uri, object())
    monkeypatch.setattr(utils, "_create_engine", lambda _: engine)
    result = utils.deploy_session_work_schema(uri, role="split-ap", target="mm1a2b3c4d5e")
    assert result.verified_objects == (
        "session_work_admission",
        "session_work",
        "ck_session_work_claim",
        "ix_session_work_unfinished",
    )
    assert disposed == [True]
    assert set(query_names) == {"omnigent.database.deploy_session_work_schema"}
    assert set(sa.inspect(engine).get_table_names()) == {"session_work_admission", "session_work"}
    engine.dispose()
    secret = "DEPLOY_SECRET_CANARY_93d41"
    failure = operator(f"sqlite:///{tmp_path / secret / 'missing' / 'cannot-open.db'}")
    assert failure.returncode == 1
    assert json.loads(failure.stderr) == {"error": "Database schema deployment failed."}
    assert secret not in failure.stdout + failure.stderr
    credentialed = operator(f"postgresql://user:{secret}@127.0.0.1:invalid/no_db")
    assert credentialed.returncode == 1
    assert secret not in credentialed.stdout + credentialed.stderr
    assert "postgresql://" not in credentialed.stdout + credentialed.stderr


def test_failed_deployment_disposes_owned_engine(tmp_path, monkeypatch):
    from omnigent.db import utils

    uri = f"sqlite:///{tmp_path / 'drift.db'}"
    engine = utils._create_engine(uri)
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE session_work (wrong INTEGER)"))
    disposed = []
    sa.event.listen(engine, "engine_disposed", lambda _: disposed.append(True))
    monkeypatch.setattr(utils, "_create_engine", lambda _: engine)
    with pytest.raises(
        utils.SessionWorkDeploymentError, match="Incompatible session_work columns"
    ):
        utils.deploy_session_work_schema(uri, role="split-ap", target="mm1a2b3c4d5e")
    assert disposed == [True]
    assert set(sa.inspect(engine).get_table_names()) == {"session_work"}
    engine.dispose()


def test_artifact_target_mismatch_refuses_before_engine_creation(monkeypatch):
    from omnigent.db import utils

    monkeypatch.setattr(utils, "_get_head_db_revision", lambda _: "future_revision")
    with pytest.raises(utils.SessionWorkDeploymentError, match="artifact head"):
        utils.deploy_session_work_schema("sqlite://", role="split-ap", target="mm1a2b3c4d5e")


def fault_script(fragment, *, before=False):
    event = "before_execute" if before else "after_execute"
    commit = "" if before else "connection.commit()"
    return f"""
import os
import re
import runpy
import sys
from sqlalchemy import Engine, event

def fail(connection, clause, *args):
    sql = re.sub(r"\\s+", " ", str(clause)).strip().lower()
    if {fragment!r} in sql:
        {commit or "pass"}
        os._exit(73)

event.listen(Engine, {event!r}, fail)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "create table session_work_admission (",
        "create table session_work (",
        "create index ix_session_work_unfinished",
    ],
)
def test_ap_recovers_published_ddl_after_process_exit(uninitialized_database, boundary):
    uri, engine = uninitialized_database
    failed = operator(uri, code=fault_script(boundary))
    assert failed.returncode == 73, failed.stderr
    partial = set(sa.inspect(engine).get_table_names())
    assert "session_work_admission" in partial
    assert not {"alembic_version", "omnigent_crdb_bootstrap"} & partial
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO session_work_admission "
                "(workspace_id, session_id, agent_id, runner_id, incarnation, reservation_id) "
                "VALUES (7, :session, :agent, 'old runner', :incarnation, :reservation)"
            ),
            {
                "session": SESSION,
                "agent": AGENT,
                "incarnation": INCARNATION,
                "reservation": RESERVATION,
            },
        )
    result = operator(uri)
    assert result.returncode == 0, result.stderr
    assert rows(engine, "session_work_admission") == [
        (7, SESSION, AGENT, "old runner", INCARNATION, RESERVATION)
    ]
    assert set(sa.inspect(engine).get_table_names()) == {"session_work_admission", "session_work"}
    assert next(
        i
        for i in sa.inspect(engine).get_indexes("session_work")
        if i["name"] == "ix_session_work_unfinished"
    )["column_names"] == ["workspace_id", "session_id", "state", "operation_id"]


@pytest.mark.parametrize(
    "boundary",
    [
        "create table omnigent_crdb_bootstrap",
        "insert into omnigent_crdb_bootstrap",
        "create table agents (",
        "create table conversation_labels (",
        "create table session_work_admission (",
        "create table session_work (",
        "create index ix_session_work_unfinished",
        "insert into alembic_version",
        "drop table omnigent_crdb_bootstrap",
    ],
)
def test_fresh_shared_crdb_resumes_every_boundary(uninitialized_database, boundary):
    uri, engine = uninitialized_database
    if engine.dialect.name != "cockroachdb":
        pytest.skip("requires configured CockroachDB; SQLite does not qualify bootstrap")
    failed = operator(
        uri, "shared", code=fault_script(boundary, before=boundary.startswith("drop"))
    )
    assert failed.returncode == 73, failed.stderr
    assert "omnigent_crdb_bootstrap" in sa.inspect(engine).get_table_names()
    if "alembic_version" in sa.inspect(engine).get_table_names():
        revisions = rows(engine, "alembic_version")
        if revisions:
            assert revisions == [("mm1a2b3c4d5e",)]
            assert {"session_work", "session_work_admission"} <= set(
                sa.inspect(engine).get_table_names()
            )
    result = operator(uri, "shared")
    assert result.returncode == 0, result.stderr
    assert rows(engine, "alembic_version") == [("mm1a2b3c4d5e",)]
    assert "omnigent_crdb_bootstrap" not in sa.inspect(engine).get_table_names()
    seed_ownership(engine)
    assert operator(uri, "shared").returncode == 0
    assert_ownership(engine)


@pytest.mark.parametrize("at_head", [False, True])
@pytest.mark.parametrize("marker", ["valid", "wrong_token", "wrong_target", "extra_row"])
def test_crdb_marker_is_validated_before_repair(uninitialized_database, at_head, marker):
    uri, engine = uninitialized_database
    if engine.dialect.name != "cockroachdb":
        pytest.skip("requires configured CockroachDB; SQLite does not qualify bootstrap")
    if at_head:
        assert operator(uri, "shared").returncode == 0
        with schema_connection(engine) as connection:
            connection.execute(sa.text("DROP TABLE session_work"))
    with schema_connection(engine) as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE omnigent_crdb_bootstrap "
                "(token STRING PRIMARY KEY, target_revision STRING NOT NULL)"
            )
        )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO omnigent_crdb_bootstrap (token, target_revision) "
                "VALUES (:token, :target)"
            ),
            {
                "token": "wrong" if marker == "wrong_token" else "omnigent-crdb-bootstrap-v1",
                "target": "wrong" if marker == "wrong_target" else "mm1a2b3c4d5e",
            },
        )
        if marker == "extra_row":
            connection.execute(
                sa.text("INSERT INTO omnigent_crdb_bootstrap VALUES ('extra', 'mm1a2b3c4d5e')")
            )
    before = set(sa.inspect(engine).get_table_names())
    before_marker = rows(engine, "omnigent_crdb_bootstrap")
    result = operator(uri, "shared")
    if marker == "valid":
        assert result.returncode == 0, result.stderr
        assert rows(engine, "alembic_version") == [("mm1a2b3c4d5e",)]
        assert {"session_work_admission", "session_work"} <= set(
            sa.inspect(engine).get_table_names()
        )
        assert "omnigent_crdb_bootstrap" not in sa.inspect(engine).get_table_names()
    else:
        assert result.returncode == 1
        assert set(sa.inspect(engine).get_table_names()) == before
        assert rows(engine, "omnigent_crdb_bootstrap") == before_marker


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("drift", [False, True])
def test_shared_incomplete_mm_is_not_ready(uninitialized_database, marker, drift):
    uri, engine = uninitialized_database
    if engine.dialect.name != "cockroachdb":
        pytest.skip("requires configured CockroachDB; SQLite does not qualify bootstrap")
    assert operator(uri, "shared").returncode == 0
    seed_ownership(engine)
    with schema_connection(engine) as connection:
        connection.execute(sa.text("DROP TABLE session_work"))
        if drift:
            connection.execute(
                sa.text(ownership_ddl(engine).replace("VARCHAR(64)", "VARCHAR(65)"))
            )
        if marker:
            connection.execute(
                sa.text(
                    "CREATE TABLE omnigent_crdb_bootstrap "
                    "(token STRING PRIMARY KEY, target_revision STRING NOT NULL)"
                )
            )
    if marker:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO omnigent_crdb_bootstrap VALUES "
                    "('omnigent-crdb-bootstrap-v1', 'mm1a2b3c4d5e')"
                )
            )
    result = operator(uri, "shared")
    assert result.returncode == (1 if drift else 0), result.stderr
    assert rows(engine, "alembic_version") == [("mm1a2b3c4d5e",)]
    assert rows(engine, "session_work_admission") == [
        (7, SESSION, AGENT, "old runner", INCARNATION, RESERVATION)
    ]
    assert ("omnigent_crdb_bootstrap" in sa.inspect(engine).get_table_names()) == (
        marker and drift
    )
    if not drift:
        assert next(
            i
            for i in sa.inspect(engine).get_indexes("session_work")
            if i["name"] == "ix_session_work_unfinished"
        )["column_names"] == ["workspace_id", "session_id", "state", "operation_id"]


@pytest.mark.parametrize(
    "boundary",
    [
        "create table session_work_admission (",
        "create table session_work (",
        "create index ix_session_work_unfinished",
    ],
)
def test_shared_ll_resumes_published_ownership_ddl(uninitialized_database, boundary):
    uri, engine = uninitialized_database
    seed_shared_ll(uri, engine)
    failed = operator(uri, "shared", code=fault_script(boundary))
    assert failed.returncode == 73, failed.stderr
    assert rows(engine, "alembic_version") == [("ll1a2b3c4d5e",)]
    assert "session_work_admission" in sa.inspect(engine).get_table_names()
    result = operator(uri, "shared")
    assert result.returncode == 0, result.stderr
    assert rows(engine, "alembic_version") == [("mm1a2b3c4d5e",)]
    seed_ownership(engine)
    assert operator(uri, "shared").returncode == 0
    assert_ownership(engine)


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "mysql", "cockroachdb"])
def test_offline_migration_emits_schema_without_a_connection(dialect):
    import importlib
    import io

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module(
        "omnigent.db.migrations.versions.mm1a2b3c4d5e_add_session_work_admission"
    )
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect, opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE session_work_admission" in sql
    assert "PRIMARY KEY (workspace_id, session_id, operation_id)" in sql
    assert "CHECK ((state IN (1, 5) AND claim_id IS NULL) OR " in sql
    assert (
        "ix_session_work_unfinished ON session_work "
        "(workspace_id, session_id, state, operation_id)" in sql
    )
    assert {
        "sqlite": "session_id BLOB",
        "postgresql": "session_id BYTEA",
        "mysql": "session_id BINARY(16)",
        "cockroachdb": "session_id BYTEA",
    }[dialect] in sql


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown_revision",
        "prebaseline_revision",
        "unexpected_table",
        "malformed_marker",
        "ownership_drift",
    ],
)
def test_crdb_refuses_invalid_bootstrap_without_mutations(uninitialized_database, invalid):
    uri, engine = uninitialized_database
    if engine.dialect.name != "cockroachdb":
        pytest.skip("requires configured CockroachDB; SQLite does not qualify bootstrap")
    with schema_connection(engine) as connection:
        if invalid == "malformed_marker":
            connection.execute(
                sa.text("CREATE TABLE omnigent_crdb_bootstrap (wrong STRING PRIMARY KEY)")
            )
        else:
            connection.execute(
                sa.text(
                    "CREATE TABLE omnigent_crdb_bootstrap "
                    "(token STRING PRIMARY KEY, target_revision STRING NOT NULL)"
                )
            )
        if invalid in {"unknown_revision", "prebaseline_revision"}:
            connection.execute(
                sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            )
        if invalid == "unexpected_table":
            connection.execute(
                sa.text("CREATE TABLE unexpected_application (id BIGINT PRIMARY KEY)")
            )
        if invalid == "ownership_drift":
            connection.execute(
                sa.text(ownership_ddl(engine).replace("VARCHAR(64)", "VARCHAR(65)"))
            )
    with engine.begin() as connection:
        if invalid != "malformed_marker":
            connection.execute(
                sa.text(
                    "INSERT INTO omnigent_crdb_bootstrap VALUES "
                    "('omnigent-crdb-bootstrap-v1', 'mm1a2b3c4d5e')"
                )
            )
        if invalid in {"unknown_revision", "prebaseline_revision"}:
            connection.execute(
                sa.text("INSERT INTO alembic_version VALUES (:revision)"),
                {
                    "revision": "unknown_revision"
                    if invalid == "unknown_revision"
                    else "f1a2b3c4d5e6"
                },
            )
    tables = set(sa.inspect(engine).get_table_names())
    before = {table: rows(engine, table) for table in tables}
    result = operator(uri, "shared")
    assert result.returncode == 1
    assert set(sa.inspect(engine).get_table_names()) == tables
    assert {table: rows(engine, table) for table in tables} == before

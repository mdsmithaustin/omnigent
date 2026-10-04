from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.utils import _build_alembic_config


def test_existing_database_upgrades_and_retains_work(tmp_path):
    uri = f"sqlite:///{tmp_path / 'upgrade.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "ll1a2b3c4d5e")
    assert "session_work" not in sa.inspect(engine).get_table_names()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.execute(
            sa.text(
                "INSERT INTO session_work "
                "(workspace_id, session_id, operation_id, runner_id, incarnation, "
                "kind, state, claim_id) "
                "VALUES (0, :session_id, :operation_id, 'runner', :incarnation, "
                "1, 2, :claim_id)"
            ),
            {
                "session_id": bytes.fromhex("12345678123456781234567812345678"),
                "operation_id": bytes.fromhex("23456781234567812345678123456781"),
                "incarnation": bytes.fromhex("34567812345678123456781234567812"),
                "claim_id": bytes.fromhex("45678123456781234567812345678123"),
            },
        )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert connection.execute(sa.text("SELECT state, claim_id FROM session_work")).one() == (
            2,
            bytes.fromhex("45678123456781234567812345678123"),
        )
    engine.dispose()


def test_ownership_migration_emits_postgresql_ddl():
    import importlib
    import io

    migration = importlib.import_module(
        "omnigent.db.migrations.versions.mm1a2b3c4d5e_add_session_work_admission"
    )
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    ddl = output.getvalue()
    assert "CREATE TABLE session_work_admission" in ddl
    assert "CREATE TABLE session_work" in ddl
    assert "workspace_id BIGINT" in ddl
    assert "session_id BYTEA" in ddl
    assert "PRIMARY KEY (workspace_id, session_id, operation_id)" in ddl


def test_unfinished_work_query_uses_the_covering_index(tmp_path):
    uri = f"sqlite:///{tmp_path / 'query-plan.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "mm1a2b3c4d5e")
    with engine.connect() as connection:
        plan = connection.execute(
            sa.text(
                "EXPLAIN QUERY PLAN SELECT operation_id FROM session_work "
                "WHERE workspace_id = 0 AND session_id = :session_id "
                "AND state IN (1, 2, 3) LIMIT 1"
            ),
            {"session_id": bytes.fromhex("12345678123456781234567812345678")},
        ).all()
    assert [row[3] for row in plan] == [
        "SEARCH session_work USING COVERING INDEX ix_session_work_unfinished "
        "(workspace_id=? AND session_id=? AND state=?)"
    ]


def test_isolated_ap_ddl_fixture_does_not_create_metadata_history(tmp_path):
    import importlib

    migration = importlib.import_module(
        "omnigent.db.migrations.versions.mm1a2b3c4d5e_add_session_work_admission"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'split-ap.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE conversations (id BLOB PRIMARY KEY)"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
    assert set(sa.inspect(engine).get_table_names()) == {
        "conversations",
        "session_work_admission",
        "session_work",
    }
    engine.dispose()

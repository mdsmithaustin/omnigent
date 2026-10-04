"""Migration exits leave pooled CockroachDB connections safe for schema work."""

from __future__ import annotations

import pytest
from alembic import command
from packaging.version import Version
from sqlalchemy import event, inspect

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.utils import (
    _build_alembic_config,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
)
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from tests.db.test_cockroachdb import _crdb_engine


@pytest.mark.parametrize("prior_setting", ["on", "off"])
@pytest.mark.parametrize("restoration_fails", [False, True])
def test_failed_migration_restores_or_discards_connection(
    db_uri: str, prior_setting: str, restoration_fails: bool
) -> None:
    engine = _crdb_engine(db_uri)
    version = _crdb_server_version(engine)
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("setting-user", "retained-password")
    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, version)
        config.attributes["connection"] = connection
        command.downgrade(config, "hh1b2c3d4e5f")
        connection.commit()

    connections = [engine.connect() for _ in range(max(2, engine.pool.checkedin()))]
    try:
        for connection in connections:
            if version >= Version("24.1"):
                connection.exec_driver_sql(f"SET autocommit_before_ddl = {prior_setting}")
                connection.commit()
    finally:
        for connection in connections:
            connection.close()

    failed_drivers = []
    primary_errors = []
    restoration_errors = []
    migration_prior_settings = []

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if (
            version >= Version("24.1")
            and statement == "SET autocommit_before_ddl = true"
            and not primary_errors
        ):
            migration_prior_settings.append(
                (
                    conn.connection.driver_connection,
                    conn.exec_driver_sql("SHOW autocommit_before_ddl").scalar_one(),
                )
            )
        if statement.startswith("UPDATE users SET account_generation"):
            failed_drivers.append(conn.connection.driver_connection)
            try:
                conn.exec_driver_sql("SELECT 1 / 0")
            except Exception as error:
                primary_errors.append(error)
                raise
        restoration_statement = (
            "SET autocommit_before_ddl = true"
            if version >= Version("24.1")
            else "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        if restoration_fails and primary_errors and statement == restoration_statement:
            error = RuntimeError("injected session restoration failure")
            restoration_errors.append(error)
            raise error

    with engine.connect() as operator_connection:
        operator_driver = operator_connection.connection.driver_connection
        if version >= Version("24.1"):
            operator_connection.exec_driver_sql("SET autocommit_before_ddl = false")
            operator_connection.commit()

        event.listen(engine, "before_cursor_execute", interrupt)
        try:
            with pytest.raises(RuntimeError, match="schema migration failed") as caught:
                _initialize_or_verify_schema(engine, db_uri)
        finally:
            event.remove(engine, "before_cursor_execute", interrupt)

        assert len(primary_errors) == 1
        assert len(failed_drivers) == 1
        failed_driver = failed_drivers[0]
        assert failed_driver is not operator_driver
        if version >= Version("24.1"):
            assert len(migration_prior_settings) == 1
            assert migration_prior_settings[0][0] is failed_driver
            assert migration_prior_settings[0][1] == prior_setting
        assert caught.value.__cause__ is primary_errors[0]
        assert primary_errors[0].orig.sqlstate == "22012"
        assert _get_current_db_revision(engine) == "hh1b2c3d4e5f"
        assert "account_generation" in {c["name"] for c in inspect(engine).get_columns("users")}
        assert len(restoration_errors) == int(restoration_fails)
        if restoration_fails:
            assert failed_driver.closed

        connections = [engine.connect() for _ in range(engine.pool.checkedin())]
        assert connections
        try:
            reused_failed_driver = False
            for index, connection in enumerate(connections):
                driver = connection.connection.driver_connection
                reused_failed_driver |= driver is failed_driver
                assert (
                    connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()
                    == "read committed"
                )
                if driver is failed_driver and version >= Version("24.1"):
                    assert (
                        connection.exec_driver_sql("SHOW autocommit_before_ddl").scalar_one()
                        == "on"
                    )
                else:
                    connection.rollback()
                    _prepare_crdb_schema_transaction(connection, version)
                connection.exec_driver_sql(
                    f"CREATE TABLE setting_check_{index} (id INT PRIMARY KEY)"
                )
                connection.commit()
                assert inspect(connection).has_table(f"setting_check_{index}")
            assert reused_failed_driver == (not restoration_fails)
        finally:
            for connection in connections:
                connection.close()

        if version >= Version("24.1"):
            assert (
                operator_connection.exec_driver_sql("SHOW autocommit_before_ddl").scalar_one()
                == "off"
            )
            operator_connection.rollback()

        _initialize_or_verify_schema(engine, db_uri)
        assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)
        account = accounts.get_user("setting-user")
        assert account is not None and len(account.account_generation) == 32
        assert accounts.get_password_hash("setting-user") == "retained-password"
        assert operator_connection.connection.driver_connection is operator_driver
        assert not operator_driver.closed
        if version >= Version("24.1"):
            assert (
                operator_connection.exec_driver_sql("SHOW autocommit_before_ddl").scalar_one()
                == "off"
            )


def test_successful_migration_discards_connection_when_restoration_fails(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _crdb_engine(db_uri)
    version = _crdb_server_version(engine)
    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, version)
        config.attributes["connection"] = connection
        command.downgrade(config, "hh1b2c3d4e5f")
        connection.commit()

    completed_drivers = []
    upgrade = command.upgrade
    restoration_error = RuntimeError("injected successful migration restoration failure")

    def complete_upgrade(config, revision):
        upgrade(config, revision)
        completed_drivers.append(config.attributes["connection"].connection.driver_connection)

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        restoration_statement = (
            "SET autocommit_before_ddl = true"
            if version >= Version("24.1")
            else "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        if completed_drivers and statement == restoration_statement:
            raise restoration_error

    monkeypatch.setattr(command, "upgrade", complete_upgrade)
    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="schema migration failed") as caught:
            _initialize_or_verify_schema(engine, db_uri)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)

    assert caught.value.__cause__ is restoration_error
    assert len(completed_drivers) == 1
    assert completed_drivers[0].closed
    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)
    with engine.connect() as connection:
        assert connection.connection.driver_connection is not completed_drivers[0]
        _prepare_crdb_schema_transaction(connection, version)
        connection.exec_driver_sql("CREATE TABLE successful_setting_check (id INT PRIMARY KEY)")
        connection.commit()
        assert inspect(connection).has_table("successful_setting_check")
    _initialize_or_verify_schema(engine, db_uri)
    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)

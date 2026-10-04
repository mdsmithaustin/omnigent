"""Immutable Core schema and additive reconciliation for revision mm1a2b3c4d5e."""

from __future__ import annotations

import ast
import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import ReflectedIndex
from sqlalchemy.engine.reflection import Inspector

from omnigent.db.db_models import Uuid16

REVISION = "mm1a2b3c4d5e"


class SchemaDriftError(RuntimeError):
    """An existing object does not match this revision's row contract."""


def schema() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table(
        "session_work_admission",
        metadata,
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("session_id", Uuid16(), primary_key=True, nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=True),
        sa.Column("runner_id", sa.String(64), nullable=False),
        sa.Column("incarnation", Uuid16(), nullable=False),
        sa.Column("reservation_id", Uuid16(), nullable=True),
    )
    work = sa.Table(
        "session_work",
        metadata,
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("session_id", Uuid16(), primary_key=True, nullable=False),
        sa.Column("operation_id", Uuid16(), primary_key=True, nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=True),
        sa.Column("runner_id", sa.String(64), nullable=False),
        sa.Column("incarnation", Uuid16(), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("claim_id", Uuid16(), nullable=True),
        sa.CheckConstraint(
            "(state IN (1, 5) AND claim_id IS NULL) OR "
            "(state IN (2, 3, 4) AND claim_id IS NOT NULL)",
            name="ck_session_work_claim",
        ),
    )
    sa.Index(
        "ix_session_work_unfinished",
        work.c.workspace_id,
        work.c.session_id,
        work.c.state,
        work.c.operation_id,
    )
    return metadata


def verified_objects() -> tuple[str, ...]:
    metadata = schema()
    return (
        *metadata.tables,
        *(
            str(check.name)
            for table in metadata.tables.values()
            for check in table.constraints
            if isinstance(check, sa.CheckConstraint)
        ),
        *(str(index.name) for table in metadata.tables.values() for index in table.indexes),
    )


def _refuse(table: str, property_name: str) -> None:
    raise SchemaDriftError(f"Incompatible {table} {property_name}.")


def _check_expression(expression: str, dialect: str) -> str:
    """Compare the supported catalog spellings without discarding Boolean grouping."""
    expression = " ".join(expression.lower().split())
    if dialect == "postgresql":
        expression = re.sub(
            r"\bstate\s*=\s*any\s*\(array\[([0-9, ]+)\]\)", r"state in (\1)", expression
        )
    elif dialect == "mysql":
        expression = expression.replace("`state`", "state").replace("`claim_id`", "claim_id")
    expression = re.sub(r"\bnull\b", "None", expression)
    try:
        parsed = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        return "unrecognized"
    return ast.dump(parsed, include_attributes=False)


def _verify_table(connection: Connection, inspector: Inspector, table: sa.Table) -> None:
    dialect = connection.dialect
    columns = inspector.get_columns(table.name)
    if [column["name"] for column in columns] != list(table.c.keys()):
        _refuse(table.name, "columns")
    crdb_types = {}
    if dialect.name == "cockroachdb":
        crdb_types = dict(
            connection.execute(
                sa.text(
                    "SELECT column_name, crdb_sql_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table"
                ),
                {"table": table.name},
            )
            .tuples()
            .all()
        )
    for found, expected in zip(columns, table.c, strict=True):
        expected_type = expected.type.compile(dialect=dialect).upper()
        actual_type = found["type"].compile(dialect=dialect).upper()
        if dialect.name == "cockroachdb":
            expected_type = {"BIGINT": "INT8", "SMALLINT": "INT2", "BYTEA": "BYTES"}.get(
                expected_type, expected_type
            )
            actual_type = crdb_types.get(expected.name)
        if (
            actual_type != expected_type
            or found["nullable"] != expected.nullable
            or found.get("default") is not None
            or found.get("computed") is not None
            or found.get("identity") is not None
            or found.get("is_hidden", False)
        ):
            _refuse(table.name, f"column {expected.name}")
    if inspector.get_pk_constraint(table.name)["constrained_columns"] != [
        column.name for column in table.primary_key.columns
    ]:
        _refuse(table.name, "primary key")
    if inspector.get_foreign_keys(table.name):
        _refuse(table.name, "foreign keys")
    if inspector.get_unique_constraints(table.name):
        _refuse(table.name, "unique constraints")
    expected_checks = {
        check.name: _check_expression(str(check.sqltext), "sqlite")
        for check in table.constraints
        if isinstance(check, sa.CheckConstraint)
    }
    checks = inspector.get_check_constraints(table.name)
    if any(check.get("dialect_options") for check in checks):
        _refuse(table.name, "check options")
    if (
        len(checks) != len(expected_checks)
        or {check["name"]: _check_expression(check["sqltext"], dialect.name) for check in checks}
        != expected_checks
    ):
        _refuse(table.name, "check constraints")
    if dialect.name == "sqlite":
        reflected = {index["name"] for index in inspector.get_indexes(table.name)}
        for index in connection.execute(sa.text(f'PRAGMA index_list("{table.name}")')).mappings():
            if index["unique"] and index["origin"] != "pk":
                _refuse(table.name, "unique index")
            if index["name"] in {i.name for i in table.indexes} and index["name"] not in reflected:
                _refuse(table.name, "unrecognized index")
    if dialect.name == "cockroachdb":
        ddl = connection.execute(sa.text(f"SHOW CREATE TABLE {table.name}")).one()[1]
        if re.search(r"\bttl_\w+\s*=", ddl, re.IGNORECASE):
            _refuse(table.name, "expiry")
        hidden = inspector.get_columns(table.name, include_hidden=True)
        if [column["name"] for column in hidden] != list(table.c.keys()):
            _refuse(table.name, "hidden columns")


def _verify_index(
    connection: Connection, table: sa.Table, found: ReflectedIndex, expected: sa.Index
) -> None:
    if (
        found["column_names"] != [column.name for column in expected.columns]
        or bool(found["unique"]) != expected.unique
        or (connection.dialect.name != "cockroachdb" and found.get("column_sorting"))
        or found.get("include_columns")
        or found.get("expressions")
    ):
        _refuse(table.name, f"index {expected.name}")
    if connection.dialect.name == "sqlite":
        entries = connection.execute(sa.text(f'PRAGMA index_xinfo("{expected.name}")')).mappings()
        keys = [(row["name"], row["desc"], row["coll"]) for row in entries if row["key"]]
        if keys != [(column.name, 0, "BINARY") for column in expected.columns]:
            _refuse(table.name, f"index {expected.name} ordering")
    if connection.dialect.name == "cockroachdb":
        keys = [
            row
            for row in connection.execute(sa.text(f"SHOW INDEXES FROM {table.name}")).mappings()
            if row["index_name"] == expected.name
        ]
        if [
            (
                row["column_name"],
                row["definition"],
                row["direction"],
                row["non_unique"],
                row["storing"],
                row["implicit"],
                row["visible"],
                row["visibility"],
            )
            for row in keys
        ] != [
            (column.name, column.name, "ASC", True, False, False, True, 1.0)
            for column in expected.columns
        ]:
            _refuse(table.name, f"index {expected.name} keys")
        if found.get("column_sorting") != {
            column.name: ("nulls_first",) for column in expected.columns
        }:
            _refuse(table.name, f"index {expected.name} ordering")
    for option, value in found.get("dialect_options", {}).items():
        if option in {"postgresql_include", "cockroachdb_include"} and value == []:
            continue
        if option in {"postgresql_using", "cockroachdb_using"} and value == "btree":
            continue
        if connection.dialect.name == "cockroachdb":
            if option == "postgresql_ops" and value == {c.name: None for c in expected.columns}:
                continue
            if option == "postgresql_using" and value == "prefix":
                continue
        _refuse(table.name, f"index {expected.name} options")


def preflight(connection: Connection) -> None:
    """Reject all incompatible existing objects before the first schema write."""
    if connection.dialect.name == "mysql":
        raise SchemaDriftError("Unverified MySQL session work catalog.")
    metadata = schema()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    views = set(inspector.get_view_names())
    if connection.dialect.name in {"postgresql", "cockroachdb"}:
        views.update(inspector.get_materialized_view_names())
    names = set(verified_objects())
    if views & names:
        _refuse("session_work", "view collision")
    if connection.dialect.name in {"postgresql", "cockroachdb"}:
        relations = connection.execute(
            sa.text(
                "SELECT c.relname, c.relkind, owner.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid "
                "LEFT JOIN pg_catalog.pg_class owner ON owner.oid = i.indrelid "
                "WHERE n.nspname = current_schema() AND c.relname IN :names"
            ).bindparams(sa.bindparam("names", expanding=True)),
            {"names": sorted(names)},
        )
        expected_relations: dict[str, tuple[str, str | None]] = {
            table.name: ("r", None) for table in metadata.tables.values()
        }
        expected_relations.update(
            {
                str(index.name): ("i", table.name)
                for table in metadata.tables.values()
                for index in table.indexes
            }
        )
        for name, kind, owner in relations:
            if expected_relations.get(name) != (kind, owner):
                _refuse("session_work", "relation collision")
    if connection.dialect.name == "sqlite":
        for name, table_name in connection.execute(
            sa.text("SELECT name, tbl_name FROM sqlite_schema WHERE type = 'index'")
        ):
            if name in names and not (
                table_name in metadata.tables
                and name in {i.name for i in metadata.tables[table_name].indexes}
            ):
                _refuse("session_work", "index collision")
    for table_name in tables:
        if table_name in names and table_name not in metadata.tables:
            _refuse("session_work", "table collision")
        for index in inspector.get_indexes(table_name):
            if index["name"] in names and not (
                table_name in metadata.tables
                and index["name"] in {i.name for i in metadata.tables[table_name].indexes}
            ):
                _refuse("session_work", "index collision")
    for table in metadata.tables.values():
        if table.name not in tables:
            continue
        _verify_table(connection, inspector, table)
        expected_indexes = {index.name: index for index in table.indexes}
        for found in inspector.get_indexes(table.name):
            if found["unique"]:
                _refuse(table.name, "unique index")
            if found["name"] in expected_indexes:
                _verify_index(connection, table, found, expected_indexes[found["name"]])


def verify(connection: Connection) -> None:
    preflight(connection)
    inspector = sa.inspect(connection)
    for table in schema().tables.values():
        if not inspector.has_table(table.name):
            _refuse(table.name, "missing table")
        found = {index["name"] for index in inspector.get_indexes(table.name)}
        if any(index.name not in found for index in table.indexes):
            _refuse(table.name, "missing index")


def apply(connection: Connection) -> None:
    preflight(connection)
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names())
    for table in schema().tables.values():
        if table.name not in existing:
            table.create(connection)
        else:
            found = {index["name"] for index in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in found:
                    index.create(connection)
    verify(connection)

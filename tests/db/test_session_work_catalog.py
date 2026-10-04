"""Recorded native catalogs exercise verification without a database server."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from omnigent.db.migrations.schema import mm1a2b3c4d5e as revision


@pytest.fixture
def mysql_catalog(monkeypatch):
    recorded = json.loads(
        (Path(__file__).parent / "fixtures/session_work_mysql_8_0_46.json").read_text()
    )
    tables = copy.deepcopy(recorded["tables"])
    types = {
        "BIGINT": mysql.BIGINT(),
        "SMALLINT": mysql.SMALLINT(),
        "BINARY(16)": sa.BINARY(16),
        "VARCHAR(64)": mysql.VARCHAR(64),
    }
    for table in tables.values():
        for column in table["columns"]:
            column["type"] = types[column["type_sql"]]
    inspector = Mock()
    inspector.get_table_names.return_value = list(tables)
    inspector.get_view_names.return_value = []
    inspector.has_table.side_effect = lambda table: table in tables
    for method, key in {
        "get_columns": "columns",
        "get_pk_constraint": "pk",
        "get_check_constraints": "checks",
        "get_indexes": "indexes",
        "get_unique_constraints": "unique_constraints",
        "get_foreign_keys": "foreign_keys",
    }.items():
        getattr(inspector, method).side_effect = lambda name, key=key: tables[name][key]
    monkeypatch.setattr(revision.sa, "inspect", lambda connection: inspector)

    def execute(statement, parameters=None):
        sql = str(statement)
        if sql.startswith("SHOW INDEX FROM "):
            result = tables[sql.removeprefix("SHOW INDEX FROM ")]["show_index"]
        elif "information_schema.columns" in sql:
            result = tables[parameters["table"]]["information_schema_columns"]
        elif "information_schema.table_constraints" in sql:
            result = tables[parameters["table"]]["information_schema_table_constraints"]
        else:
            raise AssertionError(f"Unexpected catalog query: {sql}")
        return SimpleNamespace(mappings=lambda: result)

    return SimpleNamespace(dialect=mysql.dialect(), execute=execute), tables


@pytest.mark.parametrize(
    ("catalog_name", "field", "value", "property_name"),
    [
        ("information_schema_columns", "COLUMN_TYPE", "bigint unsigned", "column workspace_id"),
        ("information_schema_columns", "IS_NULLABLE", "YES", "column workspace_id"),
        ("information_schema_columns", "COLUMN_DEFAULT", "0", "column workspace_id"),
        ("information_schema_columns", "EXTRA", "auto_increment", "column workspace_id"),
        ("show_index", "Collation", "D", "index ix_session_work_unfinished"),
        ("show_index", "Non_unique", 0, "index"),
        ("show_index", "Seq_in_index", 2, "index ix_session_work_unfinished"),
        ("show_index", "Sub_part", 8, "index ix_session_work_unfinished"),
        ("show_index", "Index_type", "HASH", "index ix_session_work_unfinished"),
        ("show_index", "Visible", "NO", "index ix_session_work_unfinished"),
        ("show_index", "Expression", "(state + 1)", "index ix_session_work_unfinished"),
    ],
)
def test_mysql_catalog_rejects_native_drift(
    mysql_catalog, catalog_name, field, value, property_name
):
    connection, tables = mysql_catalog
    revision.verify(connection)
    rows = tables["session_work"][catalog_name]
    if catalog_name == "show_index":
        rows = [row for row in rows if row["Key_name"] == "ix_session_work_unfinished"]
    rows[0][field] = value
    with pytest.raises(revision.SchemaDriftError, match=property_name):
        revision.verify(connection)


def test_mysql_catalog_rejects_changed_check(mysql_catalog):
    connection, tables = mysql_catalog
    revision.verify(connection)
    tables["session_work"]["checks"][0]["sqltext"] = (
        "(((`state` in (1,5,6)) and (`claim_id` is null)) or "
        "((`state` in (2,3,4)) and (`claim_id` is not null)))"
    )
    with pytest.raises(revision.SchemaDriftError, match="check constraints"):
        revision.verify(connection)

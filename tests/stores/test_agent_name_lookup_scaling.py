"""Bound the work of fetching one page's agent names."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlAgent, workspace_scope
from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


@pytest.mark.parametrize("schema", ["bootstrap", "upgrade"])
def test_get_names_work_is_bounded_by_requested_ids(tmp_path: Path, schema: str) -> None:
    uri = f"sqlite:///{tmp_path / 'agents.db'}"
    if schema == "upgrade":
        engine = sa.create_engine(uri)
        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "ll1a2b3c4d5e")
        engine.dispose()
    store = SqlAlchemyAgentStore(uri)
    engine = get_or_create_engine(uri)
    ids = [f"{i:032x}" for i in range(1, 21)]
    expected = {agent_id: f"agent-{i}" for i, agent_id in enumerate(ids, 1)}
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM agents" in statement:
            queries.append((statement, parameters))

    work = []
    try:
        for corpus_size in (1_017, 16_017):
            with engine.begin() as conn:
                conn.execute(sa.delete(SqlAgent))
                rows = [
                    {
                        "workspace_id": 0,
                        "id": f"{i:032x}",
                        "created_at": i,
                        "name": f"agent-{i}",
                        "bundle_location": "test/bundle",
                        "version": 1,
                        "kind": 2,
                    }
                    for i in range(1, corpus_size - 1)
                ]
                rows.extend(
                    dict(rows[0], workspace_id=1, id=agent_id, name="other-workspace")
                    for agent_id in (ids[0], "f" * 32)
                )
                conn.execute(sa.insert(SqlAgent), rows)
                assert conn.scalar(sa.select(sa.func.count()).select_from(SqlAgent)) == corpus_size
            with workspace_scope(0):
                sa.event.listen(engine, "before_cursor_execute", capture)
                try:
                    assert store.get_names(ids) == expected
                finally:
                    sa.event.remove(engine, "before_cursor_execute", capture)
                assert store.get_names([ids[0], "e" * 32, "f" * 32]) == {ids[0]: "agent-1"}
            assert len(queries) == 1
            statement, parameters = queries.pop()
            steps = 0

            def tick():
                nonlocal steps
                steps += 1
                return 0

            with engine.connect() as conn:
                db = conn.connection.driver_connection
                assert isinstance(db, sqlite3.Connection)
                plan = db.execute("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
                db.set_progress_handler(tick, 1)
                try:
                    result = db.execute(statement, parameters).fetchall()
                finally:
                    db.set_progress_handler(None, 0)
                assert {bytes(row[0]).hex(): row[1] for row in result} == expected
            receipt = {"schema": schema, "agents": corpus_size, "vm_steps": steps, "plan": plan}
            print(json.dumps(receipt))
            work.append(receipt)
        assert max(row["vm_steps"] for row in work) < 1_000, work
        assert work[1]["vm_steps"] <= work[0]["vm_steps"] + 100, work
    finally:
        engine.dispose()

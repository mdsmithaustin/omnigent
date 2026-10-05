"""The agent-name covering index is additive and reversible."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlAgent
from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_agent_name_index_upgrade_downgrade_preserves_rows(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'upgrade.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "ll1a2b3c4d5e")
            conn.execute(
                sa.insert(SqlAgent),
                {
                    "workspace_id": 0,
                    "id": "1" * 32,
                    "created_at": 1,
                    "name": "retained",
                    "bundle_location": "test/bundle",
                    "version": 1,
                    "kind": 2,
                },
            )
            before = conn.execute(sa.select(SqlAgent)).all()
            command.upgrade(config, "mm1a2b3c4d5e")
            upgraded = {i["name"]: i for i in sa.inspect(conn).get_indexes("agents")}
            assert upgraded["ix_agents_id_name"]["column_names"] == ["workspace_id", "id", "name"]
            assert conn.execute(sa.select(SqlAgent)).all() == before
            command.downgrade(config, "ll1a2b3c4d5e")
            assert "ix_agents_id_name" not in {
                i["name"] for i in sa.inspect(conn).get_indexes("agents")
            }
            assert conn.execute(sa.select(SqlAgent)).all() == before
            command.upgrade(config, "mm1a2b3c4d5e")
            assert conn.execute(sa.select(SqlAgent)).all() == before
        fresh = get_or_create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
        try:
            bootstrap = {i["name"]: i for i in sa.inspect(fresh).get_indexes("agents")}
            assert bootstrap["ix_agents_id_name"] == upgraded["ix_agents_id_name"]
        finally:
            fresh.dispose()
    finally:
        engine.dispose()

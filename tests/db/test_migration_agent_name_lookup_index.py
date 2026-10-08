"""The fork's agent-name index sits after upstream's mm1a2b3c4d5e and repairs either mm1 state."""

import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from omnigent.db.db_models import SqlAgent
from omnigent.db.utils import (
    _build_alembic_config,
    _get_head_db_revision,
    get_or_create_engine,
)

_NAME_INDEX = ["workspace_id", "id", "name"]
_OWNER_INDEX = ["workspace_id", "kind", "created_by", "created_at", "id"]


def _agent_indexes(conn: sa.Connection) -> dict[str, list[str]]:
    # CockroachDB reflects index changes only after commit.
    conn.commit()
    return {i["name"]: i["column_names"] for i in sa.inspect(conn).get_indexes("agents")}


def _revision(conn: sa.Connection) -> str:
    return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


@pytest.fixture
def migration(tmp_path: Path, _worker_db_uri: str) -> Iterator[tuple[sa.Connection, Config]]:
    uri = _worker_db_uri or f"sqlite:///{tmp_path / 'upgrade.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            if _worker_db_uri:
                command.downgrade(config, "ll1a2b3c4d5e")
            yield conn, config
    finally:
        engine.dispose()


def test_migration_scripts_resolve_one_head_without_duplicate_revisions() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _get_head_db_revision("sqlite://")
    duplicates = [str(w.message) for w in caught if "present more than once" in str(w.message)]
    assert duplicates == []


def test_fresh_upgrade_and_bootstrap_create_both_indexes(
    migration: tuple[sa.Connection, Config], tmp_path: Path
) -> None:
    conn, config = migration
    command.upgrade(config, "fork1a2b3c4d")
    upgraded = _agent_indexes(conn)
    assert _revision(conn) == "fork1a2b3c4d"
    assert upgraded["ix_agents_id_name"] == _NAME_INDEX
    assert upgraded["ix_agents_kind_owner_created"] == _OWNER_INDEX
    fresh = get_or_create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        with fresh.connect() as fresh_conn:
            bootstrap = _agent_indexes(fresh_conn)
        assert bootstrap["ix_agents_id_name"] == _NAME_INDEX
        assert bootstrap["ix_agents_kind_owner_created"] == _OWNER_INDEX
    finally:
        fresh.dispose()


def test_old_fork_mm1_state_upgrades_to_both_indexes(
    migration: tuple[sa.Connection, Config],
) -> None:
    conn, config = migration
    command.upgrade(config, "ll1a2b3c4d5e")
    conn.execute(sa.text("CREATE INDEX ix_agents_id_name ON agents (workspace_id, id, name)"))
    conn.commit()
    command.stamp(config, "mm1a2b3c4d5e")
    assert "ix_agents_kind_owner_created" not in _agent_indexes(conn)
    command.upgrade(config, "fork1a2b3c4d")
    upgraded = _agent_indexes(conn)
    assert _revision(conn) == "fork1a2b3c4d"
    assert upgraded["ix_agents_id_name"] == _NAME_INDEX
    assert upgraded["ix_agents_kind_owner_created"] == _OWNER_INDEX


@pytest.mark.parametrize("existing_name_index", [False, True])
def test_upstream_mm1_state_upgrades_to_both_indexes(
    migration: tuple[sa.Connection, Config],
    existing_name_index: bool,
) -> None:
    conn, config = migration
    command.upgrade(config, "mm1a2b3c4d5e")
    assert "ix_agents_id_name" not in _agent_indexes(conn)
    if existing_name_index:
        conn.execute(sa.text("CREATE INDEX ix_agents_id_name ON agents (workspace_id, id, name)"))
        conn.commit()
    command.upgrade(config, "fork1a2b3c4d")
    upgraded = _agent_indexes(conn)
    assert _revision(conn) == "fork1a2b3c4d"
    assert upgraded["ix_agents_id_name"] == _NAME_INDEX
    assert upgraded["ix_agents_kind_owner_created"] == _OWNER_INDEX


def test_downgrade_drops_only_agent_name_index_and_preserves_rows(
    migration: tuple[sa.Connection, Config],
) -> None:
    conn, config = migration
    command.upgrade(config, "mm1a2b3c4d5e")
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
    conn.commit()
    command.upgrade(config, "fork1a2b3c4d")
    command.downgrade(config, "mm1a2b3c4d5e")
    downgraded = _agent_indexes(conn)
    assert _revision(conn) == "mm1a2b3c4d5e"
    assert "ix_agents_id_name" not in downgraded
    assert downgraded["ix_agents_kind_owner_created"] == _OWNER_INDEX
    assert conn.execute(sa.select(SqlAgent.name)).scalars().all() == ["retained"]
    command.upgrade(config, "fork1a2b3c4d")
    assert _agent_indexes(conn)["ix_agents_id_name"] == _NAME_INDEX
    assert conn.execute(sa.select(SqlAgent.name)).scalars().all() == ["retained"]


def test_downgrade_without_agent_name_index_preserves_upstream_index(
    migration: tuple[sa.Connection, Config],
) -> None:
    conn, config = migration
    command.upgrade(config, "mm1a2b3c4d5e")
    command.stamp(config, "fork1a2b3c4d")
    command.downgrade(config, "mm1a2b3c4d5e")
    assert _revision(conn) == "mm1a2b3c4d5e"
    assert _agent_indexes(conn)["ix_agents_kind_owner_created"] == _OWNER_INDEX
    assert "ix_agents_id_name" not in _agent_indexes(conn)

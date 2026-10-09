"""Cover workspace-scoped agent-name lookups by ID.

This fork-only revision follows the fork's za3b4c5d6e7f, which follows upstream's
mm1a2b3c4d5e. It repairs databases stamped by either the fork's or upstream's
mm1a2b3c4d5e, so both agents indexes exist at head.

The pre-v0.17.0 development line defined this index as fork1a2b3c4d directly after
mm1a2b3c4d5e. That revision is deliberately not reused, because it skipped
za3b4c5d6e7f. Startup refuses a database stamped fork1a2b3c4d. To recover it, run
`alembic stamp --purge mm1a2b3c4d5e` with the newer code, then upgrade to head.

When an upstream sync brings a migration revising mm1a2b3c4d5e, add an Alembic
merge revision of fork2a3b4c5d and the new upstream head in that sync.
test_migration_scripts_resolve_one_head_without_duplicate_revisions fails until
the merge revision exists.

Coordinate this migration with the application deployment. The added index
preserves compatibility with older SQL readers and writers, but older application
processes cannot start against revision fork2a3b4c5d. Their startup revision check
recognizes only revisions through za3b4c5d6e7f. Deploy code that knows this revision
before restarting application processes against the upgraded database.

Stop database readers and writers for schema changes and keep them stopped through
rollback. Before restarting the older application, use the newer migration code
to run an Alembic downgrade from fork2a3b4c5d to za3b4c5d6e7f. This removes the
agent-name index and updates the recorded Alembic revision while preserving agent
data. Dropping the index alone does not permit older application startup. Verify
that the recorded revision is za3b4c5d6e7f before restarting the older application.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fork2a3b4c5d"
down_revision: str | None = "za3b4c5d6e7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "agents"
_INDEXES = (
    ("ix_agents_id_name", ["workspace_id", "id", "name"]),
    ("ix_agents_kind_owner_created", ["workspace_id", "kind", "created_by", "created_at", "id"]),
)


def _reflected_indexes() -> set[str | None] | None:
    """Index names on MySQL, which lacks IF [NOT] EXISTS but commits each DDL; else None.

    Elsewhere reflection can miss an index built earlier in the same transaction
    (CockroachDB 23.2), so callers use IF [NOT] EXISTS instead.
    """
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return None
    return {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    existing = _reflected_indexes()
    for name, columns in _INDEXES:
        if existing is None:
            op.create_index(name, _TABLE, columns, if_not_exists=True)
        elif name not in existing:
            op.create_index(name, _TABLE, columns)


def downgrade() -> None:
    existing = _reflected_indexes()
    if existing is None:
        op.drop_index("ix_agents_id_name", table_name=_TABLE, if_exists=True)
    elif "ix_agents_id_name" in existing:
        op.drop_index("ix_agents_id_name", table_name=_TABLE)

"""Cover workspace-scoped agent-name lookups by ID.

This fork-only revision sits after upstream's mm1a2b3c4d5e and repairs databases
stamped by either the fork's or upstream's mm1a2b3c4d5e, so both agents indexes
exist at head.

When an upstream sync brings a migration revising mm1a2b3c4d5e, add an Alembic
merge revision of fork1a2b3c4d and the new upstream head in that sync.
test_migration_scripts_resolve_one_head_without_duplicate_revisions fails until
the merge revision exists.

Coordinate this migration with the application deployment. The added index
preserves compatibility with older SQL readers and writers, but older application
processes cannot start against revision fork1a2b3c4d. Their startup revision check
recognizes only revisions through mm1a2b3c4d5e. Deploy code that knows this revision
before restarting application processes against the upgraded database.

Stop database readers and writers for schema changes and keep them stopped through
rollback. Before restarting the older application, use the newer migration code
to run an Alembic downgrade from fork1a2b3c4d to mm1a2b3c4d5e. This removes the
agent-name index and updates the recorded Alembic revision while preserving agent
data. Dropping the index alone does not permit older application startup. Verify
that the recorded revision is mm1a2b3c4d5e before restarting the older application.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fork1a2b3c4d"
down_revision: str | None = "mm1a2b3c4d5e"
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

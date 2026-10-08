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


def upgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("agents")}
    if "ix_agents_id_name" not in indexes:
        op.create_index("ix_agents_id_name", "agents", ["workspace_id", "id", "name"])
    if "ix_agents_kind_owner_created" not in indexes:
        op.create_index(
            "ix_agents_kind_owner_created",
            "agents",
            ["workspace_id", "kind", "created_by", "created_at", "id"],
        )


def downgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("agents")}
    if "ix_agents_id_name" in indexes:
        op.drop_index("ix_agents_id_name", table_name="agents")

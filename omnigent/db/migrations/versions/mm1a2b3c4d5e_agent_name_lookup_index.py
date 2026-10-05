"""Cover workspace-scoped agent-name lookups by ID.

Coordinate this migration with the application deployment. The added index
preserves compatibility with older SQL readers and writers, but older application
processes cannot start against revision mm1a2b3c4d5e. Their startup revision check
recognizes only revisions through ll1a2b3c4d5e. Deploy code that knows this revision
before restarting application processes against the upgraded database.

Stop database readers and writers for schema changes and keep them stopped through
rollback. Before restarting the older application, use the newer migration code
to run an Alembic downgrade from mm1a2b3c4d5e to ll1a2b3c4d5e. This removes the
index and updates the recorded Alembic revision while preserving agent data.
Dropping the index alone does not permit older application startup. Verify that
the recorded revision is ll1a2b3c4d5e before restarting the older application.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_agents_id_name", "agents", ["workspace_id", "id", "name"])


def downgrade() -> None:
    op.drop_index("ix_agents_id_name", table_name="agents")

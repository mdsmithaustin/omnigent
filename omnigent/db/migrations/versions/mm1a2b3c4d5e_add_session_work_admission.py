"""Persist AP session work admission and exact dispatch ownership."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_work_admission",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("session_id", Uuid16(), primary_key=True),
        sa.Column("agent_id", Uuid16(), nullable=True),
        sa.Column("runner_id", sa.String(64), nullable=False),
        sa.Column("incarnation", Uuid16(), nullable=False),
        sa.Column("reservation_id", Uuid16(), nullable=True),
    )
    op.create_table(
        "session_work",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("session_id", Uuid16(), primary_key=True),
        sa.Column("operation_id", Uuid16(), primary_key=True),
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
    op.create_index(
        "ix_session_work_unfinished",
        "session_work",
        ["workspace_id", "session_id", "state", "operation_id"],
    )


def downgrade() -> None:
    op.drop_table("session_work")
    op.drop_table("session_work_admission")

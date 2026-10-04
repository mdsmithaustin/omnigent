"""Persist AP session work admission and exact dispatch ownership."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.migrations.schema import mm1a2b3c4d5e as definition

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_context().as_sql:
        for table in definition.schema().tables.values():
            op.execute(sa.schema.CreateTable(table))
            for index in table.indexes:
                op.execute(sa.schema.CreateIndex(index))
    else:
        connection = op.get_bind()
        definition.apply(connection)
        if connection.dialect.name == "cockroachdb":
            # Indexes on existing CRDB tables become visible at commit.
            connection.commit()
            connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        definition.verify(connection)


def downgrade() -> None:
    op.drop_table("session_work")
    op.drop_table("session_work_admission")

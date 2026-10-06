"""Add generation-bound native source authority to the conversation database."""

from alembic import op

from omnigent.db.db_models import SqlNativeSource

revision = "za3b4c5d6e7f"
down_revision = "mm1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    SqlNativeSource.metadata.tables["conversation_native_sources"].create(
        op.get_bind(), checkfirst=True
    )


def downgrade() -> None:
    SqlNativeSource.metadata.tables["conversation_native_sources"].drop(
        op.get_bind(), checkfirst=True
    )

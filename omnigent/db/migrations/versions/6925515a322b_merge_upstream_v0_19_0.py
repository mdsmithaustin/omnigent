"""merge upstream v0.19.0

Revision ID: 6925515a322b
Revises: fork2a3b4c5d, nf12ab34cd56
Create Date: 2026-10-20 18:44:20.940541
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6925515a322b"
down_revision: str | Sequence[str] | None = ("fork2a3b4c5d", "nf12ab34cd56")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

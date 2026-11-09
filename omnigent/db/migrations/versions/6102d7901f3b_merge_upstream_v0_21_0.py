"""merge upstream v0.21.0

Revision ID: 6102d7901f3b
Revises: 6925515a322b, d0a1f879a01d
Create Date: 2026-11-09 10:28:23.709419
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6102d7901f3b"
down_revision: str | Sequence[str] | None = ("6925515a322b", "d0a1f879a01d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

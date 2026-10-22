"""ai servers: the expressive voice's connection

Revision ID: f2a7c3d9e1b4
Revises: e5c1a9d47b20
Create Date: 2026-10-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a7c3d9e1b4"
down_revision: str | Sequence[str] | None = "e5c1a9d47b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "ai_servers", sa.Column("voice_connection_id", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("ai_servers", "voice_connection_id")

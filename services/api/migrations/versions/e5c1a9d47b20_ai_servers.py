"""ai servers (self-hosted model hosts reached through the AI gateway, ADR 0007)

Revision ID: e5c1a9d47b20
Revises: b3e8f1a4c6d2
Create Date: 2026-10-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e5c1a9d47b20"
down_revision: str | Sequence[str] | None = "b3e8f1a4c6d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ai_servers",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.Column("live_stt_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="unchecked", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "last_health",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("llm_connection_id", sa.String(length=64), nullable=True),
        sa.Column("whisper_connection_id", sa.String(length=64), nullable=True),
        sa.Column("tts_connection_id", sa.String(length=64), nullable=True),
        sa.Column("live_stt_connection_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_ai_servers_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ai_servers_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_servers")),
        sa.UniqueConstraint(
            "organization_id", "base_url", name=op.f("uq_ai_servers_organization_id")
        ),
    )
    op.create_index(
        op.f("ix_ai_servers_organization_id"), "ai_servers", ["organization_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_ai_servers_organization_id"), table_name="ai_servers")
    op.drop_table("ai_servers")

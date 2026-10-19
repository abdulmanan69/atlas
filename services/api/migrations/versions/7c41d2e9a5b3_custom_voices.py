"""custom voices (Pocket TTS voice cloning: which organization owns each cloned voice)

Revision ID: 7c41d2e9a5b3
Revises: 5ab2a3d0e22c
Create Date: 2026-10-19 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7c41d2e9a5b3'
down_revision: Union[str, Sequence[str], None] = '5ab2a3d0e22c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'custom_voices',
        sa.Column('connection_id', sa.String(length=64), nullable=False),
        sa.Column('voice_id', sa.String(length=80), nullable=False),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_custom_voices_created_by_users'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_custom_voices_organization_id_organizations'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_custom_voices')),
        sa.UniqueConstraint('voice_id', name=op.f('uq_custom_voices_voice_id')),
    )
    op.create_index('ix_custom_voices_org_connection', 'custom_voices', ['organization_id', 'connection_id'], unique=False)
    op.create_index(op.f('ix_custom_voices_organization_id'), 'custom_voices', ['organization_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_custom_voices_organization_id'), table_name='custom_voices')
    op.drop_index('ix_custom_voices_org_connection', table_name='custom_voices')
    op.drop_table('custom_voices')

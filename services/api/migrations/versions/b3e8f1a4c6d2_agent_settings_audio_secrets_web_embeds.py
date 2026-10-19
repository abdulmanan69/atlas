"""agent settings: sound files, setting secrets and website widgets

Revision ID: b3e8f1a4c6d2
Revises: 7c41d2e9a5b3
Create Date: 2026-10-19 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3e8f1a4c6d2'
down_revision: Union[str, Sequence[str], None] = '7c41d2e9a5b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('agent_audio',
    sa.Column('agent_id', sa.UUID(), nullable=False),
    sa.Column('purpose', sa.String(length=16), nullable=False),
    sa.Column('filename', sa.String(length=200), nullable=False),
    sa.Column('content_type', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('runtime_storage_key', sa.String(length=400), nullable=True),
    sa.Column('runtime_storage_backend', sa.String(length=32), nullable=True),
    sa.Column('runtime_recording_id', sa.String(length=64), nullable=True),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_agent_audio_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_agent_audio_created_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_agent_audio_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_audio'))
    )
    op.create_index(op.f('ix_agent_audio_agent_id'), 'agent_audio', ['agent_id'], unique=False)
    op.create_index(op.f('ix_agent_audio_organization_id'), 'agent_audio', ['organization_id'], unique=False)

    op.create_table('agent_secrets',
    sa.Column('agent_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('value_encrypted', sa.Text(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_agent_secrets_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_agent_secrets_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_secrets')),
    sa.UniqueConstraint('agent_id', 'kind', name=op.f('uq_agent_secrets_agent_id'))
    )
    op.create_index(op.f('ix_agent_secrets_organization_id'), 'agent_secrets', ['organization_id'], unique=False)

    op.create_table('agent_web_embeds',
    sa.Column('agent_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('allowed_domains', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('sessions_started', sa.Integer(), server_default='0', nullable=False),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_agent_web_embeds_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_agent_web_embeds_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_web_embeds')),
    sa.UniqueConstraint('agent_id', name=op.f('uq_agent_web_embeds_agent_id')),
    sa.UniqueConstraint('key', name=op.f('uq_agent_web_embeds_key'))
    )
    op.create_index(op.f('ix_agent_web_embeds_organization_id'), 'agent_web_embeds', ['organization_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_agent_web_embeds_organization_id'), table_name='agent_web_embeds')
    op.drop_table('agent_web_embeds')
    op.drop_index(op.f('ix_agent_secrets_organization_id'), table_name='agent_secrets')
    op.drop_table('agent_secrets')
    op.drop_index(op.f('ix_agent_audio_organization_id'), table_name='agent_audio')
    op.drop_index(op.f('ix_agent_audio_agent_id'), table_name='agent_audio')
    op.drop_table('agent_audio')

"""pan_backup tables and status enum extension

Revision ID: 029_pan_backup
Revises: 028_user_voice_source_metadata
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, BYTEA

revision = '029_pan_backup'
down_revision = '028_user_voice_source_metadata'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure pgcrypto for gen_random_uuid() — idempotent, safe to call
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto";')

    # pan_credentials: one row per (user, provider) tuple
    op.create_table(
        'pan_credentials',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('access_token_encrypted', BYTEA, nullable=False),
        sa.Column('refresh_token_encrypted', BYTEA, nullable=False),
        sa.Column('access_token_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scope', sa.String(255), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='active'),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('last_refreshed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('user_id', 'provider', name='uq_pan_credentials_user_provider'),
    )

    # backup_records: one row per backup attempt (failures kept for audit)
    op.create_table(
        'backup_records',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('job_id', sa.String(64), nullable=False),  # not FK — allow surviving jobs row deletion
        sa.Column('job_edit_generation', sa.Integer, nullable=False, server_default='0'),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('remote_path', sa.Text, nullable=False),
        sa.Column('size_bytes', sa.BigInteger, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('md5', sa.String(32), nullable=False),
        sa.Column('manifest_json', JSONB, nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text, nullable=True),
    )
    # Partial unique index: at most one in-flight backup per (user, job, provider, generation)
    op.create_index(
        'uniq_backup_in_flight',
        'backup_records',
        ['user_id', 'job_id', 'provider', 'job_edit_generation'],
        unique=True,
        postgresql_where=sa.text("status IN ('uploading', 'restoring')"),
    )
    op.create_index('idx_backup_user_status', 'backup_records', ['user_id', 'status'])
    op.create_index('idx_backup_user_job_gen', 'backup_records', ['user_id', 'job_id', 'job_edit_generation'])
    op.create_index(
        'idx_backup_heartbeat',
        'backup_records',
        ['heartbeat_at'],
        postgresql_where=sa.text("status IN ('uploading', 'restoring')"),
    )

    # pan_oauth_states: short-lived (10min) CSRF protection for OAuth flow
    op.create_table(
        'pan_oauth_states',
        sa.Column('token', sa.String(64), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('pan_oauth_states')
    op.drop_index('idx_backup_heartbeat', table_name='backup_records')
    op.drop_index('idx_backup_user_job_gen', table_name='backup_records')
    op.drop_index('idx_backup_user_status', table_name='backup_records')
    op.drop_index('uniq_backup_in_flight', table_name='backup_records')
    op.drop_table('backup_records')
    op.drop_table('pan_credentials')

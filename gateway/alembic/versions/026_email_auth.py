"""Add email verification challenges and verified timestamp.

Revision ID: 026_email_auth
Revises: 025_add_r2_artifacts
Create Date: 2026-05-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "026_email_auth"
down_revision: Union[str, None] = "025_add_r2_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "email_verification_challenges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "purpose",
            sa.String(length=32),
            server_default="registration",
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_email_challenges_email",
        "email_verification_challenges",
        ["email"],
    )
    op.create_index(
        "idx_email_challenges_expires",
        "email_verification_challenges",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_email_challenges_expires",
        table_name="email_verification_challenges",
    )
    op.drop_index(
        "idx_email_challenges_email",
        table_name="email_verification_challenges",
    )
    op.drop_table("email_verification_challenges")
    op.drop_column("users", "email_verified_at")

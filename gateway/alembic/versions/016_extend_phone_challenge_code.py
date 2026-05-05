"""Extend phone verification challenge code length.

Revision ID: 016_extend_phone_challenge_code
Revises: 015_post_edit_fields
Create Date: 2026-05-05

``phone_verification_challenges.code`` stores both short SMS OTPs and the
post-OTP registration token issued to new phone users. The token is currently
``uuid.uuid4().hex`` (32 characters), so the original VARCHAR(16) column
truncated/crashed the new-user registration flow after SMS verification.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "016_extend_phone_challenge_code"
down_revision: Union[str, None] = "015_post_edit_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "phone_verification_challenges",
        "code",
        existing_type=sa.String(length=16),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "phone_verification_challenges",
        "code",
        existing_type=sa.String(length=128),
        type_=sa.String(length=16),
        existing_nullable=False,
    )

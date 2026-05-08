"""Add phone-first auth fields and the phone_verification_challenges table.

Revision ID: 007_phone_auth
Revises: 006_label_tasks
Create Date: 2026-04-05

Task 3: phone-only public registration main path + minimal trial bookkeeping.

- users.email / users.password_hash become nullable so phone-only accounts can
  exist without a synthetic placeholder email.
- users gains phone_number / phone_verified_at / trial_granted_at / trial_ends_at.
  trial_ends_at is left nullable and unpopulated — the numeric trial rule
  (days / source minutes) is still unfrozen in the gateway truth source.
- phone_verification_challenges stores single-use OTP codes with per-row TTL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "007_phone_auth"
down_revision: Union[str, None] = "006_label_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- users: relax email / password_hash, add phone + trial fields ---
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=True)
    op.alter_column(
        "users", "password_hash", existing_type=sa.String(length=255), nullable=True
    )

    op.add_column(
        "users",
        sa.Column("phone_number", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("trial_granted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_users_phone_number", "users", ["phone_number"]
    )

    # --- phone_verification_challenges ---
    op.create_table(
        "phone_verification_challenges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("phone_number", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "purpose",
            sa.String(length=32),
            nullable=False,
            server_default="login",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_phone_challenges_phone",
        "phone_verification_challenges",
        ["phone_number"],
    )
    op.create_index(
        "idx_phone_challenges_expires",
        "phone_verification_challenges",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_phone_challenges_expires", table_name="phone_verification_challenges"
    )
    op.drop_index(
        "idx_phone_challenges_phone", table_name="phone_verification_challenges"
    )
    op.drop_table("phone_verification_challenges")

    op.drop_constraint("uq_users_phone_number", "users", type_="unique")
    op.drop_column("users", "trial_ends_at")
    op.drop_column("users", "trial_granted_at")
    op.drop_column("users", "phone_verified_at")
    op.drop_column("users", "phone_number")

    # P2-22B / D-HIGH-4 (audit 2026-05-07): we DO NOT restore NOT NULL
    # on email / password_hash here.
    #
    # The pre-007 schema required both columns to be NOT NULL because at
    # that point email-only registration was the only path. Once 007
    # opened phone registration, every phone-only user has email = NULL
    # AND password_hash = NULL (until they bind an email). Production
    # already has phone-only users — at least 3 confirmed in the
    # 2026-05-08 trial-grant audit.
    #
    # Trying to re-add NOT NULL via ``op.alter_column(..., nullable=False)``
    # would fail at PostgreSQL with ``column "X" contains null values`` and
    # abort the rollback transaction. The pre-fix downgrade would simply
    # crash on any DB where 007 had been applied for long enough to
    # accumulate phone-only registrations — which is every realistic DR
    # scenario.
    #
    # Two options the audit allowed:
    #   (a) skip the NOT NULL re-add — accept schema asymmetry post-rollback
    #   (b) DELETE FROM users WHERE email IS NULL OR password_hash IS NULL
    #       — drops legitimate user data
    #
    # Option (a) wins: a DR rollback should NOT delete real user accounts,
    # period. The downside is post-rollback ``users.email`` /
    # ``users.password_hash`` stay nullable, diverging from the pre-007
    # column definitions. That's harmless — application code from the 006
    # generation handled NULL through the legacy login path's existence
    # check anyway, and any future migration that re-tightens these would
    # need the same guard.
    #
    # If your DR plan REQUIRES the original NOT NULL constraint, run this
    # by hand BEFORE the alembic downgrade:
    #
    #     DELETE FROM users WHERE email IS NULL OR password_hash IS NULL;
    #     -- ALTER TABLE users ALTER COLUMN email SET NOT NULL;
    #     -- ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;
    #
    # That's an explicit, reviewed loss-of-data action — NOT something
    # the migration should do silently.

"""Audit S-HIGH-4 (P1-10a-2): track wrong-code attempts on phone challenges.

Revision ID: 019_add_phone_challenge_attempts
Revises: 018_pricing_active_unique
Create Date: 2026-05-08

The pre-019 ``verify_code_endpoint`` / ``reset_password_endpoint``
flow was:

    challenge.consumed_at = now
    await db.commit()
    if challenge.code != code:
        raise HTTPException(400)

That made *any* wrong guess burn the challenge — a perfect single-
shot DoS primitive. An attacker who knows the victim's phone number
just spams ``/auth/phone/verify-code`` with random codes; the FIRST
wrong guess marks ``consumed_at``, and the legitimate user — who
holds the real OTP from their SMS — gets ``验证码已过期`` because
the WHERE clause now filters out the row they need. Combined with
the per-phone send-code rate limit (1/min, 5/hour), the victim is
locked out for an hour at attacker cost ~0.

The audit fix requires comparing the code FIRST, and only marking
``consumed_at`` when:
  (a) the code matched, or
  (b) wrong-attempt count has reached the configured limit
      (default 3), at which point the challenge IS retired so a
      naive online brute-force can't simply keep trying.

This migration adds the ``attempts`` counter the new logic needs.
``server_default='0'`` backfills existing rows; the model's
``default=0`` keeps newly-created challenges in sync.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "019_add_phone_challenge_attempts"
down_revision: Union[str, None] = "018_pricing_active_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "phone_verification_challenges",
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("phone_verification_challenges", "attempts")

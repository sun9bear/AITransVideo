"""PR-E user_voices migration — user_voices.compatible_target_languages.

Revision ID: 043_uservoice_compat_langs
Revises: 042_voice_compat_target_langs
Create Date: 2026-06-20

plan 2026-06-13-multilingual-mutual-translation-plan-v3.md Phase 5.

Adds the DUB *target* language compatibility list to user_voices (cloned voices),
mirroring voice_catalog.compatible_target_languages (alembic 042). A cloned voice is
matchable only for the targets it was cloned for; this lets language-aware matching
exclude e.g. a Chinese-cloned voice from an English dub.

Backfill: every existing cloned voice → ["zh-CN"] (the current en→zh pipeline). The
three clone write paths (manual / Express auto / Smart auto) stamp this going forward.
Pure add-column + data backfill; NULL is treated as legacy zh-CN-only, so behavior is
unchanged until the language-aware matching is enabled.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "043_uservoice_compat_langs"
down_revision: Union[str, None] = "042_voice_compat_target_langs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_voices",
        sa.Column(
            "compatible_target_languages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    # Existing cloned voices belong to the en→zh pipeline → zh-CN target.
    # Only fills rows still NULL (idempotent re-run safe).
    op.execute(
        """
        UPDATE user_voices
        SET compatible_target_languages = '["zh-CN"]'::jsonb
        WHERE compatible_target_languages IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("user_voices", "compatible_target_languages")

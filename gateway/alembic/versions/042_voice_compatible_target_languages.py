"""PR-E matchable migration — voice_catalog.compatible_target_languages.

Revision ID: 042_voice_compat_target_langs
Revises: 041_payment_order_reconcile
Create Date: 2026-06-20

plan 2026-06-13-multilingual-mutual-translation-plan-v3.md Phase 5 (B).

Adds the DUB *target* language compatibility list to voice_catalog (distinct from
``language``, the voice's own spoken language). Drives the kill-switched
language-aware catalog query (``voice_catalog_target_language_filter_enabled``) so a
zh dub never returns en voices once en voices are added — the "止血" ordered
migration: ① backfill here (matchable untouched) → ② flip the runtime query to
filter by target → ③ verify zh target returns 0 en rows → ④ only then set newly
added en voices matchable=true.

Backfill maps each existing voice by its ``language`` column: ``en*`` → ["en"],
everything else (zh, dialects, NULL) → ["zh-CN"] — conservative for the dominant
en→zh pipeline. Pure add-column + data backfill; the kill switch defaults OFF so
the runtime query stays legacy (byte-identical) until explicitly enabled.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "042_voice_compat_target_langs"
down_revision: Union[str, None] = "041_payment_order_reconcile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "voice_catalog",
        sa.Column(
            "compatible_target_languages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    # Backfill by the voice's own language. en-speaking voices dub into en;
    # everything else (Chinese voices, dialects, NULL language) → zh-CN, matching the
    # current en→zh pipeline. Only fills rows still NULL (idempotent re-run safe).
    op.execute(
        """
        UPDATE voice_catalog
        SET compatible_target_languages =
            CASE
                -- English voices across providers: VolcEngine ``language='en'`` /
                -- ``voice_id`` like ``en_*``; MiniMax localizes ``language='英语'`` with
                -- ``English_*`` voice ids (re-CodeX P1 — these were mis-backfilled to zh).
                WHEN lower(coalesce(language, '')) LIKE 'en%'
                     OR lower(coalesce(language, '')) = 'english'
                     OR coalesce(language, '') = '英语'
                     OR left(voice_id, 3) = 'en_'
                     OR left(voice_id, 7) = 'ICL_en_'
                     OR voice_id LIKE 'English%'
                THEN '["en"]'::jsonb
                ELSE '["zh-CN"]'::jsonb
            END
        WHERE compatible_target_languages IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("voice_catalog", "compatible_target_languages")

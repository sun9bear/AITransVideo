"""Audit P2-24 / D-HIGH-2: index sessions.expires_at.

Revision ID: 021_sessions_expires_at_index
Revises: 019_add_phone_challenge_attempts
Create Date: 2026-05-08

``auth.create_session`` runs an opportunistic purge on every call:

    DELETE FROM sessions WHERE expires_at <= NOW()

Pre-021 ``sessions.expires_at`` had NO index, so the purge does a
sequential scan on every login. Once the table accumulates 10k+ rows
(modest for a few weeks of normal traffic), the scan blocks the same
transaction's insert + creates row-lock contention. Login latency
balloons under load.

Fix: create a btree index on ``expires_at``. The ``DELETE WHERE
expires_at <= NOW()`` then becomes an index-range scan that touches
only the actually-expired rows.

``postgresql_concurrently=True`` so the upgrade does NOT take an
ACCESS EXCLUSIVE lock on the table — production logins continue
during the build. ``CREATE INDEX CONCURRENTLY`` cannot run inside a
transaction; we use ``op.get_context().autocommit_block()`` to opt
this migration out of alembic's default transaction wrapping.

``if_not_exists=True`` keeps the upgrade idempotent — re-running the
migration after a partial failure (where ``CREATE INDEX CONCURRENTLY``
left an INVALID index) won't error a second time. The downgrade is
symmetric with ``if_exists=True`` for the same reason.

Chain note: ``down_revision`` chains to 019 (the production head as
of 2026-05-08), NOT to a hypothetical 020. The user's in-progress AI
customer support migration ``020_add_support_and_notifications`` is
33 chars (overflows alembic_version VARCHAR(32)) and is still WIP at
the time this migration was authored. When 020 lands with a
shortened revision id, EITHER:
  (a) re-chain 021's ``down_revision`` to 020's final id and re-run
      tests, OR
  (b) keep 021 chained at 019 and treat the chain as branched from
      019 (alembic supports merge migrations).
Option (a) is cleaner; option (b) is the fallback if 020 lands long
after 021 deploys.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "021_sessions_expires_at_index"
down_revision: Union[str, None] = "019_add_phone_challenge_attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "idx_sessions_expires_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX_NAME,
            "sessions",
            ["expires_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="sessions",
            postgresql_concurrently=True,
            if_exists=True,
        )

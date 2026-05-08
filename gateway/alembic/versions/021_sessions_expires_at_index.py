"""Audit P2-24 / D-HIGH-2: index sessions.expires_at.

Revision ID: 021_sessions_expires_at_index
Revises: 020_support_notifications
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

INVALID-index cleanup (Codex review of 9d25be7): if a previous
``CREATE INDEX CONCURRENTLY`` was cancelled / failed mid-build,
PostgreSQL leaves the index in ``indisvalid=false`` state. The name
is taken but the planner won't use the index — it's effectively
dead. A naive ``if_not_exists=True`` retry would see the name,
skip creation, and let alembic mark 021 applied while the index
silently does nothing.

So this migration first probes ``pg_index`` for any INVALID row with
the target name and DROPs CONCURRENTLY before CREATE. ``if_not_exists``
on CREATE keeps a successful prior build idempotent (re-running on a
healthy DB is a no-op); the dynamic DROP-on-INVALID makes a
partial-failure rerun actually rebuild rather than rubber-stamp.

Downgrade is symmetric with ``if_exists=True`` so re-running it after
a manual cleanup doesn't error.

Chain: 019 → 020 (``020_support_notifications``, AI customer support
+ notifications) → 021 (this migration). The 020 revision id was
finalised at 25 chars (down from a 33-char draft that would have
overflowed alembic_version VARCHAR(32) and tripped the same trap
that bit migration 018 in production).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "021_sessions_expires_at_index"
down_revision: Union[str, None] = "020_support_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "idx_sessions_expires_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # First clean up any INVALID leftover from a previously
        # cancelled / failed CREATE INDEX CONCURRENTLY. Without this,
        # ``if_not_exists=True`` below would see the dead-name slot
        # and let alembic mark this revision applied while the index
        # is unusable. ``DROP INDEX CONCURRENTLY`` itself must run as
        # a top-level statement (cannot live inside a transaction or
        # function block), which the surrounding autocommit_block
        # already provides.
        conn = op.get_bind()
        invalid_row = conn.execute(
            sa.text(
                "SELECT 1 FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :idx_name "
                "AND i.indisvalid = false"
            ),
            {"idx_name": _INDEX_NAME},
        ).first()
        if invalid_row is not None:
            op.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"
            )

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

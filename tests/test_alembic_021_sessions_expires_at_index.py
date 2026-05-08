"""Audit P2-24 / D-HIGH-2 regression: sessions.expires_at index.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-HIGH-2 — auth.create_session opportunistic purge does
                   ``DELETE WHERE expires_at <= NOW()`` on every
                   login. Pre-021 expires_at had no index, so the
                   delete was a sequential scan. After 10k+ session
                   rows it becomes a per-login latency cliff and
                   creates row-lock contention with the same
                   transaction's INSERT.

Migration 021 adds the btree index. These guards keep three places
in lockstep — the alembic migration, the SQLAlchemy model
``__table_args__``, and the index name. Drift between any pair lets
the perf cliff sneak back in (or alembic autogenerate proposes
dropping the index).
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


_MIGRATION_PATH = (
    _REPO_ROOT / "gateway" / "alembic" / "versions"
    / "021_sessions_expires_at_index.py"
)


# =====================================================================
# §1 — Migration 021 file shape
# =====================================================================


def test_migration_021_file_exists():
    assert _MIGRATION_PATH.is_file(), (
        "P2-24 regression: alembic 021 migration is missing. The model "
        "declares the index but without the migration, alembic "
        "autogenerate would propose creating it on next revision."
    )


def test_migration_021_revision_id_within_varchar_32():
    """alembic_version.version_num is VARCHAR(32). Revision ids longer
    than 32 chars cause ``StringDataRightTruncationError`` on upgrade
    (the same trap that bit migration 018 in production)."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    # Extract: revision: str = "021_..."
    import re
    match = re.search(r'revision:\s*str\s*=\s*"([^"]+)"', src)
    assert match is not None, (
        "P2-24 regression: revision id literal not found in migration "
        "021. Schema guard cannot verify the length."
    )
    rev = match.group(1)
    assert rev == "021_sessions_expires_at_index", (
        f"P2-24 regression: migration 021 revision id drifted from "
        f"canonical name. Got {rev!r}."
    )
    assert len(rev) <= 32, (
        f"P2-24 regression: migration 021 revision id is {len(rev)} "
        f"chars, exceeds VARCHAR(32) on alembic_version. Will fail "
        f"on upgrade with StringDataRightTruncationError. Got {rev!r}."
    )


def test_migration_021_revision_chain():
    """021 chains directly to 019 — the production head as of
    2026-05-08. The user's WIP 020 (AI customer support) had a
    33-char revision id that overflows alembic_version VARCHAR(32),
    so 021 deliberately bypasses 020 to stay deployable today.
    When 020 lands with a shortened revision id, this test should
    be updated to expect the new chain.
    """
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "019_add_phone_challenge_attempts"' in src, (
        "P2-24 regression: migration 021 down_revision is not "
        "019_add_phone_challenge_attempts. Chain stitching may need "
        "re-doing if user's 020 landed in between."
    )


def test_migration_021_creates_concurrent_index():
    """The upgrade must use ``CREATE INDEX CONCURRENTLY`` so production
    logins keep working during the build."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "idx_sessions_expires_at" in src
    assert "postgresql_concurrently=True" in src, (
        "P2-24 regression: migration 021 builds the index without "
        "CONCURRENTLY. That takes an ACCESS EXCLUSIVE lock on the "
        "sessions table, blocking every login on a busy production DB "
        "for the entire build (potentially minutes on a 100k-row table)."
    )
    # The autocommit_block context manager opts this migration out of
    # alembic's default transaction wrap so CREATE INDEX CONCURRENTLY
    # actually works (it can't run inside a transaction).
    assert "autocommit_block" in src, (
        "P2-24 regression: migration 021 does not use "
        "op.get_context().autocommit_block(). Without it, "
        "CREATE INDEX CONCURRENTLY raises 'CONCURRENTLY cannot run "
        "inside a transaction block' because alembic wraps each "
        "migration in BEGIN/COMMIT by default."
    )


def test_migration_021_handles_invalid_index_leftover():
    """Codex review of 9d25be7: a previous ``CREATE INDEX
    CONCURRENTLY`` that was cancelled mid-build leaves a row in
    ``pg_index`` with ``indisvalid=false``. Vanilla ``if_not_exists=True``
    sees the name, skips creation, and lets alembic mark this revision
    applied — but the planner won't actually use the dead index.

    The migration must probe pg_index for INVALID rows and DROP them
    before CREATE, so a partial-failure rerun actually rebuilds.
    """
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    # The probe SQL.
    assert "indisvalid = false" in src, (
        "P2-24 follow-up regression: migration 021 no longer probes "
        "pg_index for INVALID leftovers. A cancelled CREATE INDEX "
        "CONCURRENTLY would leave a dead index that the next deploy "
        "would silently rubber-stamp via if_not_exists=True."
    )
    # The cleanup statement.
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in src, (
        "P2-24 follow-up regression: migration 021 detects INVALID "
        "leftovers but doesn't DROP CONCURRENTLY them. The CREATE "
        "step would then skip via if_not_exists=True, leaving the "
        "DB with a dead index."
    )


def test_migration_021_downgrade_drops_index():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "drop_index" in src
    assert "idx_sessions_expires_at" in src


# =====================================================================
# §2 — Model ``__table_args__`` declares the index
# =====================================================================


def test_session_model_has_expires_at_index():
    """SQLAlchemy autogenerate compares the model to the live schema.
    If the model omits the index, autogenerate would suggest dropping
    it on the next revision — silently re-opening the perf cliff."""
    from models import Session

    indexes = list(Session.__table__.indexes)
    matched = [
        idx for idx in indexes
        if idx.name == "idx_sessions_expires_at"
    ]
    assert len(matched) == 1, (
        "P2-24 regression: Session.__table_args__ is missing the "
        "expires_at index. Found indexes: "
        f"{sorted(idx.name for idx in indexes if idx.name)}. Without "
        "the model declaration, alembic autogenerate would propose "
        "dropping it on next revision."
    )
    idx = matched[0]
    assert [c.name for c in idx.columns] == ["expires_at"], (
        f"P2-24 regression: idx_sessions_expires_at column drift; "
        f"got {[c.name for c in idx.columns]!r}, expected ['expires_at']."
    )

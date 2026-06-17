"""Guards for migration 036 payment order reconciliation marker."""
from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT
    / "gateway"
    / "alembic"
    / "versions"
    / "036_payment_order_last_reconciled_at.py"
)


def _revision_literal(src: str) -> str:
    match = re.search(r'revision:\s*str\s*=\s*"([^"]+)"', src)
    assert match is not None, "migration 036 revision literal not found"
    return match.group(1)


def test_migration_036_revision_id_fits_alembic_version_column() -> None:
    """Production alembic_version.version_num is VARCHAR(32)."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    revision = _revision_literal(src)

    assert revision == "036_payment_order_reconcile"
    assert len(revision) <= 32


def test_migration_036_revises_anonymous_preview() -> None:
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "035_anonymous_preview"' in src

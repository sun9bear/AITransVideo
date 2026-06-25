"""PR-E migration 043 — user_voices.compatible_target_languages consistency guard.

Mirrors the 031 guard style (AST migration parse + ORM reflection, no real DB):
1. migration 043 adds ``compatible_target_languages`` to ``user_voices``
2. UserVoice ORM has the same column (no ORM↔DB schema drift)
3. down_revision chains to 042 (no broken alembic head)
4. revision id fits the production ``alembic_version.version_num`` VARCHAR(32)
5. downgrade drops the column (symmetric)
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "gateway" / "alembic" / "versions"
    / "043_user_voices_compatible_target_languages.py"
)
_COL = "compatible_target_languages"


def _migration_src() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def _added_columns() -> set[str]:
    tree = ast.parse(_migration_src())
    cols: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_column" or len(node.args) < 2:
            continue
        tbl = node.args[0]
        if not (isinstance(tbl, ast.Constant) and tbl.value == "user_voices"):
            continue
        col = node.args[1]
        if (isinstance(col, ast.Call) and isinstance(col.func, ast.Attribute)
                and col.func.attr == "Column" and col.args
                and isinstance(col.args[0], ast.Constant)):
            cols.add(col.args[0].value)
    return cols


def test_migration_043_adds_compatible_target_languages_to_user_voices() -> None:
    assert _COL in _added_columns()


def test_migration_043_down_revision_chains_to_042() -> None:
    assert 'down_revision: Union[str, None] = "042_voice_compat_target_langs"' in _migration_src()


def test_migration_043_revision_id_fits_alembic_version_column() -> None:
    tree = ast.parse(_migration_src())
    revision_value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "revision" \
                and isinstance(node.value, ast.Constant):
            revision_value = node.value.value
            break
    assert revision_value == "043_uservoice_compat_langs"
    assert len(revision_value) <= 32  # production alembic_version.version_num VARCHAR(32)


def test_migration_043_downgrade_drops_the_column() -> None:
    assert f'op.drop_column("user_voices", "{_COL}")' in _migration_src()


def test_uservoice_orm_has_compatible_target_languages() -> None:
    from models import UserVoice  # type: ignore[import-not-found]

    cols = {c.name for c in UserVoice.__table__.columns}
    assert _COL in cols, "UserVoice ORM missing compatible_target_languages (043)"

"""Phase 4.3b-A — user_voices cleanup tracking migration 033 + ORM 守卫。

守护（spec §3 + §11 4.3b-A）：

1. migration 033 revision chain：revision='033_*'，down_revision='032_*'（接 PR2 head）
2. migration 加 5 列 + downgrade 对称 drop（不新表 / 不新索引）
3. ORM UserVoice 5 个 cleanup_* 列：类型 / nullable / server_default 与 spec §3 对齐
   - cleanup_attempts INT NOT NULL server_default 0
   - cleanup_retry_after / cleanup_claim_until: TIMESTAMPTZ nullable
   - cleanup_last_error: String(200) nullable
   - cleanup_run_id: String(36) nullable
4. 复用现有 idx_user_voices_temp_expires_pending（不改它）+ 不污染 reservation 表

设计：ORM 反射（运行时真源）为主 + migration AST 验 revision chain（同 PR2-A 模式）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "gateway"
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

MIGRATION_PATH = GATEWAY / "alembic" / "versions" / "033_user_voice_cleanup_tracking.py"

_CLEANUP_COLUMNS = (
    "cleanup_attempts",
    "cleanup_retry_after",
    "cleanup_last_error",
    "cleanup_claim_until",
    "cleanup_run_id",
)


# ---------------------------------------------------------------------------
# 1. migration revision chain
# ---------------------------------------------------------------------------


def _migration_assignments() -> dict[str, str | None]:
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))
    out: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name in ("revision", "down_revision") and isinstance(node.value, ast.Constant):
                out[name] = node.value.value
    return out


def test_migration_033_exists():
    assert MIGRATION_PATH.exists(), f"migration 033 缺失: {MIGRATION_PATH}"


def test_migration_033_revision_chain():
    """revision='033_*'，down_revision='032_*'（接 PR2 head 032）。"""
    a = _migration_assignments()
    assert a.get("revision") == "033_user_voice_cleanup_tracking"
    assert a.get("down_revision") == "032_express_clone_reservations", (
        f"down_revision 必须是 032（PR2 head），实际 {a.get('down_revision')!r}"
    )


def test_migration_033_adds_five_columns_symmetric_downgrade():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    for col in _CLEANUP_COLUMNS:
        assert f'"{col}"' in src, f"migration 缺列 {col}"
    # upgrade 用 add_column，downgrade 用 drop_column，且对称（各 5 次）
    assert src.count("op.add_column(") == 5, "upgrade 应 add 恰好 5 列"
    assert src.count("op.drop_column(") == 5, "downgrade 应 drop 恰好 5 列（对称）"
    # 4.3b-A 不新表 / 不新索引（复用现有 expired_at + 部分索引）
    assert "create_table" not in src, "4.3b-A 不应新建表"
    assert "create_index" not in src, "4.3b-A 不应新建索引（复用现有部分索引）"


# ---------------------------------------------------------------------------
# 2. ORM UserVoice 5 列
# ---------------------------------------------------------------------------


def test_orm_user_voice_has_cleanup_columns():
    from models import UserVoice
    cols = set(UserVoice.__table__.columns.keys())
    missing = set(_CLEANUP_COLUMNS) - cols
    assert not missing, f"ORM UserVoice 缺 cleanup 列: {missing}"


def test_orm_cleanup_attempts_not_null_default_zero():
    from models import UserVoice
    col = UserVoice.__table__.columns["cleanup_attempts"]
    assert col.nullable is False, "cleanup_attempts 必须 NOT NULL"
    sd = col.server_default
    assert sd is not None and "0" in str(sd.arg.text), "cleanup_attempts server_default 必须 0"


def test_orm_cleanup_nullable_columns():
    """retry_after / last_error / claim_until / run_id 默认 NULL（未清理行不占值）。"""
    from models import UserVoice
    cols = UserVoice.__table__.columns
    for name in (
        "cleanup_retry_after",
        "cleanup_last_error",
        "cleanup_claim_until",
        "cleanup_run_id",
    ):
        assert cols[name].nullable is True, f"{name} 必须 nullable"


def test_orm_cleanup_string_lengths():
    """last_error String(200) / run_id String(36)（与 spec §3 一致）。"""
    from models import UserVoice
    cols = UserVoice.__table__.columns
    assert cols["cleanup_last_error"].type.length == 200
    assert cols["cleanup_run_id"].type.length == 36


def test_orm_reuses_existing_temp_expires_index_unchanged():
    """复用现有部分索引 idx_user_voices_temp_expires_pending（4.3b 不改它）。"""
    from models import UserVoice
    idx = next(
        (i for i in UserVoice.__table__.indexes if i.name == "idx_user_voices_temp_expires_pending"),
        None,
    )
    assert idx is not None, "现有部分索引 idx_user_voices_temp_expires_pending 应仍在"
    where = idx.dialect_options.get("postgresql", {}).get("where")
    assert where is not None and "is_temporary" in str(where) and "expired_at" in str(where)


def test_orm_cleanup_columns_only_on_user_voices():
    """守卫：cleanup 列只加在 user_voices，不污染 reservation 表（职责分离）。"""
    from models import ExpressCloneReservation, UserVoice
    resv_cols = set(ExpressCloneReservation.__table__.columns.keys())
    for col in _CLEANUP_COLUMNS:
        assert col in UserVoice.__table__.columns.keys()
        assert col not in resv_cols, f"{col} 不应出现在 reservation 表"


# ---------------------------------------------------------------------------
# 3. 不破坏既有 migration 链
# ---------------------------------------------------------------------------


def test_032_and_031_migrations_still_intact():
    for name in (
        "032_express_clone_reservations.py",
        "031_user_voice_temp_expiry.py",
    ):
        assert (GATEWAY / "alembic" / "versions" / name).exists(), f"{name} 不应被 4.3b 触碰"

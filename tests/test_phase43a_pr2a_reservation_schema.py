"""Phase 4.3a PR2-A — express_clone_reservations migration 032 + ORM + admin TTL 守卫。

守护四件事（spec §3 + §12 PR2-A）：

1. migration 032 revision chain：revision='032_*'，down_revision='031_*'
2. ORM ExpressCloneReservation 表结构：列 / nullable / server_default 与
   spec §3 对齐（特别是 expires_at NOT NULL —— 永不过期是 bug）
3. 三个索引：uq_express_reservation_active(partial unique where status='reserved')
   / idx_express_reservation_user_status / idx_express_reservation_ttl_pending
   (partial where status='reserved')
4. admin reservation_ttl_minutes validator [5, 120] + 前端 DEFAULT_SETTINGS/reset

设计：ORM 反射（运行时真源）为主 + migration AST 验 revision chain。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "gateway"
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

MIGRATION_PATH = GATEWAY / "alembic" / "versions" / "032_express_clone_reservations.py"
ADMIN_SETTINGS_PAGE = (
    REPO_ROOT / "frontend-next" / "src" / "app" / "[locale]" / "(app)" / "admin"
    / "settings" / "page.tsx"
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


def test_migration_032_exists():
    assert MIGRATION_PATH.exists(), f"migration 032 缺失: {MIGRATION_PATH}"


def test_migration_032_revision_chain():
    """revision='032_*'，down_revision='031_*'（接在 PR1 head 031 后）。"""
    a = _migration_assignments()
    assert a.get("revision") == "032_express_clone_reservations"
    assert a.get("down_revision") == "031_user_voice_temp_expiry", (
        f"down_revision 必须是 031（PR1 head），实际 {a.get('down_revision')!r}"
    )


def test_migration_032_creates_table_and_three_indexes():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'op.create_table(\n        "express_clone_reservations"' in src or \
           'create_table(' in src and '"express_clone_reservations"' in src
    for idx in (
        "uq_express_reservation_active",
        "idx_express_reservation_user_status",
        "idx_express_reservation_ttl_pending",
    ):
        assert idx in src, f"migration 缺索引 {idx}"
    # partial where
    assert "status = 'reserved'" in src, "partial index where 子句缺失"
    # downgrade 对称
    assert "drop_table" in src and "drop_index" in src


# ---------------------------------------------------------------------------
# 2. ORM ExpressCloneReservation 表结构
# ---------------------------------------------------------------------------


def test_orm_table_name_and_columns():
    from models import ExpressCloneReservation
    t = ExpressCloneReservation.__table__
    assert t.name == "express_clone_reservations"
    expected = {
        "id", "user_id", "job_id", "speaker_id", "status", "target_model",
        "created_at", "updated_at", "expires_at", "consumed_voice_id",
        "released_reason",
    }
    actual = set(t.columns.keys())
    assert expected <= actual, f"ORM 缺列: {expected - actual}"


def test_orm_expires_at_not_null():
    """spec §4.1：expires_at NOT NULL —— 永不过期的 reservation 是 bug。"""
    from models import ExpressCloneReservation
    col = ExpressCloneReservation.__table__.columns["expires_at"]
    assert col.nullable is False, "expires_at 必须 NOT NULL（永不过期是 bug）"


def test_orm_status_server_default_reserved():
    from models import ExpressCloneReservation
    col = ExpressCloneReservation.__table__.columns["status"]
    assert col.nullable is False
    # server_default 文本含 'reserved'
    sd = col.server_default
    assert sd is not None and "reserved" in str(sd.arg.text), (
        "status server_default 必须是 'reserved'"
    )


def test_orm_nullable_optional_columns():
    """consume/release 字段在 reserved 阶段为 NULL → 必须 nullable。"""
    from models import ExpressCloneReservation
    cols = ExpressCloneReservation.__table__.columns
    assert cols["consumed_voice_id"].nullable is True
    assert cols["released_reason"].nullable is True


def test_orm_required_columns_not_null():
    from models import ExpressCloneReservation
    cols = ExpressCloneReservation.__table__.columns
    for name in ("user_id", "job_id", "speaker_id", "target_model"):
        assert cols[name].nullable is False, f"{name} 必须 NOT NULL"


def test_orm_partial_unique_active_index():
    """uq_express_reservation_active：partial UNIQUE where status='reserved'
    （spec §2.3 幂等第二道防线）。"""
    from models import ExpressCloneReservation
    t = ExpressCloneReservation.__table__
    idx = next((i for i in t.indexes if i.name == "uq_express_reservation_active"), None)
    assert idx is not None, "缺 uq_express_reservation_active 索引"
    assert idx.unique is True, "必须 unique"
    cols = [c.name for c in idx.columns]
    assert cols == ["user_id", "job_id", "speaker_id"], f"列顺序错: {cols}"
    where = idx.dialect_options.get("postgresql", {}).get("where")
    assert where is not None and "reserved" in str(where), (
        "partial unique where 必须含 status='reserved'"
    )


def test_orm_ttl_pending_partial_index():
    """idx_express_reservation_ttl_pending：partial where status='reserved'
    （TTL sweeper + reserve inline expire 选行；spec §4.1/§8）。"""
    from models import ExpressCloneReservation
    t = ExpressCloneReservation.__table__
    idx = next(
        (i for i in t.indexes if i.name == "idx_express_reservation_ttl_pending"), None
    )
    assert idx is not None
    where = idx.dialect_options.get("postgresql", {}).get("where")
    assert where is not None and "reserved" in str(where)


def test_orm_user_status_count_index():
    from models import ExpressCloneReservation
    t = ExpressCloneReservation.__table__
    idx = next(
        (i for i in t.indexes if i.name == "idx_express_reservation_user_status"), None
    )
    assert idx is not None, "缺 budget count 索引 idx_express_reservation_user_status"


def test_orm_does_not_pollute_user_voices():
    """守卫：reservation 是独立表，不复用 user_voices（spec §2.1 决策 1）。"""
    from models import ExpressCloneReservation, UserVoice
    assert ExpressCloneReservation.__tablename__ == "express_clone_reservations"
    assert UserVoice.__tablename__ == "user_voices"
    assert ExpressCloneReservation.__table__ is not UserVoice.__table__


# ---------------------------------------------------------------------------
# 3. admin reservation_ttl_minutes validator + 前端
# ---------------------------------------------------------------------------


def test_admin_reservation_ttl_field_default_30():
    from admin_settings import AdminSettings
    assert AdminSettings().express_cosyvoice_auto_clone_reservation_ttl_minutes == 30


def test_admin_reservation_ttl_validator_5_to_120():
    from admin_settings import AdminSettings
    import pydantic
    # 合法边界
    for ok in (5, 30, 120):
        AdminSettings(express_cosyvoice_auto_clone_reservation_ttl_minutes=ok)
    # 越界拒收
    for bad in (0, 4, 121, 10080):  # 10080 = 7 天
        try:
            AdminSettings(express_cosyvoice_auto_clone_reservation_ttl_minutes=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(f"ttl_minutes 接受了越界值 {bad}（应 [5,120] 拒收）")


def test_frontend_default_settings_includes_reservation_ttl():
    src = ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")
    assert "express_cosyvoice_auto_clone_reservation_ttl_minutes: number" in src, (
        "interface 缺 reservation_ttl_minutes"
    )
    assert "express_cosyvoice_auto_clone_reservation_ttl_minutes: 30," in src, (
        "DEFAULT_SETTINGS 缺 reservation_ttl_minutes 默认 30"
    )


def test_frontend_reset_passes_through_reservation_ttl():
    """reset 按钮透传 reservation_ttl_minutes（full-body save 守卫，PR1 D.1 同模式）。"""
    src = ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")
    assert "s.express_cosyvoice_auto_clone_reservation_ttl_minutes" in src, (
        "reset 按钮未透传 reservation_ttl_minutes"
    )


# ---------------------------------------------------------------------------
# 4. PR1 alembic head 守卫现仍存在（不破坏 031 链）
# ---------------------------------------------------------------------------


def test_031_migration_still_intact():
    p = GATEWAY / "alembic" / "versions" / "031_user_voice_temp_expiry.py"
    assert p.exists(), "031 migration 不应被 PR2 触碰"

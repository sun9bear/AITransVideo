"""P3a — smart_clone_reservations + clone_billing_events 数据模型守卫（migration 037）.

plan 2026-06-14-p3-smart-clone-600-credit-subplan v3。守护智能版预览克隆的
**钱-正确性账本** schema（CodeX P0 硬要求）：

1. migration 037 revision chain：revision='037_*'，down_revision='036_*'（head）
2. ORM SmartCloneReservation：列 / nullable / server_default；状态机
   reserved→captured|released|expired；expires_at NOT NULL（TTL，永不过期是 bug）
3. partial unique (task_id, purpose) where status='reserved'（幂等第二道防线）
   + TTL pending partial + user_status count 索引
4. ORM CloneBillingEvent：唯一 reservation_id（幂等，一 reservation 一 event）；
   chargeable 列；与 reservation FK

设计同 test_phase43a_pr2a_reservation_schema：ORM 反射 + migration AST。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "gateway"
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

MIGRATION_PATH = GATEWAY / "alembic" / "versions" / "037_smart_clone_reservations.py"


# ---------------------------------------------------------------------------
# 1. migration revision chain（037 接 036 head）
# ---------------------------------------------------------------------------


def _migration_assignments() -> dict[str, object]:
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name in ("revision", "down_revision") and isinstance(node.value, ast.Constant):
                out[name] = node.value.value
    return out


def test_migration_037_exists():
    assert MIGRATION_PATH.exists(), f"migration 037 缺失: {MIGRATION_PATH}"


def test_migration_037_revision_chain():
    a = _migration_assignments()
    assert a.get("revision") == "037_smart_clone_reservations"
    assert a.get("down_revision") == "036_job_language_fields", (
        f"down_revision 必须是 036（当前 head），实际 {a.get('down_revision')!r}"
    )


def test_migration_037_creates_both_tables_and_indexes():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"smart_clone_reservations"' in src
    assert '"clone_billing_events"' in src
    for idx in (
        "uq_smart_clone_reservation_active",
        "idx_smart_clone_reservation_user_status",
        "idx_smart_clone_reservation_ttl_pending",
        "uq_clone_billing_event_reservation",
        "idx_clone_billing_event_task",
    ):
        assert idx in src, f"migration 缺索引/约束 {idx}"
    assert "status = 'reserved'" in src, "partial index where 子句缺失"
    # downgrade 对称
    assert src.count("drop_table") >= 2 and "drop_index" in src


def test_migration_037_has_only_production_chain_child():
    """037 已接入生产迁移链，且只能由 038 继续向后延伸。"""
    versions = (GATEWAY / "alembic" / "versions")
    refs_037 = [
        p.name for p in versions.glob("*.py")
        if p.name != "037_smart_clone_reservations.py"
        and "037_smart_clone_reservations" in p.read_text(encoding="utf-8")
    ]
    assert refs_037 == ["038_smart_clone_created_at_index.py"], (
        f"037 must only feed the current production chain: {refs_037}"
    )


# ---------------------------------------------------------------------------
# 2. ORM SmartCloneReservation
# ---------------------------------------------------------------------------


def test_reservation_table_name_and_columns():
    from models import SmartCloneReservation
    t = SmartCloneReservation.__table__
    assert t.name == "smart_clone_reservations"
    expected = {
        "id", "user_id", "task_id", "purpose", "amount_credits", "status",
        "created_at", "updated_at", "expires_at", "settled_at",
        "captured_voice_id", "reason_code", "carryover_applied_to_task_id",
    }
    actual = set(t.columns.keys())
    assert expected <= actual, f"ORM 缺列: {expected - actual}"


def test_reservation_expires_at_not_null():
    """TTL：expires_at NOT NULL —— 永不过期 reservation = 永久占点/占库容 bug。"""
    from models import SmartCloneReservation
    assert SmartCloneReservation.__table__.columns["expires_at"].nullable is False


def test_reservation_status_server_default_reserved():
    from models import SmartCloneReservation
    col = SmartCloneReservation.__table__.columns["status"]
    assert col.nullable is False
    sd = col.server_default
    assert sd is not None and "reserved" in str(sd.arg.text)


def test_reservation_required_and_optional_columns():
    from models import SmartCloneReservation
    cols = SmartCloneReservation.__table__.columns
    for name in ("user_id", "task_id", "purpose", "amount_credits", "status", "expires_at"):
        assert cols[name].nullable is False, f"{name} 必须 NOT NULL"
    for name in (
        "settled_at",
        "captured_voice_id",
        "reason_code",
        "carryover_applied_to_task_id",
    ):
        assert cols[name].nullable is True, f"{name} 在 reserved 阶段为 NULL → 必须 nullable"


def test_reservation_partial_unique_active():
    """幂等第二道防线：partial UNIQUE (task_id, purpose) where status='reserved'。"""
    from models import SmartCloneReservation
    t = SmartCloneReservation.__table__
    idx = next((i for i in t.indexes if i.name == "uq_smart_clone_reservation_active"), None)
    assert idx is not None, "缺 uq_smart_clone_reservation_active"
    assert idx.unique is True
    assert [c.name for c in idx.columns] == ["task_id", "purpose"]
    where = idx.dialect_options.get("postgresql", {}).get("where")
    assert where is not None and "reserved" in str(where)


def test_reservation_ttl_pending_partial_index():
    from models import SmartCloneReservation
    t = SmartCloneReservation.__table__
    idx = next((i for i in t.indexes if i.name == "idx_smart_clone_reservation_ttl_pending"), None)
    assert idx is not None
    where = idx.dialect_options.get("postgresql", {}).get("where")
    assert where is not None and "reserved" in str(where)


def test_reservation_user_status_count_index():
    from models import SmartCloneReservation
    t = SmartCloneReservation.__table__
    assert any(i.name == "idx_smart_clone_reservation_user_status" for i in t.indexes)
    assert any(i.name == "idx_smart_clone_reservation_created_at" for i in t.indexes)
    assert any(i.name == "idx_smart_clone_reservation_carryover" for i in t.indexes)


# ---------------------------------------------------------------------------
# 3. ORM CloneBillingEvent（唯一权威计费信号 + 幂等）
# ---------------------------------------------------------------------------


def test_billing_event_table_and_columns():
    from models import CloneBillingEvent
    t = CloneBillingEvent.__table__
    assert t.name == "clone_billing_events"
    expected = {"id", "task_id", "reservation_id", "provider", "voice_id", "chargeable", "created_at"}
    assert expected <= set(t.columns.keys())


def test_billing_event_required_columns_not_null():
    from models import CloneBillingEvent
    cols = CloneBillingEvent.__table__.columns
    for name in ("task_id", "reservation_id", "provider", "voice_id", "chargeable"):
        assert cols[name].nullable is False, f"{name} 必须 NOT NULL"


def test_billing_event_unique_reservation_idempotency():
    """🔥 幂等：reservation_id 唯一约束——一个 reservation 最多一条 event，
    防重复写 → 防重复 capture（CodeX P0-2）。"""
    from models import CloneBillingEvent
    t = CloneBillingEvent.__table__
    uniques = [
        c for c in t.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert any(
        [col.name for col in c.columns] == ["reservation_id"] for c in uniques
    ), "缺 reservation_id 唯一约束（幂等）"


def test_billing_event_fk_to_reservation():
    from models import CloneBillingEvent
    fks = list(CloneBillingEvent.__table__.columns["reservation_id"].foreign_keys)
    assert any("smart_clone_reservations" in fk.target_fullname for fk in fks)


# ---------------------------------------------------------------------------
# 4. 独立表，不污染 user_voices
# ---------------------------------------------------------------------------


def test_tables_independent_of_user_voices():
    from models import SmartCloneReservation, CloneBillingEvent, UserVoice
    assert SmartCloneReservation.__table__ is not UserVoice.__table__
    assert CloneBillingEvent.__table__ is not UserVoice.__table__


# ---------------------------------------------------------------------------
# 5. 运行时建表 + 关键约束实测（SQLite in-memory，反射 ORM 真源）
# ---------------------------------------------------------------------------


def test_sqlite_create_and_partial_unique_idempotency():
    """SQLite 建两表，实测 partial unique：同 (task_id,purpose) 第二个 reserved
    插入失败；但 released 后可再插 reserved（lifecycle 不被全量 unique 卡死）。"""
    import uuid
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import IntegrityError
    from models import SmartCloneReservation

    eng = create_engine("sqlite://")
    SmartCloneReservation.__table__.create(eng)
    now = datetime.now(timezone.utc)
    uid = uuid.uuid4()

    def _row(status="reserved"):
        return SmartCloneReservation(
            id=uuid.uuid4(), user_id=uid, task_id="job_x",
            purpose="smart_clone_minimax_600", amount_credits=600, status=status,
            created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
        )

    with Session(eng) as s:
        s.add(_row("reserved"))
        s.commit()
        # 第二个 active(reserved) 同 (task_id,purpose) → 唯一冲突
        s.add(_row("reserved"))
        raised = False
        try:
            s.commit()
        except IntegrityError:
            raised = True
            s.rollback()
        assert raised, "同 (task_id,purpose) 第二个 reserved 必须被 partial unique 拒"

        # 把第一条置 released 后，可再插 reserved（partial 不锁历史行）
        first = s.query(SmartCloneReservation).first()
        first.status = "released"
        s.commit()
        s.add(_row("reserved"))
        s.commit()  # 不应抛

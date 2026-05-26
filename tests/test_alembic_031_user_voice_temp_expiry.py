"""Phase 4.2 A.1 — migration 031 与 UserVoice ORM 一致性 + 命名守卫。

migration 031 给 ``user_voices`` 加 2 个新字段 + 1 个 partial index 支撑
CosyVoice 临时音色生命周期。本测试集守护四件事：

1. migration 加的字段 / 类型 / nullable / server_default 与 plan v4-followup
   §12.3 严格对齐
2. UserVoice ORM 必须有同 2 个字段，类型 / nullable / server_default 与
   migration 对齐（防 ORM-DB schema drift）
3. partial index 必须真带 ``postgresql_where`` + WHERE 表达式与 plan §12.4
   清理 sweeper SELECT 对齐
4. 字段命名守护：**绝不**简写成 ``expires_at`` —— 与同表现有 ``expired_at``
   软删时间戳冲突会让后续 query 大概率写错（Codex 2026-05-26 v4-followup
   review 重点）

设计：不连真实 DB，AST 解析 migration + SQLAlchemy ORM 反射两端比对。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "gateway" / "alembic" / "versions"
    / "031_user_voice_temp_expiry.py"
)


# Phase 4.2 A.1 plan v4-followup §12.3 明确的 2 个字段 —— 任何漂移立刻 red.
_EXPECTED_FIELDS: dict[str, dict] = {
    "is_temporary": {
        "sql_type": "Boolean()",
        "nullable": False,
        "server_default": "false",  # 旧 row 兜底 (sa.false())
    },
    "temporary_expires_at": {
        "sql_type": "DateTime(timezone=True)",
        "nullable": True,
        "server_default": None,
    },
}


# ----------------------------------------------------------------------
# AST helpers (复用 030 测试的解析模式)
# ----------------------------------------------------------------------


def _parse_migration_add_columns() -> dict[str, dict]:
    """AST 扫 migration 031 的 ``op.add_column("user_voices", sa.Column(...))``
    调用，返回 ``{col_name: {sql_type, nullable, server_default}}``。
    """
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))

    found: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_column":
            continue
        if len(node.args) < 2:
            continue
        # 第一个 positional arg 必须是 table 名
        tbl_node = node.args[0]
        if not (isinstance(tbl_node, ast.Constant) and tbl_node.value == "user_voices"):
            continue

        col_node = node.args[1]
        if not (isinstance(col_node, ast.Call) and isinstance(col_node.func, ast.Attribute)
                and col_node.func.attr == "Column"):
            continue

        if not col_node.args:
            continue
        name_node = col_node.args[0]
        if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
            continue
        col_name = name_node.value

        if len(col_node.args) < 2:
            continue
        type_node = col_node.args[1]
        sql_type = ast.unparse(type_node).replace("sa.", "")

        nullable = None
        server_default = None
        for kw in col_node.keywords:
            if kw.arg == "nullable" and isinstance(kw.value, ast.Constant):
                nullable = kw.value.value
            elif kw.arg == "server_default":
                server_default = _normalize_server_default(kw.value)

        found[col_name] = {
            "sql_type": sql_type,
            "nullable": nullable,
            "server_default": server_default,
        }
    return found


def _normalize_server_default(node: ast.AST) -> str | None:
    """规范化 ``server_default`` AST 节点到字符串形态。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call):
        # sa.false() / sa.true()
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("false", "true"):
            return node.func.attr
        if isinstance(node.func, ast.Attribute) and node.func.attr == "text":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
        if isinstance(node.func, ast.Name) and node.func.id == "text":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
    return None


# ----------------------------------------------------------------------
# Migration-side asserts
# ----------------------------------------------------------------------


def test_migration_031_adds_all_2_phase42_a1_fields() -> None:
    """Migration 031 必须 add 全部 2 个 Phase 4.2 A.1 字段。"""
    actual = _parse_migration_add_columns()
    missing = set(_EXPECTED_FIELDS.keys()) - set(actual.keys())
    assert not missing, (
        f"Migration 031 缺 Phase 4.2 A.1 字段: {missing}; 现有: {list(actual.keys())}"
    )


def test_migration_031_does_not_add_extra_fields() -> None:
    """Migration 031 不能多加意外字段（防 scope creep）。"""
    actual = _parse_migration_add_columns()
    extra = set(actual.keys()) - set(_EXPECTED_FIELDS.keys())
    assert not extra, (
        f"Migration 031 多了 plan 未列字段: {extra}; "
        f"改前先更新 _EXPECTED_FIELDS"
    )


@pytest.mark.parametrize("field_name, expected", list(_EXPECTED_FIELDS.items()))
def test_migration_031_field_type_and_nullable_locked(
    field_name: str, expected: dict,
) -> None:
    """每字段 SQL 类型 + nullable 锁死。"""
    actual = _parse_migration_add_columns()
    assert field_name in actual, f"migration 缺字段 {field_name}"
    actual_field = actual[field_name]
    assert actual_field["sql_type"] == expected["sql_type"], (
        f"{field_name} SQL 类型不符：expected {expected['sql_type']!r}，"
        f"actual {actual_field['sql_type']!r}"
    )
    assert actual_field["nullable"] == expected["nullable"], (
        f"{field_name} nullable 不符：expected {expected['nullable']}, "
        f"actual {actual_field['nullable']}"
    )


@pytest.mark.parametrize("field_name, expected", list(_EXPECTED_FIELDS.items()))
def test_migration_031_field_server_default_locked(
    field_name: str, expected: dict,
) -> None:
    """``is_temporary`` 必须有 server_default=sa.false() 让旧 row 兜底；
    ``temporary_expires_at`` 无 server_default（应用层填）。"""
    actual = _parse_migration_add_columns()
    actual_default = actual[field_name]["server_default"]
    expected_default = expected["server_default"]
    assert actual_default == expected_default, (
        f"{field_name} server_default 不符：expected {expected_default!r}, "
        f"actual {actual_default!r}"
    )


def test_migration_031_revises_030() -> None:
    """031 的 down_revision 必须是 030，防 alembic head 链断。"""
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "030_cosyvoice_clone_metadata"' in src, (
        "031 down_revision 必须是 030_cosyvoice_clone_metadata"
    )


def test_migration_031_revision_id_fits_alembic_version_column() -> None:
    """Production ``alembic_version.version_num`` 是 VARCHAR(32)."""
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    revision_value = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        if not (isinstance(node.target, ast.Name) and node.target.id == "revision"):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            revision_value = node.value.value
            break

    assert revision_value == "031_user_voice_temp_expiry"
    assert len(revision_value) <= 32


def test_migration_031_has_downgrade_for_all_added_columns() -> None:
    """downgrade() 必须 drop 所有 upgrade() 加的字段 + 加的 index。"""
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    for col in _EXPECTED_FIELDS.keys():
        assert f'op.drop_column("user_voices", "{col}")' in src, (
            f"downgrade() 缺 drop_column({col!r})，与 upgrade 不对称"
        )
    assert 'op.drop_index(' in src and 'idx_user_voices_temp_expires_pending' in src, (
        "downgrade() 缺 drop_index('idx_user_voices_temp_expires_pending', ...)"
    )


def test_migration_031_partial_index_where_clause() -> None:
    """``idx_user_voices_temp_expires_pending`` 必须是 partial index，
    WHERE 子句必须包含 ``is_temporary = TRUE AND expired_at IS NULL``。

    plan v4-followup §12.4 清理 sweeper 的 SELECT 条件依赖这个 partial 索引：
    sweeper 成功删 DashScope voice 后写 ``expired_at=now()`` → 该行从 index
    自动剔除（幂等）。
    """
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))

    found = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_index"):
            continue
        if not node.args:
            continue
        name_node = node.args[0]
        if not (isinstance(name_node, ast.Constant)
                and name_node.value == "idx_user_voices_temp_expires_pending"):
            continue
        found = True

        # 必须有 postgresql_where kwarg
        where_kw = next(
            (kw for kw in node.keywords if kw.arg == "postgresql_where"),
            None,
        )
        assert where_kw is not None, (
            "idx_user_voices_temp_expires_pending 必须是 partial index "
            "（带 postgresql_where=...），与 plan §12.4 sweeper SELECT 对齐"
        )

        # WHERE 子句必须含两个条件
        where_text = ast.unparse(where_kw.value)
        assert "is_temporary" in where_text and "TRUE" in where_text.upper(), (
            f"partial WHERE 必须含 'is_temporary = TRUE'；实际: {where_text!r}"
        )
        assert "expired_at" in where_text and "NULL" in where_text.upper(), (
            f"partial WHERE 必须含 'expired_at IS NULL'；实际: {where_text!r}"
        )

    assert found, "找不到 create_index('idx_user_voices_temp_expires_pending', ...)"


# ----------------------------------------------------------------------
# ORM-side asserts (反射比对)
# ----------------------------------------------------------------------


def test_uservoice_orm_has_all_phase42_a1_fields() -> None:
    """``UserVoice`` ORM 必须有 migration 031 加的全部 2 个字段。"""
    from models import UserVoice  # type: ignore[import-not-found]

    orm_columns = {c.name for c in UserVoice.__table__.columns}
    missing = set(_EXPECTED_FIELDS.keys()) - orm_columns
    assert not missing, (
        f"UserVoice ORM 缺 Phase 4.2 A.1 字段 {missing}；"
        f"migration 跑完后 SQLAlchemy 不感知这些字段，下次 autogenerate 会"
        f"误提议 DROP COLUMN"
    )


@pytest.mark.parametrize("field_name, expected", list(_EXPECTED_FIELDS.items()))
def test_uservoice_orm_field_nullable_matches_migration(
    field_name: str, expected: dict,
) -> None:
    """ORM 字段 nullable 与 migration 一致。"""
    from models import UserVoice  # type: ignore[import-not-found]

    columns = {c.name: c for c in UserVoice.__table__.columns}
    col = columns[field_name]
    assert col.nullable == expected["nullable"], (
        f"{field_name} ORM nullable={col.nullable}, "
        f"migration nullable={expected['nullable']}; 必须一致"
    )


@pytest.mark.parametrize("field_name, expected", list(_EXPECTED_FIELDS.items()))
def test_uservoice_orm_field_server_default_matches_migration(
    field_name: str, expected: dict,
) -> None:
    """ORM ``server_default`` 与 migration 一致。"""
    from models import UserVoice  # type: ignore[import-not-found]

    columns = {c.name: c for c in UserVoice.__table__.columns}
    col = columns[field_name]
    expected_default = expected["server_default"]

    if expected_default is None:
        assert col.server_default is None, (
            f"{field_name} migration 无 server_default，但 ORM 设了 "
            f"{col.server_default!r}"
        )
        return

    assert col.server_default is not None, (
        f"{field_name} migration 设了 server_default={expected_default!r}, "
        f"但 ORM 端 server_default 是 None"
    )
    arg = col.server_default.arg
    arg_text = str(arg) if not isinstance(arg, str) else arg
    assert expected_default in arg_text, (
        f"{field_name} ORM server_default text={arg_text!r}, "
        f"应包含 expected {expected_default!r}"
    )


def test_uservoice_orm_temp_expires_pending_index_is_partial() -> None:
    """ORM 端 ``idx_user_voices_temp_expires_pending`` 必须是 partial，
    与 migration 完全对齐。
    """
    from models import UserVoice  # type: ignore[import-not-found]

    target_idx_name = "idx_user_voices_temp_expires_pending"
    indices = [
        ix for ix in UserVoice.__table__.indexes
        if ix.name == target_idx_name
    ]
    assert indices, f"UserVoice 缺 Index {target_idx_name}"

    idx = indices[0]
    pg_opts = idx.dialect_options.get("postgresql") or {}
    where_clause = pg_opts.get("where")
    assert where_clause is not None, (
        f"{target_idx_name} ORM 端必须是 partial（postgresql_where=...），"
        "与 migration 一致"
    )
    where_text = str(where_clause)
    assert "is_temporary" in where_text and "expired_at" in where_text, (
        f"partial WHERE 应同时含 is_temporary 与 expired_at；实际: {where_text!r}"
    )


# ----------------------------------------------------------------------
# 字段命名守卫 — Codex 2026-05-26 v4-followup review 三条
# ----------------------------------------------------------------------


def test_guard_no_alembic_migration_adds_bare_expires_at_to_user_voices() -> None:
    """**字段命名守卫 #1**：禁止任何 alembic migration 给 ``user_voices`` 加
    裸 ``expires_at`` 列。

    历史教训：Phase 4.2 v4 草案差点把 ``expires_at`` 加到 user_voices，
    与同表现有 ``expired_at``（软删时间戳）字面冲突。v4-followup §12.3
    改为 ``temporary_expires_at``。本守卫确保后续不会有人改回去。
    """
    versions_dir = REPO_ROOT / "gateway" / "alembic" / "versions"
    offenders: list[str] = []

    for mig_path in sorted(versions_dir.glob("*.py")):
        try:
            tree = ast.parse(mig_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_column"):
                continue
            if len(node.args) < 2:
                continue
            tbl_node = node.args[0]
            if not (isinstance(tbl_node, ast.Constant)
                    and tbl_node.value == "user_voices"):
                continue
            col_node = node.args[1]
            if not (isinstance(col_node, ast.Call)
                    and isinstance(col_node.func, ast.Attribute)
                    and col_node.func.attr == "Column"):
                continue
            if not col_node.args:
                continue
            name_node = col_node.args[0]
            if (isinstance(name_node, ast.Constant)
                    and name_node.value == "expires_at"):
                offenders.append(f"{mig_path.name}:{node.lineno}")

    assert not offenders, (
        f"禁止给 user_voices 加裸 `expires_at` 列（与现有 `expired_at` "
        f"软删时间戳冲突）。违规位置: {offenders}. "
        f"应使用 `temporary_expires_at` （plan v4-followup §12.3）"
    )


def test_guard_uservoice_orm_does_not_declare_bare_expires_at() -> None:
    """**字段命名守卫 #2**：``UserVoice`` ORM 类禁止声明裸 ``expires_at``
    属性。

    扫 ``gateway/models.py``，找 ``class UserVoice(Base):`` 块内的
    ``mapped_column`` 赋值，断言无 ``expires_at = mapped_column(...)``。
    （``temporary_expires_at`` 是允许的。）
    """
    models_path = REPO_ROOT / "gateway" / "models.py"
    tree = ast.parse(models_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "UserVoice"):
            continue
        for stmt in node.body:
            # 形态 1：``expires_at: Mapped[...] = mapped_column(...)``
            if isinstance(stmt, ast.AnnAssign):
                target = stmt.target
                if isinstance(target, ast.Name) and target.id == "expires_at":
                    offenders.append(f"UserVoice.expires_at (line {stmt.lineno})")
            # 形态 2：``expires_at = mapped_column(...)``
            elif isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "expires_at":
                        offenders.append(f"UserVoice.expires_at (line {stmt.lineno})")

    assert not offenders, (
        f"UserVoice ORM 禁止声明裸 `expires_at` 字段（与现有 `expired_at` "
        f"软删时间戳冲突）。违规: {offenders}. "
        f"应使用 `temporary_expires_at`（plan v4-followup §12.3）"
    )


def test_guard_active_row_queries_use_expired_at_not_temporary_expires_at() -> None:
    """**字段命名守卫 #3**：gateway/ 目录里所有 ``user_voices`` 的 active
    判断必须用 ``expired_at IS NULL`` （或 SQLAlchemy 等价表达），**禁止**
    用 ``temporary_expires_at`` 做 active 判断。

    扫 gateway/ 所有 .py 文件，找：
    - 字符串字面量 ``temporary_expires_at IS NULL`` 或
      ``temporary_expires_at IS NOT NULL``（裸字符串 SQL）→ red
    - ``UserVoice.temporary_expires_at.is_(None)`` 或 ``.isnot(None)``
      （SQLAlchemy 表达式）→ red

    plan v4-followup §12.3 强制 ``expired_at IS NULL`` 是唯一 active 判据；
    ``temporary_expires_at`` 只能作为清理 sweeper 入选条件（``< now()``）
    和 UI 剩余时间展示（``> now()`` 兜底过滤），不得用于 active。

    例外：本测试文件本身、partial index 的 WHERE 子句（``is_temporary = TRUE
    AND expired_at IS NULL`` 整体），以及 docstring / 注释 不算 active 判据。
    """
    gateway_root = REPO_ROOT / "gateway"

    bad_string_patterns = [
        "temporary_expires_at IS NULL",
        "temporary_expires_at IS NOT NULL",
        "temporary_expires_at is null",
        "temporary_expires_at is not null",
    ]
    # SQLAlchemy 形态：obj.temporary_expires_at.is_(None) / .is_not(None) / .isnot(None)
    bad_sqla_attrs = (".is_(None)", ".isnot(None)", ".is_not(None)")

    offenders: list[str] = []

    for py_path in gateway_root.rglob("*.py"):
        if "alembic/versions" in str(py_path).replace("\\", "/"):
            # alembic migrations 自己加列时会用 partial WHERE，不算 active 判据
            continue
        try:
            src = py_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # 字符串字面量
        for pat in bad_string_patterns:
            if pat in src:
                # 排除：本 partial index 的合法 WHERE
                # ``is_temporary = TRUE AND expired_at IS NULL``
                # 这是 sweeper 入选条件，不是 active 判据，但字符串
                # 不含 ``temporary_expires_at IS NULL``。所以裸串 hit 即违规。
                idx = src.find(pat)
                line = src.count("\n", 0, idx) + 1
                offenders.append(
                    f"{py_path.relative_to(REPO_ROOT)}:{line} 含 {pat!r}"
                )

        # SQLAlchemy 表达式
        for sqla_attr in bad_sqla_attrs:
            needle = f"temporary_expires_at{sqla_attr}"
            if needle in src:
                idx = src.find(needle)
                line = src.count("\n", 0, idx) + 1
                offenders.append(
                    f"{py_path.relative_to(REPO_ROOT)}:{line} 含 {needle!r}"
                )

    assert not offenders, (
        f"`temporary_expires_at` 禁止用于 active 判断。active 判据是 "
        f"`expired_at IS NULL`（plan v4-followup §12.3）。\n"
        f"违规位置:\n  " + "\n  ".join(offenders)
    )

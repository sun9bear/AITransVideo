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


def _extract_column_name_from_ast(col_node: ast.Call) -> str | None:
    """从 ``sa.Column(...)`` AST 节点提取列名，覆盖 4 种形态：

    1. ``Column("expires_at", ...)``      —— 位置参数
    2. ``Column(name="expires_at", ...)`` —— kwarg ``name=``（Codex P2 #2 补）

    返回 column 名字符串，或 None（提取不出来）。
    """
    if not (isinstance(col_node, ast.Call)
            and isinstance(col_node.func, ast.Attribute)
            and col_node.func.attr == "Column"):
        return None

    # 形态 1：位置参数
    if col_node.args:
        first = col_node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value

    # 形态 2：``name=`` kwarg（Codex 2026-05-26 P2 #2）
    for kw in col_node.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) \
                and isinstance(kw.value.value, str):
            return kw.value.value

    return None


def test_guard_no_alembic_migration_adds_bare_expires_at_to_user_voices() -> None:
    """**字段命名守卫 #1**：禁止任何 alembic migration 给 ``user_voices`` 加
    裸 ``expires_at`` 列（含 positional 与 ``name=`` kwarg 两种形态）。

    历史教训：Phase 4.2 v4 草案差点把 ``expires_at`` 加到 user_voices，
    与同表现有 ``expired_at``（软删时间戳）字面冲突。v4-followup §12.3
    改为 ``temporary_expires_at``。本守卫确保后续不会有人改回去。

    Codex 2026-05-26 PR #9 review P2 #2 补：原版本只抓 ``Column("expires_at",
    ...)``（位置参数），漏抓 ``Column(name="expires_at", ...)`` kwarg 形态。
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
            col_name = _extract_column_name_from_ast(col_node)
            if col_name == "expires_at":
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


# ----------------------------------------------------------------------
# G3 AST 辅助函数 — Codex 2026-05-26 PR #9 review P2 #1 + #2 重写
# ----------------------------------------------------------------------

# 节点判定函数 ----------------------------------------------------------


def _is_temp_expires_attr(node: ast.AST) -> bool:
    """判断 AST 节点是否是 ``<anything>.temporary_expires_at`` 属性访问。

    覆盖 ``UserVoice.temporary_expires_at`` / ``self.temporary_expires_at``
    / 多级链 ``UserVoice.something.temporary_expires_at`` 等所有 Attribute
    形态。
    """
    return isinstance(node, ast.Attribute) and node.attr == "temporary_expires_at"


def _is_none_or_null_arg(node: ast.AST) -> bool:
    """判断 AST 节点是否表达 "NULL" 语义。涵盖：

    - ``None`` 字面量
    - ``null()``（裸函数调用 —— sqlalchemy.null）
    - ``sa.null()`` / ``sqlalchemy.null()``（带 module 前缀的函数调用）
    """
    # ``None`` 字面量
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    # 函数调用：``null()`` / ``sa.null()`` / ``sqlalchemy.null()``
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "null":
            return True
        if isinstance(f, ast.Attribute) and f.attr == "null":
            return True
    return False


# G3 主测试 ------------------------------------------------------------


def test_guard_active_row_queries_use_expired_at_not_temporary_expires_at() -> None:
    """**字段命名守卫 #3**：gateway/ 目录里所有 ``user_voices`` 的 active
    判断必须用 ``expired_at IS NULL``（或 SQLAlchemy 等价表达），**禁止**
    用 ``temporary_expires_at`` 做 active 判断。

    plan v4-followup §12.3 强制 ``expired_at IS NULL`` 是唯一 active 判据；
    ``temporary_expires_at`` 只能作为清理 sweeper 入选条件（``< now()``）
    和 UI 剩余时间展示（``> now()`` 兜底过滤），不得用于 active。

    **Codex 2026-05-26 PR #9 review P2 #1 重写**：
    原版本用 raw text 扫整个文件，会被 **docstring / 注释 / 字符串字面量**
    误报（GitHub Codex 已自动捕获这条 finding）。本版本改为 **AST 扫描**
    Compare 节点和 Call 节点，docstring / 注释天然不会出现在这两种 AST
    节点里，从源头消除误报。

    覆盖以下所有 SQLAlchemy 写法（Codex 2026-05-26 P2 #1 列出 + 通配）：

    Compare 形态（``ast.Compare``）：
    - ``UserVoice.temporary_expires_at == None``
    - ``UserVoice.temporary_expires_at != None``
    - ``UserVoice.temporary_expires_at == sa.null()``
    - ``UserVoice.temporary_expires_at != null()``

    Call 形态（``ast.Call``，方法 ``is_`` / ``is_not`` / ``isnot``）：
    - ``UserVoice.temporary_expires_at.is_(None)``
    - ``UserVoice.temporary_expires_at.is_(null())``
    - ``UserVoice.temporary_expires_at.is_(sa.null())``
    - ``UserVoice.temporary_expires_at.is_not(None)``
    - ``UserVoice.temporary_expires_at.is_not(null())``
    - ``UserVoice.temporary_expires_at.isnot(None)``  # 旧 SA 写法
    - ``UserVoice.temporary_expires_at.isnot(sa.null())``

    Raw SQL 形态（``text()`` 调用中的字符串字面量）：
    - ``text("temporary_expires_at IS NULL")``
    - ``text("... temporary_expires_at IS NOT NULL ...")``

    **排除**：alembic migrations（自身定义 partial WHERE 不算 active 判据）+
    本测试文件（声明被禁止模式作为反例字符串）。
    """
    gateway_root = REPO_ROOT / "gateway"
    self_path = Path(__file__).resolve()
    offenders: list[str] = []

    for py_path in gateway_root.rglob("*.py"):
        norm = str(py_path).replace("\\", "/")
        # 排除 alembic migrations：partial index 的 WHERE 不算 active 判据
        if "alembic/versions" in norm:
            continue
        # 排除本测试文件：例外可省略（test 在 tests/ 不在 gateway/）
        if py_path.resolve() == self_path:
            continue

        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        rel = py_path.relative_to(REPO_ROOT)

        for node in ast.walk(tree):
            # --- Compare 形态：``temp_expires.X == None`` / `!= None` 等 ---
            if isinstance(node, ast.Compare):
                # Compare.left + Compare.comparators 形成一串：left op0 c0 op1 c1 ...
                # 同时检查 left 和每个 comparator 是否对 temporary_expires_at
                # 与 None/null 做比较
                operands = [node.left] + list(node.comparators)
                ops = node.ops
                # 遍历相邻配对
                for i, op in enumerate(ops):
                    if not isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                        continue
                    left_node = operands[i]
                    right_node = operands[i + 1]
                    # 两侧任一是 temp_expires，另一侧是 None/null → 违规
                    if (
                        (_is_temp_expires_attr(left_node)
                         and _is_none_or_null_arg(right_node))
                        or
                        (_is_temp_expires_attr(right_node)
                         and _is_none_or_null_arg(left_node))
                    ):
                        offenders.append(
                            f"{rel}:{node.lineno} Compare 形态 "
                            f"(temporary_expires_at == / != / is / is not None|null)"
                        )

            # --- Call 形态：``temp_expires.is_(...)`` / .is_not / .isnot ---
            elif isinstance(node, ast.Call):
                f = node.func
                # f 必须是 ``<expr>.is_`` / ``.is_not`` / ``.isnot``
                if not (isinstance(f, ast.Attribute)
                        and f.attr in ("is_", "is_not", "isnot")):
                    # ---- raw SQL via text("...") ----
                    # text("temporary_expires_at IS [NOT] NULL ...")
                    is_text_call = (
                        (isinstance(f, ast.Name) and f.id == "text")
                        or (isinstance(f, ast.Attribute) and f.attr == "text")
                    )
                    if is_text_call and node.args:
                        first = node.args[0]
                        if (isinstance(first, ast.Constant)
                                and isinstance(first.value, str)):
                            up = first.value.upper()
                            if ("TEMPORARY_EXPIRES_AT" in up
                                    and (" IS NULL" in up or " IS NOT NULL" in up)):
                                offenders.append(
                                    f"{rel}:{node.lineno} text() raw SQL "
                                    f"(temporary_expires_at IS [NOT] NULL)"
                                )
                    continue

                # f.value 是被调用方法的接收者；必须指向 temp_expires
                if not _is_temp_expires_attr(f.value):
                    continue

                # 参数必须是 None / null() / sa.null() — 否则不算 active 判断
                if not node.args:
                    continue
                arg = node.args[0]
                if _is_none_or_null_arg(arg):
                    offenders.append(
                        f"{rel}:{node.lineno} Call 形态 "
                        f"(temporary_expires_at.{f.attr}(None|null))"
                    )

    assert not offenders, (
        f"`temporary_expires_at` 禁止用于 active 判断。active 判据是 "
        f"`expired_at IS NULL`（plan v4-followup §12.3）。\n"
        f"违规位置:\n  " + "\n  ".join(offenders)
    )


# ----------------------------------------------------------------------
# 负向验证：G3 AST 守卫必须真能抓住 Codex 2026-05-26 PR #9 列出的
# 全部攻击向量。把已知违规 source 灌进 AST，跑 helper，断言 hit。
# ----------------------------------------------------------------------


def _g3_scan_source(src: str) -> list[str]:
    """对内嵌 Python 源码跑与 G3 主守卫相同的 AST 扫描，返回违规节点描述。
    用于参数化负向测试 —— 验证 G3 真能抓到 Codex 列出的每个模式。
    """
    tree = ast.parse(src)
    hits: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            for i, op in enumerate(node.ops):
                if not isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                    continue
                left_node = operands[i]
                right_node = operands[i + 1]
                if (
                    (_is_temp_expires_attr(left_node)
                     and _is_none_or_null_arg(right_node))
                    or
                    (_is_temp_expires_attr(right_node)
                     and _is_none_or_null_arg(left_node))
                ):
                    hits.append(f"Compare:line{node.lineno}")
        elif isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute)
                    and f.attr in ("is_", "is_not", "isnot")
                    and _is_temp_expires_attr(f.value)
                    and node.args
                    and _is_none_or_null_arg(node.args[0])):
                hits.append(f"Call.{f.attr}:line{node.lineno}")
                continue
            # text("...") raw SQL
            is_text_call = (
                (isinstance(f, ast.Name) and f.id == "text")
                or (isinstance(f, ast.Attribute) and f.attr == "text")
            )
            if is_text_call and node.args:
                first = node.args[0]
                if (isinstance(first, ast.Constant)
                        and isinstance(first.value, str)):
                    up = first.value.upper()
                    if ("TEMPORARY_EXPIRES_AT" in up
                            and (" IS NULL" in up or " IS NOT NULL" in up)):
                        hits.append(f"text():line{node.lineno}")
    return hits


# 所有 Codex 2026-05-26 PR #9 review P2 #1 显式列出的攻击向量 +
# 通配补充。每条都包成最小可解析 Python 模块。
_G3_BAD_PATTERNS_THAT_MUST_BE_DETECTED: list[tuple[str, str]] = [
    # --- Compare 形态 ---
    ("compare_eq_none",
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at == None)\n"),
    ("compare_neq_none",
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at != None)\n"),
    ("compare_eq_sa_null",
     "import sqlalchemy as sa\nfrom models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at == sa.null())\n"),
    ("compare_neq_bare_null",
     "from sqlalchemy import null\nfrom models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at != null())\n"),
    # --- Call .is_ / .is_not / .isnot 形态 ---
    ("call_is_none",
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at.is_(None))\n"),
    ("call_is_bare_null",
     "from sqlalchemy import null\nfrom models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at.is_(null()))\n"),
    ("call_is_sa_null",
     "import sqlalchemy as sa\nfrom models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at.is_(sa.null()))\n"),
    ("call_is_not_none",
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at.is_not(None))\n"),
    ("call_is_not_null",
     "from sqlalchemy import null\nfrom models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at.is_not(null()))\n"),
    ("call_isnot_none_legacy",
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at.isnot(None))\n"),
    ("call_isnot_sa_null_legacy",
     "import sqlalchemy as sa\nfrom models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at.isnot(sa.null()))\n"),
    # --- Raw SQL via text() ---
    ("text_is_null",
     "from sqlalchemy import text\n"
     "q = session.execute(text('SELECT 1 FROM user_voices WHERE "
     "temporary_expires_at IS NULL'))\n"),
    ("text_is_not_null",
     "from sqlalchemy import text\n"
     "q = session.execute(text('temporary_expires_at IS NOT NULL'))\n"),
    ("sa_text_is_null",
     "import sqlalchemy as sa\n"
     "q = session.execute(sa.text('temporary_expires_at IS NULL'))\n"),
    # --- self.temporary_expires_at 形态（receiver 不是 UserVoice 而是 self）---
    ("self_compare_eq_none",
     "class Foo:\n"
     "    def is_active(self):\n"
     "        return self.temporary_expires_at == None\n"),
]


@pytest.mark.parametrize(
    "case_name, src", _G3_BAD_PATTERNS_THAT_MUST_BE_DETECTED,
    ids=[c[0] for c in _G3_BAD_PATTERNS_THAT_MUST_BE_DETECTED],
)
def test_g3_negative_detects_all_codex_listed_offending_patterns(
    case_name: str, src: str,
) -> None:
    """**G3 负向验证**：保证 AST 守卫真能抓 Codex 2026-05-26 PR #9 review
    P2 #1 列出的全部攻击向量。

    每个 case 是一段最小可解析 Python 源，模拟"未来某人在 gateway/ 里写
    了 active 查询用 temporary_expires_at"的违规。``_g3_scan_source`` 用与
    G3 主测试相同的 AST 逻辑扫描，必须命中。
    """
    hits = _g3_scan_source(src)
    assert hits, (
        f"G3 守卫 AST 扫描未抓到 case={case_name!r}；该攻击向量会绕过 CI。"
        f"\n源码:\n{src}"
    )


# ----------------------------------------------------------------------
# 负向验证 #2：G3 不能误报合法 use case。
# ----------------------------------------------------------------------


_G3_GOOD_PATTERNS_THAT_MUST_NOT_BE_DETECTED: list[tuple[str, str]] = [
    # 允许：作 sweeper 入选条件（``< now()``）
    ("sweeper_lt_now",
     "from datetime import datetime, timezone\n"
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at < datetime.now(timezone.utc))\n"),
    # 允许：作 UI 兜底过滤（``> now()``）
    ("ui_gt_now",
     "from datetime import datetime, timezone\n"
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter("
     "UserVoice.temporary_expires_at > datetime.now(timezone.utc))\n"),
    # 允许：``expired_at IS NULL``（正确的 active 判据）
    ("expired_at_is_null_compare",
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter(UserVoice.expired_at == None)\n"),
    ("expired_at_is_null_call",
     "from models import UserVoice\n"
     "q = session.query(UserVoice).filter(UserVoice.expired_at.is_(None))\n"),
    # 允许：text() raw SQL 但只引用 ``expired_at``，不引用 temporary_expires_at
    ("text_only_expired_at",
     "from sqlalchemy import text\n"
     "q = session.execute(text('expired_at IS NULL'))\n"),
    # 允许：docstring / 注释 提到 ``temporary_expires_at IS NULL``（**关键**
    # —— 这正是原 raw-text 版本的误报场景，AST 不应抓）
    ("docstring_mentions_pattern",
     "'''Notes: never use temporary_expires_at IS NULL as active check.'''\n"
     "x = 1\n"),
    # 允许：字符串字面量但不在 text() 中
    ("plain_string_literal",
     "msg = 'temporary_expires_at IS NULL is forbidden in active queries'\n"),
    # 允许：UPDATE 写 temporary_expires_at（sweeper 续命场景，§12.5）
    ("update_assign_value",
     "from models import UserVoice\n"
     "from datetime import datetime, timezone, timedelta\n"
     "voice.temporary_expires_at = datetime.now(timezone.utc) + "
     "timedelta(days=7)\n"),
]


@pytest.mark.parametrize(
    "case_name, src", _G3_GOOD_PATTERNS_THAT_MUST_NOT_BE_DETECTED,
    ids=[c[0] for c in _G3_GOOD_PATTERNS_THAT_MUST_NOT_BE_DETECTED],
)
def test_g3_does_not_false_positive_on_legitimate_uses(
    case_name: str, src: str,
) -> None:
    """**G3 误报防护**：合法用法必须不被守卫抓。

    覆盖 plan §12 / §12.4 / §12.5 允许的 temporary_expires_at 用法：
    sweeper ``< now()``、UI ``> now()`` 兜底、UPDATE 续命、注释 / docstring
    引用、不在 text() 上下文的字符串等。
    """
    hits = _g3_scan_source(src)
    assert not hits, (
        f"G3 守卫误报合法用法 case={case_name!r}；hits={hits}"
        f"\n源码:\n{src}"
    )

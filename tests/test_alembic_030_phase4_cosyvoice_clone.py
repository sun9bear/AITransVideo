"""Phase 4.1 migration 030 与 UserVoice ORM 一致性守卫。

migration 030 给 ``user_voices`` 加 9 个新字段；``UserVoice`` ORM 必须
**严格对齐**：

- 字段名、SQL 类型、nullable / default 三者必须一致
- 缺一个字段 → ORM 与 DB schema 漂移 → 下次 alembic autogenerate 会
  误提议 ``DROP COLUMN``
- 多一个字段 → ORM 看到的字段在 DB 不存在 → 运行时 SELECT 报错

设计：本测试**不连真实 DB**，AST 解析 migration 文件 + ORM 反射两端
拿到字段列表后比对。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "gateway" / "alembic" / "versions"
    / "030_phase4_cosyvoice_clone_voice_metadata.py"
)


# Phase 4.1 plan 明确的 9 个字段 — 任何漂移立刻红。
# ``server_default`` 字段：仅在旧 row 必须兜底的 NOT NULL 列上设；
# 其它字段保持 None（migration 不传 ``server_default``）。
_EXPECTED_FIELDS: dict[str, dict] = {
    "region_constraint": {
        "sql_type": "String(length=20)", "nullable": False,
        "server_default": "overseas_ok",  # 旧 row 兜底
    },
    "requires_worker": {
        "sql_type": "Boolean()", "nullable": False,
        "server_default": "false",  # 旧 row 兜底
    },
    "target_model":      {"sql_type": "String(length=50)", "nullable": True, "server_default": None},
    "worker_provider":   {"sql_type": "String(length=30)", "nullable": True, "server_default": None},
    "worker_region":     {"sql_type": "String(length=30)", "nullable": True, "server_default": None},
    "clone_api_model":   {"sql_type": "String(length=50)", "nullable": True, "server_default": None},
    "billing_sku":       {"sql_type": "String(length=100)","nullable": True, "server_default": None},
    "clone_provider_request_id": {"sql_type": "String(length=64)", "nullable": True, "server_default": None},
    "clone_worker_request_id":   {"sql_type": "String(length=64)", "nullable": True, "server_default": None},
}


def _parse_migration_add_columns() -> dict[str, dict]:
    """AST 扫 migration 030 的 ``op.add_column("user_voices", sa.Column(...))``
    调用，返回 ``{col_name: {sql_type, nullable, server_default}}``。

    ``server_default`` 提取规则：
    - 字符串字面量（``"overseas_ok"``）→ 直接取值
    - ``sa.false()`` / ``sa.true()`` → 规范化为 ``"false"`` / ``"true"``
    - ``sa.text("false")`` / ``text("false")`` → 取参数文本
    - 缺省 / 其它 → None
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
    # 直接字符串："overseas_ok"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # 函数调用形态
    if isinstance(node, ast.Call):
        # sa.false() / sa.true()
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("false", "true"):
            return node.func.attr
        # sa.text("...") / text("...")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "text":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
        if isinstance(node.func, ast.Name) and node.func.id == "text":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
    return None


def test_migration_030_adds_all_9_phase4_fields() -> None:
    """Migration 030 必须 add 全部 9 个 Phase 4.1 字段。"""
    actual = _parse_migration_add_columns()
    missing = set(_EXPECTED_FIELDS.keys()) - set(actual.keys())
    assert not missing, (
        f"Migration 030 缺少 Phase 4.1 字段: {missing}; 现有: {list(actual.keys())}"
    )


def test_migration_030_does_not_add_extra_phase4_fields() -> None:
    """Migration 030 不能多加意外字段（防漂移）。"""
    actual = _parse_migration_add_columns()
    extra = set(actual.keys()) - set(_EXPECTED_FIELDS.keys())
    assert not extra, (
        f"Migration 030 多了 plan 未列字段: {extra}; 改前先更新 _EXPECTED_FIELDS"
    )


@pytest.mark.parametrize("field_name, expected", list(_EXPECTED_FIELDS.items()))
def test_migration_030_field_type_and_nullable_locked(field_name: str, expected: dict) -> None:
    """每个字段的 SQL 类型 + nullable 锁死，防 ALTER 改类型。"""
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
def test_migration_030_field_server_default_locked(field_name: str, expected: dict) -> None:
    """**关键**：``region_constraint`` / ``requires_worker`` 必须有 server_default
    才能让旧 row（MiniMax / VolcEngine）兜底。其它字段保持 None。

    Codex 2026-05-25 三轮 finding：测试注释提了 default 一致性，但实际没测；
    本测试补这道守卫，避免有人未来误删 server_default 导致旧 row 在
    ALTER NOT NULL 时报错。
    """
    actual = _parse_migration_add_columns()
    actual_default = actual[field_name]["server_default"]
    expected_default = expected["server_default"]
    assert actual_default == expected_default, (
        f"{field_name} server_default 不符：expected {expected_default!r}, "
        f"actual {actual_default!r}"
    )


def test_uservoice_orm_has_all_phase4_fields() -> None:
    """``UserVoice`` ORM 必须有 migration 030 加的所有字段。

    导入 ORM 类后用 ``__table__.columns`` 反射字段集合。
    """
    # gateway/ 已经在 conftest.py sys.path，可以直接 import
    from models import UserVoice  # type: ignore[import-not-found]

    orm_columns = {c.name for c in UserVoice.__table__.columns}
    missing = set(_EXPECTED_FIELDS.keys()) - orm_columns
    assert not missing, (
        f"UserVoice ORM 缺 Phase 4.1 字段 {missing}；"
        f"migration 跑完后 SQLAlchemy 不感知这些字段，下次 autogenerate 会"
        f"误提议 DROP COLUMN"
    )


@pytest.mark.parametrize("field_name, expected", list(_EXPECTED_FIELDS.items()))
def test_uservoice_orm_field_nullable_matches_migration(field_name: str, expected: dict) -> None:
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
    """ORM ``server_default`` 与 migration 一致。

    SQLAlchemy 把 server_default 包装成 ``DefaultClause``；通过
    ``column.server_default.arg`` 拿到原始字符串 / TextClause。
    """
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
    # DefaultClause.arg 可能是 str 或 TextClause；都转字符串比对
    arg = col.server_default.arg
    arg_text = str(arg) if not isinstance(arg, str) else arg
    assert expected_default in arg_text, (
        f"{field_name} ORM server_default text={arg_text!r}, "
        f"应包含 expected {expected_default!r}"
    )


def test_migration_030_revises_029() -> None:
    """030 的 down_revision 必须是 029_pan_backup，防止 head 链断。"""
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "029_pan_backup"' in src, (
        "030 down_revision 必须是 029_pan_backup"
    )


def test_migration_030_has_downgrade_for_all_added_columns() -> None:
    """downgrade() 必须 drop 所有 upgrade() 加的字段，对称性守卫。"""
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    for col in _EXPECTED_FIELDS.keys():
        assert f'op.drop_column("user_voices", "{col}")' in src, (
            f"downgrade() 缺 drop_column({col!r})，与 upgrade 不对称"
        )


def test_migration_030_clone_provider_request_id_index_is_partial() -> None:
    """Codex 2026-05-25 三轮 finding：``clone_provider_request_id`` 永久
    nullable，大多数旧 row 是 NULL；索引必须是 partial，跳过 NULL 行。

    AST 扫 migration 中 ``create_index("idx_user_voices_clone_provider_request_id", ...)``
    调用，断言带 ``postgresql_where=...`` kw。
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
                and name_node.value == "idx_user_voices_clone_provider_request_id"):
            continue
        found = True
        kw_names = {kw.arg for kw in node.keywords}
        assert "postgresql_where" in kw_names, (
            "idx_user_voices_clone_provider_request_id 必须是 partial index "
            "（postgresql_where=...），否则会把所有 NULL 行也纳入索引浪费空间"
        )

    assert found, "找不到 create_index('idx_user_voices_clone_provider_request_id', ...)"


def test_uservoice_orm_clone_provider_request_id_index_is_partial() -> None:
    """ORM 端的同名 Index 也必须是 partial（与 migration 一致）。"""
    from models import UserVoice  # type: ignore[import-not-found]

    target_idx_name = "idx_user_voices_clone_provider_request_id"
    indices = [
        ix for ix in UserVoice.__table__.indexes
        if ix.name == target_idx_name
    ]
    assert indices, f"UserVoice 缺 Index {target_idx_name}"

    idx = indices[0]
    # SQLAlchemy 把 ``postgresql_where`` 存进 ``idx.dialect_options['postgresql']['where']``
    pg_opts = idx.dialect_options.get("postgresql") or {}
    where_clause = pg_opts.get("where")
    assert where_clause is not None, (
        f"{target_idx_name} ORM 端必须是 partial（postgresql_where=...），"
        "与 migration 一致"
    )
    assert "clone_provider_request_id" in str(where_clause), (
        f"partial WHERE 表达式应包含 clone_provider_request_id，实际：{where_clause!r}"
    )

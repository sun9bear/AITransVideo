"""P3c — smart 克隆 reservation 终态 finalizer 接线守卫（source-scan）.

plan v3 §4：``job_terminal_mirror`` 是 smart 预览克隆 600 点结算的**单一入口**。
本守卫用 source-scan（不 import gateway 模块，避 database-stub 污染，见 memory
feedback_test_database_stub_convention）钉死接线契约：

1. 终态块调用 ``settle_smart_clone_reservations_for_task`` 这个 by-task 入口；
2. 真正结算跑在**独立 ``async_session()``**（不复用 mirror 的批量 caller-commit
   session——其 already_settled 分支 rollback 会丢同批其他 job 的 mirror）；
3. marker-gated（``_smart_clone_settle_needed``）避免无克隆 job 每轮 poll 查 DB；
4. 在 ``is_anonymous_preview`` 早返回分支**之前**调用（覆盖匿名标记跳过分钟
   结算的预览路径——克隆点结算不能随分钟结算一起被跳过）；
5. 结算 helper 整体包 try/except（结算故障绝不阻断状态镜像）。
"""
from __future__ import annotations

import ast
from pathlib import Path

_MIRROR = Path(__file__).resolve().parents[1] / "gateway" / "job_terminal_mirror.py"


def _read() -> str:
    return _MIRROR.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    src = _read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def test_finalizer_helpers_defined():
    src = _read()
    assert "def _smart_clone_settle_needed(" in src
    assert "async def _settle_smart_clone_reservations_post_terminal(" in src


def test_finalizer_called_in_terminal_block_before_anon_branch():
    """终态块在 anon 早返回分支**之前**调结算 helper（覆盖两路）。"""
    body = _func_src("mirror_job_terminal_state")
    assert body, "mirror_job_terminal_state 未找到"
    call = "await _settle_smart_clone_reservations_post_terminal(db_job)"
    assert call in body
    # 必须在 TERMINAL_STATUSES 判定之后、is_anonymous_preview 早返回之前
    i_terminal = body.index("if upstream_status in TERMINAL_STATUSES:")
    i_call = body.index(call)
    i_anon = body.index('getattr(db_job, "is_anonymous_preview"')
    assert i_terminal < i_call < i_anon, "结算 helper 必须在终态块内、anon 分支之前"


def test_finalizer_uses_dedicated_async_session():
    """结算跑在独立 async_session，不复用 mirror 批量 caller-commit 的 db。"""
    body = _func_src("_settle_smart_clone_reservations_post_terminal")
    assert body, "结算 helper 未找到"
    assert "from database import async_session" in body
    assert "async with async_session() as settle_db:" in body
    assert "settle_smart_clone_reservations_for_task(" in body
    assert "settle_db" in body


def test_finalizer_is_marker_gated():
    """结算 helper 先过 marker gate 再开 session（无克隆 job 不查 DB）。"""
    body = _func_src("_settle_smart_clone_reservations_post_terminal")
    assert "if not _smart_clone_settle_needed(db_job):" in body
    # gate 在开 session 之前（短路）。用 "async with async_session()" 这条语句
    # 比位置，避免命中 docstring 里反引号包的 ``async_session()`` 提及。
    assert body.index("if not _smart_clone_settle_needed(db_job):") < body.index("async with async_session()")


def test_finalizer_failure_never_blocks_mirror():
    """结算故障只 WARNING，不抛——绝不阻断状态镜像（单一入口教训）。"""
    body = _func_src("_settle_smart_clone_reservations_post_terminal")
    assert "except Exception" in body
    assert "logger.warning(" in body
    # 真正动钱的 settle 调用在 try 块内（异常被吞）
    i_try = body.index("try:")
    i_settle = body.index("settle_smart_clone_reservations_for_task(")
    i_except = body.index("except Exception")
    assert i_try < i_settle < i_except


def test_marker_gate_reads_smart_state_keys():
    """marker gate 读 smart_state 的 reservation_id / credit_reserved 键。"""
    src = _read()
    assert '"smart_clone_reservation_id"' in src
    assert '"smart_clone_credit_reserved"' in src
    body = _func_src("_smart_clone_settle_needed")
    assert 'getattr(db_job, "smart_state"' in body

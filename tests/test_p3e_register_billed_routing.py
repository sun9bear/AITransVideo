"""P3e §5 — register-billed 路由 + 字段 parity source-scan 守卫.

plan v3 §5。pipeline 克隆成功点：reservation_id(+task_id) 在场 → POST 到
register-billed endpoint（原子写 billing event + 入库，钱-正确性核心）；否则
register-smart（既有，只入库不计费）。两端点 rich source_* 字段 parity（否则
preview 克隆音色缺 source_content_hash 等 → 影响后续强匹配/复用）。

source-scan（不 import gateway 模块避 database-stub 污染，见 memory
feedback_test_database_stub_convention）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PROCESS_PY = _ROOT / "src" / "pipeline" / "process.py"
_API_PY = _ROOT / "gateway" / "user_voice_api.py"
_SVC_PY = _ROOT / "gateway" / "smart_clone_reservation_service.py"


def _func_src(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


# ---------------------------------------------------------------------------
# pipeline 路由：reservation 在场 → register-billed
# ---------------------------------------------------------------------------


def test_register_helper_branches_to_billed_when_reservation():
    """_register_smart_clone_in_user_voices 在 reservation_id+task_id 在场时
    POST register-billed，否则 register-smart。"""
    body = _func_src(_PROCESS_PY, "_register_smart_clone_in_user_voices")
    assert body, "_register_smart_clone_in_user_voices 未找到"
    assert "reservation_id: str | None = None" in body
    assert "task_id: str | None = None" in body
    assert "/api/internal/smart-clone/register-billed" in body
    assert "/api/internal/user-voices/register-smart" in body
    # 分支条件：两者都在场才走 billed
    flat = " ".join(body.split())
    assert "_is_billed = bool(_reservation_id and _task_id)" in flat
    assert "if _is_billed:" in flat


def test_register_billed_payload_carries_reservation_and_task():
    """走 register-billed 时 payload 必须带 reservation_id + task_id（供
    gateway 行锁校验 reservation status=reserved 且属本 task）。"""
    body = _func_src(_PROCESS_PY, "_register_smart_clone_in_user_voices")
    assert 'payload["reservation_id"] = _reservation_id' in body
    assert 'payload["task_id"] = _task_id' in body


def test_mirror_call_site_passes_reservation_and_task():
    """克隆成功 mirror 调用点必须把 _smart_clone_reservation_id + job_id 传进去。"""
    src = _PROCESS_PY.read_text(encoding="utf-8")
    flat = " ".join(src.split())
    assert "reservation_id=_smart_clone_reservation_id," in flat
    # task_id 取 job_id
    assert re.search(r"task_id=\(\s*_smart_job_id_for_mirror", flat)


# ---------------------------------------------------------------------------
# 字段 parity：service + endpoint 透传 rich source_*
# ---------------------------------------------------------------------------

_PARITY_FIELDS = (
    "source_type", "source_ref", "source_content_hash", "source_upload_md5",
    "source_video_title", "source_speaker_name", "source_speaker_name_key",
    "source_published_at", "source_content_summary", "source_content_era",
    "source_content_tags", "clone_sample_seconds", "clone_sample_segment_ids",
    "notes",
)


def test_service_register_billed_accepts_rich_fields():
    """register_smart_clone_with_billing 签名必须接收 rich source_* 字段。"""
    body = _func_src(_SVC_PY, "register_smart_clone_with_billing")
    assert body
    for f in _PARITY_FIELDS:
        assert f in body, f"service 缺字段 parity {f}"
    # source_content_hash 必须真透传给 add_user_voice（关键 match 字段）
    flat = " ".join(body.split())
    assert "source_content_hash=source_content_hash" in flat


def test_endpoint_register_billed_forwards_rich_fields():
    """register-billed endpoint 必须从 body 解析并转发 rich source_* 字段。"""
    body = _func_src(_API_PY, "internal_smart_clone_register_billed")
    assert body
    for f in ("source_type", "source_ref", "source_content_hash", "source_video_title",
              "source_content_summary", "clone_sample_segment_ids"):
        assert f in body, f"endpoint 缺字段 parity {f}"
    # source_published_at 复用既有 datetime 解析器
    assert "_parse_optional_datetime(body.get(\"source_published_at\"))" in body


# ---------------------------------------------------------------------------
# 钱-可见性（对抗性复核 V2）：billing-route 失败 loud log
# ---------------------------------------------------------------------------


def test_register_billed_failure_logs_money_event():
    """对抗性复核 V2：reservation 在场但 register-billed 失败（409 / 异常）或
    task_id 缺失静默降级 register-smart → clone 已发生但未写 billing event →
    finalizer release（业务白克隆）。必须 loud log [smart][MONEY] 供 ops 对账
    （fail-safe 方向：用户不被扣，只业务漏收 + 孤儿 voice）。"""
    body = _func_src(_PROCESS_PY, "_register_smart_clone_in_user_voices")
    # TU-08: [smart][MONEY] loud print → structured logger.error（审计 event 名）。
    assert "smart_register_billed_failed" in body
    assert "smart_billing_inconsistency" in body
    # task_id 缺失但 reservation 在场的不一致也要 log
    assert "_reservation_id and not _task_id" in " ".join(body.split())

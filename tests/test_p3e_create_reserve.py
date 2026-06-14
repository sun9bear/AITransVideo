"""P3e-2b — gateway create 端 smart 预览克隆 reserve 接线守卫（source-scan）.

plan 2026-06-14-p3e2-preview-lane-design.md §4。intercept_create_job 在 forward
前（Option C）用预生成 job_id 调 reserve_smart_clone_credit → 把 job_id +
reservation marker 塞 request_data 一并 forward。reserve 服务逻辑由
test_p3a_smart_clone_reserve 真测；本守卫锁**接线契约**（触发条件 / 预生成
job_id / marker stamp / fail-safe 降级不阻断 / PG Job.smart_state 直写 / reserve
在 forward 前）。

source-scan（不 import gateway 模块避 database-stub 污染，见 memory
feedback_test_database_stub_convention）。
"""
from __future__ import annotations

import ast
from pathlib import Path

_JI = Path(__file__).resolve().parents[1] / "gateway" / "job_intercept.py"


def _func_src(name: str) -> str:
    src = _JI.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _create_src() -> str:
    body = _func_src("intercept_create_job")
    assert body, "intercept_create_job 未找到"
    return body


def test_reserve_gated_on_smart_consent_and_flag():
    """reserve 仅当 service_mode==smart + consent.auto_voice_clone is True +
    admin smart_preview_clone_enabled。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert 'service_mode == "smart"' in flat
    assert 'request_data["smart_consent"].get("auto_voice_clone") is True' in flat
    assert "smart_preview_clone_enabled" in body


def test_reserve_uses_pregenerated_job_id_option_c():
    """Option C：forward 前预生成 job_id（task_id=job_id）调 reserve，
    并把 job_id 塞 request_data 让 Job API 用它。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert '_pre_job_id = f"job_{_uuid.uuid4().hex}"' in flat
    assert "task_id=_pre_job_id" in flat
    assert 'request_data["job_id"] = _pre_job_id' in flat


def test_reserve_stamps_smart_state_marker_into_request_data():
    """reserved → request_data['smart_state'] 写 reservation marker（pipeline
    _snap 读 + mirror→finalizer marker-gate）。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert 'request_data["smart_state"]' in flat
    assert '"smart_clone_reservation_id": _smart_resv.reservation_id' in flat
    assert '"smart_clone_credit_reserved": True' in flat


def test_reserve_amount_600_and_lib_cap():
    """预扣 600 + 库容门用 admin smart_user_voice_clone_cap。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "amount_credits=600" in flat
    assert "smart_user_voice_clone_cap" in body
    assert "library_cap=_smart_lib_cap" in flat


def test_reserve_before_forward():
    """reserve 必须在 forward（proxy_request）**之前**（Option C：marker 须随
    forward 进 JSON store JobRecord）。"""
    body = _create_src()
    i_reserve = body.index("_reserve_smart_clone(")
    i_forward = body.index("upstream_response = await proxy_request(")
    assert i_reserve < i_forward, "reserve 必须在 forward 之前"


def test_degrade_does_not_block_failsafe():
    """🔥 降级一律不阻断（CLAUDE.md 免费触点不静默降级 + fail-safe）：
    disabled/denied/error → 记 _smart_clone_skipped_reason、不 return/raise、
    不写 marker → pipeline 退预设。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert '_smart_clone_skipped_reason = "clone_disabled"' in flat
    assert "_smart_resv.deny_reason or" in flat
    assert '_smart_clone_skipped_reason = "reserve_error"' in flat
    # reserve 故障是 except 吞掉（不阻断），不是 return error
    assert "except Exception" in body
    # skip 分支不得 return/raise（降级继续建任务）
    reserve_block = body[body.index("_smart_clone_skipped_reason: str | None = None"):body.index("upstream_response = await proxy_request(")]
    assert "return _error_response" not in reserve_block, "降级不得阻断建任务"


def test_pg_job_smart_state_set_from_reservation():
    """reserved → PG Job(smart_state=marker) via Job API echo/request data.

    Rebased on the post-PR32 create path, the PG row should keep the full
    smart_state dict already forwarded to the Job API instead of rebuilding a
    narrower marker from a single local reservation variable.
    """
    body = _create_src()
    flat = " ".join(body.split())
    assert "smart_state=(" in flat
    assert 'job_data.get("smart_state")' in flat
    assert 'request_data.get("smart_state")' in flat

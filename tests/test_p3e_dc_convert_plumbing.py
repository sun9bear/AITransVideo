"""P3e D-B / D-C 接线守卫（AST source-scan，不 import gateway 避 database-stub 污染）.

plan 2026-06-15-smart-clone-600-minute-offset-plan.md §4.5（D-B）/ §4.6（D-C）。

- D-B：create + late 两处 minute reserve 都减克隆 600 offset（避免 over-gating）。
- D-C：convert 严格幂等单飞（确定性 job_id + gateway pre-forward existing-check）+
  600 结转 marker server-set + PG Job.smart_state 泛化写入（convert 无 reservation 也落）。
- PreviewReuseResolution 返回 preview_reservation_id + preview_credit_amount（CodeX #2）。
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_JI = _REPO / "gateway" / "job_intercept.py"
_PR = _REPO / "gateway" / "preview_reuse_service.py"


def _func_src(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _flat(s: str) -> str:
    return " ".join(s.split())


def _create_src() -> str:
    body = _func_src(_JI, "intercept_create_job")
    assert body, "intercept_create_job 未找到"
    return body


def _late_src() -> str:
    body = _func_src(_JI, "update_source_metadata")
    assert body, "update_source_metadata 未找到"
    return body


# ---------------------------------------------------------------------------
# D-B：create + late reserve 减 offset
# ---------------------------------------------------------------------------


def test_db_create_reserve_reduced_by_offset():
    """🔥🔥 create reserve 用 _minute_reserve_credits（=max(0,shadow_credits−offset)），
    不再直接用 shadow_credits。offset 含 R_own(600 if reservation) + R_carryover。"""
    flat = _flat(_create_src())
    # CodeX P3：自有克隆侧用单一来源常量（非硬编码 600）
    assert "_SMART_CLONE_RESERVE_CREDITS if _smart_clone_reservation_id else 0" in flat
    assert 'preview_clone_credit_offset' in flat
    assert "_minute_reserve_credits = max(0, shadow_credits - _reserve_offset)" in flat
    # reserve 调用用净额（不是 gross shadow_credits）
    assert "estimated_credits=_minute_reserve_credits" in flat
    # 既有 preview-skip guard 不变（不回归 P3e-3c-1）
    assert "if shadow_credits > 0 and not _is_smart_preview:" in flat


def test_db_late_reserve_reduced_by_offset():
    """🔥🔥 late reserve（update_source_metadata）同减 offset（CodeX #3：两处都改）。"""
    flat = _flat(_late_src())
    assert "_SMART_CLONE_RESERVE_CREDITS if _late_ss.get(\"smart_clone_reservation_id\") else 0" in flat
    assert 'preview_clone_credit_offset' in flat
    assert "_late_minute_reserve = max(0, late_credits - _late_offset)" in flat
    assert "estimated_credits=_late_minute_reserve" in flat
    # 既有 preview-skip guard 不变
    assert "if late_credits > 0 and not _late_is_smart_preview:" in flat


def test_p3_reserve_offset_single_source_constant():
    """🔥 CodeX P3：reserve-offset R_own 用命名常量（非裸 600 字面量），防 pricing 漂移。
    克隆 reserve 字面量 600 仍由 test_create_600_clone_reserve_untouched 钉真值。"""
    src = _JI.read_text(encoding="utf-8")
    flat = _flat(src)
    assert "_SMART_CLONE_RESERVE_CREDITS = 600" in flat
    # create + late 两处 offset 都用常量
    assert flat.count("_SMART_CLONE_RESERVE_CREDITS if") >= 2


def test_p1_cost_summary_backfill_propagates_carryover():
    """🔥🔥 CodeX P1：job_terminal_mirror 把 metering_snapshot 的 carryover 字段传给
    backfill_smart_cost_summary（否则减免只停在 snapshot、cost summary 不可审计）。"""
    src = (_REPO / "gateway" / "job_terminal_mirror.py").read_text(encoding="utf-8")
    flat = _flat(src)
    assert 'carryover_applied_credits=_snap.get("clone_carryover_applied_credits")' in flat
    assert 'carryover_source_job_id=_snap.get("clone_carryover_source_job_id")' in flat


# ---------------------------------------------------------------------------
# D-C：convert 单飞 + marker + PG smart_state 泛化
# ---------------------------------------------------------------------------


def test_dc_convert_deterministic_job_id():
    """🔥🔥 convert 确定性 job_id 从 (user, preview_job_id) 派生（命名空间 :convert:）。"""
    flat = _flat(_create_src())
    assert '_convert_job_id = "job_" + hashlib.sha256(' in flat
    assert 'f"{user.id}:convert:{_reuse_preview_job_id}"' in flat
    assert ".hexdigest()[:32]" in flat


def test_dc_convert_pre_forward_existing_check_idempotent():
    """🔥🔥 gateway pre-forward existing-check 命中 → 幂等返回现有 F（绝不 forward）。"""
    flat = _flat(_create_src())
    assert "select(Job).where(Job.job_id == _convert_job_id)" in flat
    assert "if _existing_convert is not None:" in flat
    assert "return await _idempotent_convert_job_response(_convert_job_id)" in flat
    # 未命中才供 job_id + forward
    assert 'request_data["job_id"] = _convert_job_id' in flat


def test_dc_idempotent_response_does_not_re_forward():
    """🔥 _idempotent_convert_job_response 用 GET 取回现有 job（不重新 forward create）。"""
    body = _func_src(_JI, "_idempotent_convert_job_response")
    assert body, "_idempotent_convert_job_response 未找到"
    flat = _flat(body)
    assert "/jobs/" in flat and ".get(" in flat
    assert "internal_headers()" in flat
    # 不调 proxy_request（那会 forward POST create）
    assert "proxy_request" not in flat


def test_dc_convert_singleflight_advisory_lock():
    """🔥🔥 CodeX P2：advisory lock 在 pre-check **之前**串行同一预览的并发 convert，
    关掉 transient 重跑窗口。key 跨进程稳定（sha256，禁 python hash()）；非 PG no-op。"""
    create = _flat(_create_src())
    assert "_acquire_convert_singleflight_lock(db, _convert_job_id)" in create
    # lock 必须在 existing-check 之前
    assert create.index("_acquire_convert_singleflight_lock(") < create.index(
        "select(Job).where(Job.job_id == _convert_job_id)"
    )
    helper = _func_src(_JI, "_acquire_convert_singleflight_lock")
    assert helper, "_acquire_convert_singleflight_lock 未找到"
    hflat = _flat(helper)
    assert 'dialect.name != "postgresql"' in hflat  # 非 PG → no-op
    assert "pg_advisory_xact_lock" in hflat
    assert "hashlib.sha256(convert_job_id" in hflat  # 跨进程稳定 key
    # 🔥 禁用 python hash()（PYTHONHASHSEED 每进程随机 → 多 worker 算出不同 key → 不串行）
    assert "hash(convert_job_id" not in hflat


def test_dc_convert_markers_stamped_server_side():
    """🔥🔥 三 marker server-set 进 request_data['smart_state']（forward 进 JobRecord）。"""
    flat = _flat(_create_src())
    assert '"preview_clone_offset_reservation_id": _reuse_resolution.preview_reservation_id' in flat
    assert '"preview_clone_credit_offset": int(_reuse_resolution.preview_credit_amount)' in flat
    assert '"preview_source_job_id": _reuse_resolution.preview_job_id' in flat
    assert 'request_data["smart_state"] = dict(_convert_smart_state)' in flat


def test_dc_pg_smart_state_write_generalized():
    """🔥🔥 CodeX #1：PG Job.smart_state 写入泛化——convert（无 reservation）也落 marker。"""
    flat = _flat(_create_src())
    # convert fallback 不再恒 None，而是进入统一的 PG smart_state 变量。
    assert "elif _convert_smart_state:" in flat
    assert "_smart_state_for_pg = dict(_convert_smart_state)" in flat
    assert "smart_state=_smart_state_for_pg" in flat
    # _convert_smart_state 初始化为 None（普通 create inert）
    assert "_convert_smart_state: dict | None = None" in flat


def test_dc_convert_still_forces_no_clone_consent():
    """🔥 inert/安全：convert 仍强制 auto_voice_clone=False（不重扣 600、不重克隆）。"""
    flat = _flat(_create_src())
    assert '"auto_voice_clone": False' in flat


# ---------------------------------------------------------------------------
# CodeX #2：PreviewReuseResolution 返回 reservation id + amount
# ---------------------------------------------------------------------------


def test_resolution_carries_reservation_id_and_amount():
    """🔥🔥 PreviewReuseResolution 含 preview_reservation_id + preview_credit_amount，
    resolve 从已校验的 captured reservation 填充（金额取 amount_credits，不写死 600）。"""
    src = _PR.read_text(encoding="utf-8")
    flat = _flat(src)
    assert "preview_reservation_id: str" in flat
    assert "preview_credit_amount: int" in flat
    assert "preview_reservation_id=str(reservation.id)" in flat
    assert "preview_credit_amount=int(getattr(reservation, \"amount_credits\", 0) or 0)" in flat

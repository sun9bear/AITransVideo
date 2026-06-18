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
    """reserve 仅当 service_mode==smart + preview_mode is True +
    consent.auto_voice_clone is True + admin smart_preview_clone_enabled。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert 'service_mode == "smart"' in flat
    assert 'request_data.get("preview_mode") is True' in flat
    assert 'request_data["smart_consent"].get("auto_voice_clone") is True' in flat
    assert "smart_preview_clone_enabled" in body


def test_preview_reserve_does_not_catch_full_smart_requests():
    """CodeX PR #33 P1：预览 600 reserve 必须限定 preview_mode；普通 full Smart
    auto_voice_clone=True 由上方 paid-confirmation create reservation 处理，不得再进
    preview block 二次预留或绕过未勾选确认。"""
    body = _create_src()
    reserve_block = body[
        body.index("_smart_clone_skipped_reason: str | None = None"):
        body.index("upstream_response = await proxy_request(")
    ]
    assert 'request_data.get("preview_mode") is True' in reserve_block


def test_full_smart_reserve_does_not_catch_preview_requests():
    """CodeX PR #33: preview requests must not run the generic full-Smart
    create-time clone reservation path even if they carry paid-clone confirm."""
    body = _create_src()
    full_block = body[
        body.index('if service_mode == "smart" and user is not None:'):
        body.index("_smart_clone_skipped_reason: str | None = None")
    ]
    flat = " ".join(full_block.split())
    assert '_smart_request_is_preview = request_data.get("preview_mode") is True' in flat
    assert "not _smart_request_is_preview" in flat


def test_reserve_uses_pregenerated_job_id_option_c():
    """Option C：forward 前预生成 job_id（task_id=job_id）调 reserve，
    并把 job_id 塞 request_data 让 Job API 用它（决定性派生见 P1-A 测试）。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert '_pre_job_id = "job_" + hashlib.sha256(' in flat
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
    assert '"smart_clone_reserved_credits": _SMART_CLONE_RESERVE_CREDITS' in flat


def test_smart_preview_mode_stamped_from_preview_request_strict():
    """P3e-3 producer：smart_state 含 smart_preview_mode，由前端 preview_mode
    请求 strict is True 驱动（前端未送前 inert=False）→ pipeline 3min teaser+水印。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert '"smart_preview_mode": request_data.get("preview_mode") is True' in flat


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
    # skip 分支不得 return/raise（entitled 用户降级继续建任务）。
    # P3e-4a 起契约细化：降级仍不阻断 **entitled** 用户；唯一允许的 error return 是免费
    # exemption 兜底——由 _smart_preview_via_exemption 守卫，只拒绝**未获 smart** 的免费
    # 预览用户 600 预留失败（防免费白嫖完整任务），entitled 用户 via_exemption=False 仍降级。
    reserve_block = body[body.index("_smart_clone_skipped_reason: str | None = None"):body.index("upstream_response = await proxy_request(")]
    assert reserve_block.count("return _error_response") <= 1, (
        "reserve 降级区出现非预期阻断 return（entitled 用户降级不得被阻断）"
    )
    if "return _error_response" in reserve_block:
        _ri = reserve_block.index("return _error_response")
        _guard = reserve_block[max(0, _ri - 220):_ri]
        assert "_smart_preview_via_exemption" in _guard, (
            "reserve 区唯一的 error return 必须由 _smart_preview_via_exemption 守卫"
            "（免费 exemption 兜底），不得无条件阻断 entitled 用户降级"
        )
        assert '"smart_preview_reserve_failed"' in reserve_block


def test_pg_job_smart_state_set_from_reservation():
    """reserved → PG Job(smart_state=marker) via Job API echo/request data.

    Rebased on the post-PR32 create path, the PG row should keep the full
    smart_state dict already forwarded to the Job API instead of rebuilding a
    narrower marker from a single local reservation variable.
    """
    body = _create_src()
    flat = " ".join(body.split())
    assert "_smart_state_for_pg = (" in flat
    assert 'job_data.get("smart_state")' in flat
    assert 'request_data.get("smart_state")' in flat
    assert "smart_state=_smart_state_for_pg" in flat


# ---------------------------------------------------------------------------
# 对抗性复核加固（P1-A 双预留 / P1-B 孤儿 / P1-C job_id 一致性）
# ---------------------------------------------------------------------------


def test_pre_job_id_derived_from_user_and_idempotency_key():
    """🔥 P1-A + CodeX 终审 P1#2：pre_job_id 决定性派生自 (user.id, idempotency_
    key)（非每次 uuid4）→ 同用户同 key 重试复用同 task_id → reserve 幂等（根治
    双预留）；**含 user.id namespace** 防跨用户撞同 reservation。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "hashlib.sha256(" in flat
    assert 'f"{user.id}:{idempotency_key}".encode(' in flat
    # P3e preview pre_job_id 不再用 uuid4 生成（否则重试双预留）。
    # The non-preview Smart create path may still mint a regular job_id via
    # uuid4, so scope this assertion to the preview-reserve block.
    reserve_block = body[
        body.index("_smart_pre_job_id: str | None = None"):
        body.index("upstream_response = await proxy_request(")
    ]
    assert 'f"job_{_uuid.uuid4().hex}"' not in reserve_block


def test_client_supplied_job_id_and_smart_state_stripped():
    """🔥 CodeX 终审 P1#1：公共 create 路径无条件剥离客户端夹带的 job_id /
    smart_state（server-only 信任标记）——否则客户端可伪造
    smart_state.smart_clone_reservation_id 让 pipeline 误开 gate 克隆（业务白付）。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert 'request_data.pop("job_id", None)' in flat
    assert 'request_data.pop("smart_state", None)' in flat


def test_terminal_replay_idempotency_check():
    """🔥 CodeX 终审 P1#3：reserve 前查已有 PG job（同 deterministic id=重放）→
    存在则不再 reserve（防终态后重放再扣 600）。CodeX 复审：duplicate **不**回
    supply _pre_job_id（否则 submit_job save_job 覆盖 + runner 重启已有 job=重跑
    付费 workflow）→ Job API mint 新 id 新预设 job，不覆盖原 job。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "select(Job).where(Job.job_id == _pre_job_id)" in flat
    assert '"duplicate_create"' in flat
    # duplicate 分支不得 request_data["job_id"] = _pre_job_id（防覆盖重启）
    dup_idx = flat.index('"duplicate_create"')
    else_idx = flat.index("else: await ensure_credit_buckets_for_user")
    dup_branch = flat[dup_idx:else_idx]
    assert 'request_data["job_id"] = _pre_job_id' not in dup_branch, (
        "duplicate 分支不得回 supply _pre_job_id（否则 Job API 覆盖+重启原 job）"
    )


def test_forward_exception_releases_reservation():
    """🔥 CodeX 复审 P2：proxy_request 抛异常 → 及时释放 reservation 后 re-raise
    （否则 600 锁到 TTL）。"""
    body = _create_src()
    flat = " ".join(body.split())
    # forward 包 try/except，except 内 release + raise
    assert "upstream_response = await proxy_request(" in flat
    i_forward = flat.index("upstream_response = await proxy_request(")
    after = flat[i_forward:i_forward + 600]
    assert "except Exception:" in after
    assert "_release_smart_clone_reservation_on_create_failure(" in after
    assert "raise" in after


def test_release_helper_defined():
    """P1-B：create 失败路径释放 helper 定义（settle→release，绝不抛）。"""
    src = _JI.read_text(encoding="utf-8")
    assert "async def _release_smart_clone_reservation_on_create_failure(" in src
    helper = _func_src("_release_smart_clone_reservation_on_create_failure")
    assert "settle_smart_clone_reservation" in helper


def test_release_called_on_forward_failure():
    """P1-B：forward 非 2xx 但已预留 → 释放（避免挂 60min TTL）。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "upstream_response.status_code not in (200, 201, 202)" in flat
    assert "_release_smart_clone_reservation_on_create_failure(" in flat


def test_release_called_on_minute_reserve_failure():
    """P1-B：分钟点 reserve 失败（InsufficientCreditsError 等）→ 释放 smart
    clone 600（够克隆但不够分钟的孤儿场景）。"""
    body = _create_src()
    # 三处 compensate 路径都伴随 release（quota / insufficient / credit_error）
    assert body.count("_release_smart_clone_reservation_on_create_failure(") >= 4


def test_job_id_consistency_check():
    """🔥 P1-C：Job API 实际 job_id 与预生成 _smart_pre_job_id 不一致 → 释放 +
    loud error（关联断裂防护，fail-safe 用户不被扣）。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "_smart_pre_job_id" in flat
    assert "str(job_id) != str(_smart_pre_job_id)" in flat
    assert "MISMATCH" in body

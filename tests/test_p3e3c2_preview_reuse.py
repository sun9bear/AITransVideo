"""P3e-3c-2 — preview→正式 server 复用契约（钱-关键，防越权 + 不重扣 600 / 不重克隆）.

plan 2026-06-14-p3e2-preview-lane-design.md §7。用户预览满意转完整流程：前端**只传
`reuse_preview_job_id`**，server 校验同用户 + captured clone（600 已真扣）+ voice 活在库
→ server 取回 voice_id + 原视频引用复用 → 生成**完整付费 smart 任务**（扣分钟、交付、
**不重克隆、不重扣 600**）。

钱-不变量：
1. ❌ 不重扣 600：复用路径强制 `auto_voice_clone=False` → create 600-reserve 块
   （条件 `auto_voice_clone is True`）跳过 → 不创建新 reservation。
2. ❌ 不重克隆：pipeline `_smart_needs_new_clone` 要求 consent.auto_voice_clone is True
   → False → 绝不调 MiniMax。
3. ✅ 照常扣分钟：不设 preview_mode/smart_preview_mode → 完整任务 minute reserve 正常。
4. ❌ 防越权：voice_a server 端从 captured reservation.captured_voice_id 取；客户端夹带
   的 voice_a/voice_b/source 一律覆盖。
5. ❌ 拒绝不合格预览（未捕获/已 release/voice 过期）→ 显式 4xx，绝不静默重克隆/错扣。

source-scan（不 import gateway 模块避 database-stub 污染，见 memory
feedback_test_database_stub_convention）+ resolve 服务行为测试（mocked db）。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_REPO = Path(__file__).resolve().parents[1]
_JI = _REPO / "gateway" / "job_intercept.py"

# gateway on path + stub ``database`` so importing the service / models doesn't
# build a real engine（见 memory feedback_test_database_stub_convention：
# setdefault、绝不替换已存在的真模块对象）。
_gateway_dir = str(_REPO / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)


def _ast_func_src(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _create_src() -> str:
    body = _ast_func_src(_JI, "intercept_create_job")
    assert body, "intercept_create_job 未找到"
    return body


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_db(*rows):
    """A MagicMock db whose successive ``execute`` calls return results whose
    ``scalar_one_or_none`` yields the given rows in order."""
    results = []
    for r in rows:
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(return_value=r)
        results.append(res)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    return db


# ---------------------------------------------------------------------------
# resolve_preview_reuse 服务行为（钱-关键 + 防越权）
# ---------------------------------------------------------------------------


def test_resolve_rejects_when_preview_not_found():
    import preview_reuse_service as prs

    db = _make_db(None)  # Job lookup → None
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=uuid.uuid4(), preview_job_id="job_x")
    )
    assert resolution is None
    assert reason == "preview_not_found"


def test_resolve_rejects_cross_user_overreach():
    """🔥🔥 防越权：preview 属于别的 user → forbidden，绝不取回其 voice。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    attacker = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="youtube_url", source_ref="https://y/v",
        smart_state={"smart_preview_mode": True},
    )
    db = _make_db(preview_job)  # only Job lookup happens, then forbidden
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=attacker, preview_job_id="job_prev")
    )
    assert resolution is None
    assert reason == "preview_forbidden"


def test_resolve_rejects_when_job_is_not_a_preview():
    """🔥🔥 契约硬化（CodeX P3e-4c 合并前审查）：reuse 的源 job 必须是 smart **预览**
    任务（smart_state.smart_preview_mode is True）。因为 smart_preview_clone_enabled
    把 *full* smart 也纳入 600 reservation gate，一个普通 full-smart 成品 job 同样会有
    captured reservation + chargeable billing + 活 voice——若无此守卫即可被当预览
    convert（同用户 + 已付费故钱安全，但破坏 preview→full 契约）。守卫在 ownership 之后
    （非 owner 不能探测是否预览），smart_state 缺键 / None 一律 fail-safe 拒绝。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    # 普通 full-smart 成品：owner 对、字段齐全，但 smart_state 不含 smart_preview_mode
    # （缺键）或整体为 None → 在 ownership 之后、reservation 查询之前被挡（只发生 1 次
    # Job lookup）。
    for bad_state in ({"status": "completed"}, None, {"smart_preview_mode": False}):
        non_preview_job = SimpleNamespace(
            job_id="job_full", user_id=owner,
            source_type="youtube_url", source_ref="https://youtu.be/abc",
            smart_state=bad_state,
        )
        db = _make_db(non_preview_job)
        resolution, reason = _run(
            prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_full")
        )
        assert resolution is None, f"smart_state={bad_state!r} 应被拒"
        assert reason == "preview_not_a_preview_job", f"smart_state={bad_state!r} reason 错"


def test_resolve_rejects_when_clone_not_captured():
    """🔥🔥 钱-关键：没有 captured reservation（reserve 后 release / 从未克隆）→
    not_captured，绝不当作"已付 600"来复用。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="youtube_url", source_ref="https://y/v",
        smart_state={"smart_preview_mode": True},
    )
    db = _make_db(preview_job, None)  # Job ok, captured reservation lookup → None
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert resolution is None
    assert reason == "preview_clone_not_captured"


def test_resolve_rejects_when_billing_event_not_chargeable():
    """🔥 capture 条件 = chargeable billing event（reader B：唯一权威计费信号）。
    reservation 标 captured 但无 chargeable event（异常态）→ not_captured。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    rid = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="youtube_url", source_ref="https://y/v",
        smart_state={"smart_preview_mode": True},
    )
    reservation = SimpleNamespace(
        id=rid, status="captured", settled_at=object(),
        captured_voice_id="vc_minimax_1",
    )
    db = _make_db(preview_job, reservation, None)  # billing event → None
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert resolution is None
    assert reason == "preview_clone_not_captured"


def test_resolve_rejects_when_voice_expired_or_deleted():
    """🔥 voice 已不在库（expired_at 非空 / 已删）→ voice_unavailable，
    不复用悬空 voice_id。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    rid = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="youtube_url", source_ref="https://y/v",
        smart_state={"smart_preview_mode": True},
    )
    reservation = SimpleNamespace(
        id=rid, status="captured", settled_at=object(),
        captured_voice_id="vc_minimax_1",
    )
    billing = SimpleNamespace(reservation_id=rid, chargeable=True, voice_id="vc_minimax_1")
    db = _make_db(preview_job, reservation, billing, None)  # UserVoice → None
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert resolution is None
    assert reason == "preview_voice_unavailable"


def test_resolve_rejects_when_source_missing():
    """🔥 preview 没有可复用 source（异常态）→ source_unavailable。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    rid = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="", source_ref="",
        smart_state={"smart_preview_mode": True},
    )
    reservation = SimpleNamespace(
        id=rid, status="captured", settled_at=object(),
        captured_voice_id="vc_minimax_1",
    )
    billing = SimpleNamespace(reservation_id=rid, chargeable=True, voice_id="vc_minimax_1")
    voice = SimpleNamespace(user_id=owner, voice_id="vc_minimax_1", expired_at=None)
    db = _make_db(preview_job, reservation, billing, voice)
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert resolution is None
    assert reason == "preview_source_unavailable"


def test_resolve_success_returns_server_derived_voice_and_source():
    """🔥🔥🔥 钱-关键成功路径：同用户 + captured + chargeable + voice 活 + source 在
    → 返回 server 端取回的 voice_id + 原视频引用（绝不依赖前端传入）。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    rid = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="youtube_url", source_ref="https://youtu.be/abc",
        smart_state={"smart_preview_mode": True},
    )
    reservation = SimpleNamespace(
        id=rid, status="captured", settled_at=object(),
        captured_voice_id="vc_minimax_main",
    )
    billing = SimpleNamespace(reservation_id=rid, chargeable=True, voice_id="vc_minimax_main")
    voice = SimpleNamespace(user_id=owner, voice_id="vc_minimax_main", expired_at=None)
    db = _make_db(preview_job, reservation, billing, voice)

    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert reason is None
    assert resolution is not None
    assert resolution.voice_id == "vc_minimax_main"
    assert resolution.source_type == "youtube_url"
    assert resolution.source_ref == "https://youtu.be/abc"
    assert resolution.preview_job_id == "job_prev"


# ---------------------------------------------------------------------------
# create 路径接线（source-scan）
# ---------------------------------------------------------------------------


def test_create_reuse_block_gated_and_calls_resolver():
    """🔥 复用块存在、读 reuse_preview_job_id、gate 在 admin
    smart_preview_clone_enabled、调 resolve_preview_reuse。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "reuse_preview_job_id" in flat
    assert "resolve_preview_reuse(" in flat
    assert "smart_preview_clone_enabled" in flat
    # flag off + 有 reuse 请求 → 显式拒绝（不静默改建普通任务）
    assert "reuse_disabled" in flat


def test_create_reuse_forces_no_clone_consent():
    """🔥🔥 钱-关键：复用路径强制 auto_voice_clone=False → 600-reserve 块跳过
    （不重扣）+ pipeline 不重克隆。"""
    body = _create_src()
    flat = " ".join(body.split())
    # 强制 consent.auto_voice_clone False（覆盖客户端）
    assert '"auto_voice_clone": False' in flat


def test_create_reuse_voice_a_is_server_derived():
    """🔥🔥 防越权：voice_a 取自 server resolution（captured_voice_id），
    不信任客户端传入的 voice_a。"""
    body = _create_src()
    flat = " ".join(body.split())
    # voice_a 由 resolution 赋值（server-derived）
    assert 'request_data["voice_a"] = ' in flat and ".voice_id" in flat
    # source 也由 resolution 覆盖（原视频引用复用）
    assert 'request_data["source"]' in flat and ".source_ref" in flat


def test_create_reuse_block_before_600_reserve():
    """🔥🔥 顺序：复用覆盖（强制 consent False）必须在 600-reserve 触发判断之前，
    否则跳分钟/跳克隆 reserve 不生效。"""
    body = _create_src()
    # reuse override 设置 source 覆盖的锚点
    reuse_anchor = body.index('request_data["voice_a"] = ')
    # 600-reserve 触发条件锚点
    reserve_anchor = body.index('request_data["smart_consent"].get("auto_voice_clone") is True')
    assert reuse_anchor < reserve_anchor


def test_create_reuse_clears_preview_mode_for_full_delivery():
    """🔥 完整任务：复用路径 pop preview_mode → 不设 smart_preview_mode → 照常扣分钟
    + 交付完整成片（非 teaser/水印/stream-only）。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert 'pop("preview_mode"' in flat


def test_create_reuse_default_inert():
    """🔥 inert：无 reuse_preview_job_id → 不进复用块（既有 create 字节级不变）。
    复用块以 reuse_preview_job_id present 为唯一入口。"""
    body = _create_src()
    # 复用块由 _reuse_preview_job_id is not None 守卫（present 才进）
    assert "_reuse_preview_job_id is not None" in body or \
        "if _reuse_preview_job_id:" in body
    # inert 入口：key **缺省**（不在 request_data）才跳过；present 即视为复用意图。
    assert '"reuse_preview_job_id" in request_data' in body


def test_create_reuse_malformed_key_rejected_not_silent():
    """🔥🔥 CodeX P2：present-but-malformed reuse_preview_job_id（[]/""/非串）
    **不得**静默回落到普通 create（否则用户本想复用却被重扣 600 + 重克隆）。
    key 存在即视为复用意图 → 非法值 400 拒绝。"""
    body = _create_src()
    flat = " ".join(body.split())
    # 以"key 在 request_data"判存在（非仅看值是否合法字符串）
    assert '"reuse_preview_job_id" in request_data' in flat
    # 非法值显式拒绝（不静默 fall through）
    assert "reuse_request_invalid" in flat


# ---------------------------------------------------------------------------
# entitlement 决策 A（项目主 2026-06-15「走正式流程·需升级套餐」）：
# preview→full 转化由 plan 不含 smart 的用户（免费档）发起时，给可区分的升级指引。
# 钱模型不变（照常按分钟扣、复用不重扣 600）；这里只验证 4xx reason 的可区分性。
# ---------------------------------------------------------------------------


def test_create_reuse_blocked_plan_gives_upgrade_not_contact_admin():
    """🔥 plan 不含 smart 的用户转完整 → smart_upgrade_required（前端渲染升级 CTA），
    而非误导性的全局 smart_disabled「联系管理员开启」。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "smart_upgrade_required" in flat, "缺升级 reason code"
    up = body.index("smart_upgrade_required")
    # 升级分支仅由 reuse 转化触发（_reuse_preview_job_id is not None 守卫在前）
    guard = body.rfind("_reuse_preview_job_id is not None", 0, up)
    assert guard != -1, "smart_upgrade_required 必须在 reuse 守卫之内"
    # 且基于 plan base（allowed_service_modes）不含 smart 判定 —— 区分 kill-switch 全局关
    window = body[guard:up]
    assert "get_effective_plan_gate" in window and "not in _plan_base_modes" in window, \
        "升级文案须基于 plan base 不含 smart 判定，而非笼统拦截"


def test_create_reuse_upgrade_preserves_generic_smart_disabled():
    """🔥 既有 kill-switch 语义不破：升级分支只在 reuse+plan-缺-smart 命中；其余
    smart 被停（kill-switch 全局关 / 非 reuse）仍回 smart_disabled 兜底。"""
    body = _create_src()
    assert "smart_disabled" in body
    up = body.index("smart_upgrade_required")
    # 通用 smart_disabled 返回必须保留在升级分支**之后**（兜底未被替换）
    disabled = body.index("smart_disabled", up)
    assert disabled > up, "通用 smart_disabled 兜底返回必须保留在升级分支之后"


def test_create_reuse_upgrade_inside_smart_entitlement_gate():
    """🔥 位置正确：升级分支嵌在 smart entitlement 门（"smart" not in effective_modes）
    + not _smart_preview_exempt 块内 —— 经预览 exemption 放行的 lane 不受影响。"""
    body = _create_src()
    gate = body.index('"smart" not in effective_modes')
    exempt = body.index("if not _smart_preview_exempt:", gate)
    up = body.index("smart_upgrade_required", exempt)
    assert gate < exempt < up, "升级分支须嵌在 smart 门 + not exempt 块内"

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
import json
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# A 方案 pre-flight 时长闸（plan 2026-06-16 转化漏斗 UX）——消除与 D7 匿名转完整路径的
# 不对称缺口（2026-06-16 对抗审查 finding #2）。本地源超套餐 cap 时提前拦，不建注定
# 失败的 job；复用 D7 同款两档 helper + reason 字面量契约（前端单一 mapper 识别）。
# ---------------------------------------------------------------------------


def _local_preview_chain(owner, *, source_ref="/opt/x/uploads/u/full.mp4"):
    """成功 LOCAL-源预览复用解析所需的 4 行 DB 链（Job / reservation / billing / voice）。"""
    rid = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="local_video", source_ref=source_ref,
        smart_state={"smart_preview_mode": True},
    )
    reservation = SimpleNamespace(
        id=rid, status="captured", settled_at=object(),
        captured_voice_id="vc_minimax_main", amount_credits=600,
    )
    billing = SimpleNamespace(reservation_id=rid, chargeable=True, voice_id="vc_minimax_main")
    voice = SimpleNamespace(user_id=owner, voice_id="vc_minimax_main", expired_at=None)
    return preview_job, reservation, billing, voice


def test_resolve_probes_local_source_duration(monkeypatch):
    """🔥 A 方案：本地源 → resolver 重探 final_path 全长，放进 source_duration_seconds。
    （桩 _probe_source_duration_seconds 避免对 ffprobe / 真视频依赖，使测试 hermetic。）"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()

    async def _fake_probe(_path):
        return 642.0

    monkeypatch.setattr(prs, "_probe_source_duration_seconds", _fake_probe)
    db = _make_db(*_local_preview_chain(owner))
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert reason is None and resolution is not None
    assert resolution.source_type == "local_video"
    assert resolution.source_duration_seconds == 642.0


def test_resolve_skips_probe_for_youtube_source(monkeypatch):
    """🔥 YouTube 源**不**重探（既有 yt-dlp 创建期闸处理，不重复）→ source_duration_seconds
    None，且 _probe_source_duration_seconds 绝不被调用（防对 URL 误跑 ffprobe / 双探测）。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()
    called = {"n": 0}

    async def _spy(_path):
        called["n"] += 1
        return 999.0

    monkeypatch.setattr(prs, "_probe_source_duration_seconds", _spy)
    rid = uuid.uuid4()
    preview_job = SimpleNamespace(
        job_id="job_prev", user_id=owner,
        source_type="youtube_url", source_ref="https://youtu.be/abc",
        smart_state={"smart_preview_mode": True},
    )
    reservation = SimpleNamespace(
        id=rid, status="captured", settled_at=object(),
        captured_voice_id="vc", amount_credits=600,
    )
    billing = SimpleNamespace(reservation_id=rid, chargeable=True, voice_id="vc")
    voice = SimpleNamespace(user_id=owner, voice_id="vc", expired_at=None)
    db = _make_db(preview_job, reservation, billing, voice)
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert reason is None and resolution is not None
    assert resolution.source_duration_seconds is None
    assert called["n"] == 0, "YouTube 源不得重探（既有 yt-dlp 路径处理）"


def test_resolve_local_duration_none_when_probe_fails(monkeypatch):
    """🔥 本地源探测失败（None）→ resolution 仍成功、duration None（闸跳过、管线兜底，
    绝不因探测失败误拒可转完整的源）。"""
    import preview_reuse_service as prs

    owner = uuid.uuid4()

    async def _fail(_path):
        return None

    monkeypatch.setattr(prs, "_probe_source_duration_seconds", _fail)
    db = _make_db(*_local_preview_chain(owner))
    resolution, reason = _run(
        prs.resolve_preview_reuse(db, user_id=owner, preview_job_id="job_prev")
    )
    assert reason is None and resolution is not None
    assert resolution.source_duration_seconds is None


def _smart_reuse_block_src() -> str:
    """intercept_create_job 内 smart 预览复用块（``resolve_preview_reuse`` 起、D7 块
    ``_reuse_anon_preview_id = None`` 止）——隔离断言，避免命中 D7 块同名 helper。"""
    body = _create_src()
    start = body.index("resolve_preview_reuse")
    end = body.index("_reuse_anon_preview_id = None")
    assert start < end, "smart 复用块边界异常"
    return body[start:end]


def _smart_reuse_block_code() -> str:
    """剥注释后的 smart 复用块（块内字符串无 #，按行 split('#') 安全；对齐 D7
    ``_d7_block_code()``）——避免未来注释里提及 _anon_convert_duration_block( 等误满足
    断言、或删真分支只留注释仍绿（CodeX 评审 finding：结构扫描脆弱性）。"""
    return "\n".join(
        line.split("#", 1)[0] for line in _smart_reuse_block_src().splitlines()
    )


def test_create_reuse_has_duration_preflight_gate():
    """🔥 smart 预览复用块加 pre-flight 时长闸：复用 D7 两档 helper + plan-tier cap +
    resolver 重探的源全长 + via=smart_preview_convert（区分 D7 默认 via）。扫**剥注释**的
    块代码（避免注释里的同名串满足断言）。行为正确性由下方 end-to-end 测试锁定。"""
    flat = " ".join(_smart_reuse_block_code().split())
    assert "_anon_convert_duration_block(" in flat, "须调两档时长判定 helper"
    assert "_anon_convert_duration_error_response(" in flat, "渲染须经可测纯 helper"
    assert "source_duration_seconds" in flat, "须用 resolver 重探的源全长"
    assert "get_effective_plan_gate" in flat, "cap 取自 plan-tier（trial-aware）"
    assert "max_self_serve_duration_minutes" in flat, "须算最高自助套餐阈值用于分流"
    assert "minimum_self_serve_plan_for" in flat, "升级须具名推荐能处理该时长的最低套餐"
    assert 'via="smart_preview_convert"' in flat, "smart 路径须标注 via=smart_preview_convert"


def test_create_reuse_duration_gate_before_single_flight():
    """🔥 顺序：时长闸（超限不建 job）必须在 single-flight job_id mint 之前——否则会先
    建出注定失败的任务再拒，违背"不建注定失败 job"的目的。扫剥注释代码。"""
    block = _smart_reuse_block_code()
    gate = block.index("_anon_convert_duration_block(")
    mint = block.index("_convert_job_id = ")
    assert gate < mint, "时长闸须在 single-flight job_id mint 之前"


# ---------------------------------------------------------------------------
# end-to-end 行为：驱动 intercept_create_job 真正跑完 smart 复用闸（CodeX 复审 HIGH）。
# 结构扫描只证"helper 名字在源里"，挡不住反转守卫 / 调换实参 / 闸恒触发等坏重构；这里
# 真调 intercept_create_job 锁住 wiring + HARD INVARIANT #2（拒绝不建 job/不取单飞锁/
# 不 forward）+ #3/#5（时长未知放行）。闸在任何 db.execute 之前返回，故拒绝路径 db 不被
# 触；放行用 sentinel 证明执行流越过闸到达 single-flight，无需 mock 整条下游 create 流。
# ---------------------------------------------------------------------------


def _make_request(body: dict):
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    req.headers = {"content-type": "application/json"}
    req.method = "POST"
    req.url = MagicMock()
    req.url.path = "/job-api/jobs"
    req.query_params = {}
    return req


def _make_user(*, role="user", plan_code="free"):
    return SimpleNamespace(
        id="uid-1", email="u@test.com", display_name="Test",
        role=role, plan_code=plan_code,
        free_jobs_quota_total=5, free_jobs_quota_used=0,
    )


def _convert_resolution(*, duration):
    """server-derived smart reuse resolution（绝不取自客户端）。"""
    return SimpleNamespace(
        preview_job_id="job_prev", voice_id="vc_main",
        source_type="local_video", source_ref="/opt/x/uploads/u/full.mp4",
        preview_reservation_id="rid-1", preview_credit_amount=600,
        source_duration_seconds=duration,
    )


def _run_intercept_smart_reuse(resolution, *, user):
    """跑 intercept_create_job 的 smart 复用路径：开 admin 旗 + 桩 resolver；返回
    (response_or_exc, lock_mock, proxy_mock)。lock/proxy 桩为 AsyncMock（拒绝路径断言未调）。"""
    import job_intercept as ji
    import preview_reuse_service as prs

    req = _make_request({"reuse_preview_job_id": "job_prev", "service_mode": "smart"})
    db = AsyncMock()
    lock = AsyncMock()
    proxy = AsyncMock()
    with patch.object(prs, "resolve_preview_reuse", AsyncMock(return_value=(resolution, None))), \
         patch("admin_settings.load_settings",
               return_value=SimpleNamespace(smart_preview_clone_enabled=True)), \
         patch.object(ji, "_acquire_convert_singleflight_lock", lock), \
         patch.object(ji, "proxy_request", proxy):
        resp = _run(ji.intercept_create_job(req, db, user))
    return resp, lock, proxy, db


def test_gate_rejects_over_max_end_to_end():
    """🔥🔥 HIGH：免费用户（cap 10）转完整 198min 本地源 → 403 duration_over_max_plan，
    via=smart_preview_convert，**且不取单飞锁 / 不 forward / 不碰 db**（HARD INVARIANT #2）。"""
    resp, lock, proxy, db = _run_intercept_smart_reuse(
        _convert_resolution(duration=11905.71), user=_make_user(plan_code="free")
    )
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["error"] == "duration_over_max_plan"
    assert body["detail"]["via"] == "smart_preview_convert"
    assert "recommended_plan" not in body["detail"], "升无可升不推荐套餐"
    lock.assert_not_called()
    proxy.assert_not_called()
    db.execute.assert_not_called()


def test_gate_rejects_upgrade_required_end_to_end():
    """🔥🔥 HIGH：免费用户（cap 10）转完整 30min 本地源 → 403 duration_upgrade_required，
    具名推荐最低自助套餐 Plus/45（minimum_self_serve_plan_for），不取锁 / 不 forward。"""
    resp, lock, proxy, db = _run_intercept_smart_reuse(
        _convert_resolution(duration=1800.0), user=_make_user(plan_code="free")
    )
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["error"] == "duration_upgrade_required"
    assert body["detail"]["recommended_plan"] == "Plus"
    assert body["detail"]["recommended_plan_minutes"] == 45
    assert body["detail"]["via"] == "smart_preview_convert"
    lock.assert_not_called()
    proxy.assert_not_called()


def test_gate_allows_when_duration_unknown_end_to_end():
    """🔥 HARD INVARIANT #3/#5：source_duration_seconds=None（YouTube / 探测失败）→ 闸放行。
    用 sentinel 证明执行流越过闸到达 single-flight（_acquire_convert_singleflight_lock），
    而**不**返回 403——无需 mock 整条下游 create 流。"""
    import job_intercept as ji
    import preview_reuse_service as prs

    class _GatePassed(Exception):
        pass

    async def _sentinel(*_a, **_k):
        raise _GatePassed()

    req = _make_request({"reuse_preview_job_id": "job_prev", "service_mode": "smart"})
    with patch.object(prs, "resolve_preview_reuse",
                      AsyncMock(return_value=(_convert_resolution(duration=None), None))), \
         patch("admin_settings.load_settings",
               return_value=SimpleNamespace(smart_preview_clone_enabled=True)), \
         patch.object(ji, "_acquire_convert_singleflight_lock", _sentinel):
        with pytest.raises(_GatePassed):
            _run(ji.intercept_create_job(req, AsyncMock(), _make_user(plan_code="free")))


def test_gate_exempts_admin_end_to_end():
    """🔥 admin 豁免（与既有创建期 duration gate 一致）：admin 转完整超长源 → 闸放行
    （越过闸到 single-flight），即便 198min 远超任何 cap。"""
    import job_intercept as ji
    import preview_reuse_service as prs

    class _GatePassed(Exception):
        pass

    async def _sentinel(*_a, **_k):
        raise _GatePassed()

    req = _make_request({"reuse_preview_job_id": "job_prev", "service_mode": "smart"})
    with patch.object(prs, "resolve_preview_reuse",
                      AsyncMock(return_value=(_convert_resolution(duration=11905.71), None))), \
         patch("admin_settings.load_settings",
               return_value=SimpleNamespace(smart_preview_clone_enabled=True)), \
         patch.object(ji, "_acquire_convert_singleflight_lock", _sentinel):
        with pytest.raises(_GatePassed):
            _run(ji.intercept_create_job(
                req, AsyncMock(), _make_user(role="admin", plan_code="free")
            ))

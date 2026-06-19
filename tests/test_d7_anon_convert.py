"""D7 — 匿名预览转完整（resolve_anonymous_preview_reuse + intercept_create_job 块）.

plan 2026-06-15-anonymous-preview-claim-binding-plan.md §6.5 / D7。

认领后用**完整原始源** audit.stored_upload_path（非 teaser）建正式计费 job。前端送
reuse_anonymous_preview_id，server 校验所有权（claim_user_id==user）+ 路径/hash（在上传
根内/非 teaser/sha256 匹配 source_hash）→ **只覆盖 source** → 走**正常付费流程**。
2026-06-16 项目主拍板：转完整=认领原视频后走完整正常流程，用户重选模式（快捷/工作台/
智能），各模式克隆行为照旧（快捷/智能自动克隆、工作台可选），正常扣点、不漏计费——
**不**强制预设、**不**中和克隆。唯一额外处理=剥 preview_mode（转完整≠预览，防跳分钟透支）。

resolver 行为测试用**真 tmp 文件**（真路径/hash 校验覆盖，非全 mock）；intercept 块用
区域源扫描锁结构（database-stub 见 memory feedback_test_database_stub_convention）。
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_REPO = Path(__file__).resolve().parents[1]
_JI = _REPO / "gateway" / "job_intercept.py"
_PRS = _REPO / "gateway" / "preview_reuse_service.py"

_gateway_dir = str(_REPO / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_db(row):
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=row)
    db = MagicMock()
    db.execute = AsyncMock(return_value=res)
    return db


def _record(*, uid, status="ready_for_mode", source_hash, stored_path, audit_extra=None, expires_at="__future__"):
    # prod 形状（CodeX P2）：匿名 record.source_type = "local_upload"（intake 内部值
    # SourceType.LOCAL_UPLOAD，**非** local_video）；audit 同时含 stored_upload_path
    # + teaser_path（anonymous_preview_api.py:528-529）；expires_at 认领已延长。
    sp = Path(str(stored_path))
    audit = {
        "stored_upload_path": str(stored_path),
        "teaser_path": str(sp.parent / f"teaser_{sp.stem}.mp4"),
    }
    if audit_extra:
        audit.update(audit_extra)
    if expires_at == "__future__":
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return SimpleNamespace(
        preview_id="prv_x",
        claim_user_id=uid,
        status=status,
        source_type="local_upload",
        source_hash=source_hash,
        audit=audit,
        expires_at=expires_at,
    )


def _write_file(path: Path, data: bytes = b"full-original-upload-bytes"):
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _resolver(monkeypatch, *, root: Path, probe_duration=None):
    """import resolver + monkeypatch _anon_upload_root → root（使 tmp 文件视为在上传根内）。

    默认把**源时长探测**桩成返回 ``probe_duration``（None = 探测失败语义）——tmp 文件
    不是真视频，真 ffprobe 会失败；桩掉避免对 ffprobe 安装 / 真视频的依赖，并使 resolver
    测试 hermetic（不 spawn subprocess）。新增的时长用例显式传 ``probe_duration=<秒>``。
    """
    import preview_reuse_service as prs

    monkeypatch.setattr(prs, "_anon_upload_root", lambda: root.resolve())

    async def _fake_probe(_path):
        return probe_duration

    monkeypatch.setattr(prs, "_probe_source_duration_seconds", _fake_probe)
    return prs


# ---------------------------------------------------------------------------
# 1. resolver 行为（真 tmp 文件，真路径/hash/越权校验）
# ---------------------------------------------------------------------------


def test_resolve_happy(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    f = tmp_path / "u123_video.mp4"
    h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path)
    db = _make_db(_record(uid=uid, source_hash=h, stored_path=f))
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert reason is None
    assert res is not None
    # CodeX P1：record.source_type 是 "local_upload"（prod 形状）→ resolution 必须归一为
    # "local_video"（create 流程/pipeline 只认 local_video，不认 local_upload）。
    assert res.source_type == "local_video"
    assert res.source_ref == str(f.resolve())
    assert res.preview_id == "prv_x"


def test_resolve_not_found(monkeypatch, tmp_path):
    prs = _resolver(monkeypatch, root=tmp_path)
    db = _make_db(None)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uuid.uuid4(), preview_id="x"))
    assert res is None and reason == prs.REASON_ANON_NOT_FOUND


def test_resolve_not_claimed_forbidden(monkeypatch, tmp_path):
    f = tmp_path / "v.mp4"; h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = _record(uid=None, source_hash=h, stored_path=f)  # claim_user_id NULL = 未认领
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uuid.uuid4(), preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_FORBIDDEN


def test_resolve_other_user_forbidden(monkeypatch, tmp_path):
    f = tmp_path / "v.mp4"; h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = _record(uid=uuid.uuid4(), source_hash=h, stored_path=f)  # 别人认领的
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uuid.uuid4(), preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_FORBIDDEN


def test_resolve_wrong_status_forbidden(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    f = tmp_path / "v.mp4"; h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = _record(uid=uid, status="rejected", source_hash=h, stored_path=f)
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_FORBIDDEN


def test_resolve_no_stored_path(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = SimpleNamespace(
        preview_id="prv_x", claim_user_id=uid, status="ready_for_mode",
        source_type="local_video", source_hash="h", audit={},  # 无 stored_upload_path
    )
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_SOURCE_UNAVAILABLE


def test_resolve_path_traversal_outside_root(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    outside = tmp_path / "outside"; outside.mkdir()
    root = tmp_path / "root"; root.mkdir()
    f = outside / "v.mp4"; h = _write_file(f)  # 文件在 root 外
    prs = _resolver(monkeypatch, root=root)
    db = _make_db(_record(uid=uid, source_hash=h, stored_path=f))
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_SOURCE_UNAVAILABLE


def test_resolve_teaser_rejected(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    f = tmp_path / "teaser_v.mp4"; h = _write_file(f)  # teaser 命名（stem 兜底）
    prs = _resolver(monkeypatch, root=tmp_path)
    db = _make_db(_record(uid=uid, source_hash=h, stored_path=f))
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_SOURCE_UNAVAILABLE


def test_resolve_rejects_exact_teaser_path(monkeypatch, tmp_path):
    """stored_upload_path 恰等于 audit.teaser_path → 拒（精确比对，CodeX P3）。
    故意用不以 teaser_ 开头的文件名，证明走的是精确比对而非 stem 启发式。"""
    uid = uuid.uuid4()
    clip = tmp_path / "u1_clip.mp4"; h = _write_file(clip)
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = SimpleNamespace(
        preview_id="prv_x", claim_user_id=uid, status="ready_for_mode",
        source_type="local_upload", source_hash=h,
        audit={"stored_upload_path": str(clip), "teaser_path": str(clip)},  # stored==teaser
    )
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_SOURCE_UNAVAILABLE


def test_resolve_file_missing(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    f = tmp_path / "gone.mp4"  # 不创建
    prs = _resolver(monkeypatch, root=tmp_path)
    db = _make_db(_record(uid=uid, source_hash="deadbeef", stored_path=f))
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_SOURCE_UNAVAILABLE


def test_resolve_expired_rejected(monkeypatch, tmp_path):
    """record 已过期（认领延长 7d 后仍过期）→ 拒（源可能已被 sweeper 清，复审 MEDIUM）。"""
    uid = uuid.uuid4()
    f = tmp_path / "v.mp4"; h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = _record(uid=uid, source_hash=h, stored_path=f,
                  expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_SOURCE_UNAVAILABLE


def test_resolve_hash_mismatch(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    f = tmp_path / "v.mp4"; _write_file(f, b"real-bytes")
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = _record(uid=uid, source_hash="0" * 64, stored_path=f)  # 错 hash
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert res is None and reason == prs.REASON_ANON_SOURCE_UNAVAILABLE


def test_resolve_hash_normalized_prefix(monkeypatch, tmp_path):
    """record.source_hash 带 'sha256:' 前缀也能匹配（归一化防格式漂移）。"""
    uid = uuid.uuid4()
    f = tmp_path / "v.mp4"; h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path)
    rec = _record(uid=uid, source_hash=f"sha256:{h}", stored_path=f)
    db = _make_db(rec)
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert reason is None and res is not None


# ---------------------------------------------------------------------------
# 2. intercept_create_job D7 块结构守卫（区域源扫描）
# ---------------------------------------------------------------------------


def _d7_block_src() -> str:
    # 锚点用 intercept 块独有的 ``_reuse_anon_preview_id = None``（"D7 匿名预览转完整"
    # 在 helper docstring + intercept 注释里都出现，find 会命中 helper → 范围跨错）。
    src = _JI.read_text(encoding="utf-8")
    start = src.find("_reuse_anon_preview_id = None")
    assert start != -1, "intercept_create_job 未找到 D7 块"
    end = src.find("PR#3C-b3g", start)
    assert end != -1, "D7 块结束锚点未找到"
    return src[start:end]


def _d7_block_code() -> str:
    """剥注释后的 D7 块代码（本块字符串内无 #，故按行 split('#') 安全）——
    用于负向断言（避免注释提及 smart_consent/voice_a 等被误判）。"""
    return "\n".join(line.split("#", 1)[0] for line in _d7_block_src().splitlines())


def test_d7_block_reads_field_and_calls_override():
    """D7 块：读 reuse_anonymous_preview_id → resolve → 调 helper 覆盖 source。
    （source 覆盖的具体 mutation 在 _apply_anon_convert_source_override，行为测试覆盖。）"""
    b = _d7_block_src()
    assert "reuse_anonymous_preview_id" in b
    assert "resolve_anonymous_preview_reuse" in b
    assert "_apply_anon_convert_source_override" in b


def test_d7_block_is_thin_source_override():
    """D7 块 = 调 _apply_anon_convert_source_override（覆盖 source + 剥 preview_mode）；
    **不**中和克隆、**不**强制 service_mode、**不**自设 job_id（避 HIGH#2）。扫剥注释代码。"""
    b = _d7_block_code()
    assert "_apply_anon_convert_source_override" in b, "D7 块经 helper 覆盖 source"
    # **不**强制 smart、**不**中和克隆 → 克隆按各模式正常付费流程触发
    assert 'service_mode"] = "smart"' not in b, "D7 不强制 smart（用户自选 mode）"
    assert '"auto_voice_clone": False' not in b, "D7 不再强制 no-clone（克隆走正常流程）"
    assert 'pop("express_consent"' not in b, "D7 不剥 express_consent（express 自动克隆照常）"
    assert 'pop("voice_strategy"' not in b, "D7 不剥 voice_strategy（克隆策略照常）"
    assert 'pop("smart_consent"' not in b, "D7 不剥 smart_consent（智能版克隆照常）"
    # **不**自设 job_id（HIGH#2：不与 600-reserve 的 idempotency_key job_id 抢）。
    # 注：用精确标记 _anon_convert_job_id（已删变量），不能用 "anon_convert"（会误匹配
    # helper 名 _apply_anon_convert_source_override）。
    assert "_anon_convert_job_id" not in b, "D7 不自设确定性 job_id"
    assert "_acquire_convert_singleflight_lock" not in b, "D7 不用 advisory lock"


def test_apply_anon_convert_override_behavior():
    """行为级（CodeX P3）：_apply_anon_convert_source_override 覆盖 source（server 派生）
    + 剥 preview_mode/reuse 字段；**保留** smart_consent/express_consent/voice_strategy/
    voice（克隆走正常付费流程，2026-06-16 拍板）+ 不改 service_mode。"""
    import job_intercept as ji

    resolution = SimpleNamespace(
        source_type="local_video",
        source_ref="/opt/x/uploads/anonymous/sess/u123_full.mp4",
    )
    rd = {
        "reuse_anonymous_preview_id": "prv_x",
        "service_mode": "smart",
        "preview_mode": True,
        "smart_consent": {"auto_voice_clone": True, "auto_retranslate": False},
        "express_consent": {"auto_voice_clone": True},
        "voice_strategy": "free_voiceclone",
        "voice_a": "v1",
    }
    ji._apply_anon_convert_source_override(rd, resolution)
    # source 被 server 派生覆盖（完整原始视频）
    assert rd["source"] == {
        "type": "local_video",
        "value": "/opt/x/uploads/anonymous/sess/u123_full.mp4",
    }
    # 剥 preview_mode（防跳分钟透支）+ reuse 字段
    assert "preview_mode" not in rd
    assert "reuse_anonymous_preview_id" not in rd
    # **保留**全部克隆相关字段（克隆走各模式正常付费流程）
    assert rd["smart_consent"] == {"auto_voice_clone": True, "auto_retranslate": False}
    assert rd["express_consent"] == {"auto_voice_clone": True}
    assert rd["voice_strategy"] == "free_voiceclone"
    assert rd["voice_a"] == "v1"
    # 不改 service_mode（用户自选）
    assert rd["service_mode"] == "smart"


def test_d7_block_gate_auth_ambiguity():
    b = _d7_block_src()
    assert "anonymous_preview_claim_enabled" in b, "gate 同认领旗"
    assert "reuse_request_ambiguous" in b, "同时指定两 reuse key → 拒"
    assert "user is None" in b and "auth_required" in b, "未登录 → 401"
    assert "resolve_anonymous_preview_reuse" in b


# ---------------------------------------------------------------------------
# 3. 红线守卫：resolver 纯读 + 不触 clone/settle（AST import 扫描）
# ---------------------------------------------------------------------------


def test_resolver_no_clone_settle_imports():
    tree = ast.parse(_PRS.read_text(encoding="utf-8"))
    banned = ("minimax", "tts_generator", "settle", "mirror_job", "voiceclone")
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        for m in mods:
            for ban in banned:
                assert ban not in m.lower(), f"preview_reuse_service 不应 import {m!r}"


# ---------------------------------------------------------------------------
# 4. A 方案 pre-flight 时长闸（plan 2026-06-16 转化漏斗 UX）
# ---------------------------------------------------------------------------


def test_resolve_surfaces_probed_source_duration(monkeypatch, tmp_path):
    """resolver 成功 → 把**完整源全长**（重探）放进 resolution.source_duration_seconds。"""
    uid = uuid.uuid4()
    f = tmp_path / "u123_video.mp4"
    h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path, probe_duration=11905.71)
    db = _make_db(_record(uid=uid, source_hash=h, stored_path=f))
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert reason is None and res is not None
    assert res.source_duration_seconds == 11905.71


def test_resolve_duration_none_when_probe_fails(monkeypatch, tmp_path):
    """探测失败（None）→ resolution 仍成功，duration=None（闸跳过、管线兜底，不误拒）。"""
    uid = uuid.uuid4()
    f = tmp_path / "u123_video.mp4"
    h = _write_file(f)
    prs = _resolver(monkeypatch, root=tmp_path, probe_duration=None)
    db = _make_db(_record(uid=uid, source_hash=h, stored_path=f))
    res, reason = _run(prs.resolve_anonymous_preview_reuse(db, user_id=uid, preview_id="prv_x"))
    assert reason is None and res is not None
    assert res.source_duration_seconds is None


def test_probe_helper_swallows_failure(monkeypatch, tmp_path):
    """_probe_source_duration_seconds：ok=False / 异常 / 非正值 → None（绝不抛）。"""
    import preview_reuse_service as prs

    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")

    # 桩 anonymous_preview_probe.probe_source（_probe_source_duration_seconds 的 lazy
    # import 目标 —— 调用时按属性解析，故 monkeypatch 模块属性即可生效）。
    import anonymous_preview_probe as app

    # ok=False → None
    monkeypatch.setattr(app, "probe_source", lambda _p: {"ok": False, "duration_seconds": None})
    assert _run(prs._probe_source_duration_seconds(f)) is None
    # ok=True 但非正 → None
    monkeypatch.setattr(app, "probe_source", lambda _p: {"ok": True, "duration_seconds": 0})
    assert _run(prs._probe_source_duration_seconds(f)) is None
    # ok=True 正值 → 返回
    monkeypatch.setattr(app, "probe_source", lambda _p: {"ok": True, "duration_seconds": 642.0})
    assert _run(prs._probe_source_duration_seconds(f)) == 642.0
    # 抛异常 → None（吞）
    def _boom(_p):
        raise RuntimeError("ffprobe blew up")
    monkeypatch.setattr(app, "probe_source", _boom)
    assert _run(prs._probe_source_duration_seconds(f)) is None


def test_over_duration_cap_pure_helper():
    """_anon_convert_over_duration_cap 纯判定（行为级）：admin / 未知 / 边界 / 超限。"""
    import job_intercept as ji

    # 超限（11min > 10）→ 返回 (src_min, cap)
    over = ji._anon_convert_over_duration_cap(660.0, 10, is_admin=False)
    assert over == (11.0, 10.0)
    # 远超（11905.71s ≈ 198.4min > 10）
    over2 = ji._anon_convert_over_duration_cap(11905.71, 10, is_admin=False)
    assert over2 is not None and round(over2[0], 1) == 198.4 and over2[1] == 10.0
    # 未超（5min < 10）→ None
    assert ji._anon_convert_over_duration_cap(300.0, 10, is_admin=False) is None
    # 恰好等于 cap（10min == 10）→ None（严格 >）
    assert ji._anon_convert_over_duration_cap(600.0, 10, is_admin=False) is None
    # admin → None（豁免，即便超限）
    assert ji._anon_convert_over_duration_cap(99999.0, 10, is_admin=True) is None
    # 源时长未知（None）→ None（探测失败放行，管线兜底）
    assert ji._anon_convert_over_duration_cap(None, 10, is_admin=False) is None
    # cap 未知（None）→ None
    assert ji._anon_convert_over_duration_cap(660.0, None, is_admin=False) is None
    # 更高 plan cap（45min）下 11min 不超 → None
    assert ji._anon_convert_over_duration_cap(660.0, 45, is_admin=False) is None


def test_d7_block_has_duration_preflight_gate():
    """D7 块结构守卫：pre-flight 时长闸调两档判定 + 渲染 helper + plan-tier/自助 cap。

    扫**剥注释**的块（``_d7_block_code()``，CodeX 评审 #3）——避免断言被注释里的同名
    字符串满足、删真分支留注释仍绿。reason 字面量已移入 _anon_convert_duration_error_
    response，由 test_anon_convert_duration_error_response_* 行为级锁定（CodeX 评审 #4）。
    """
    b = _d7_block_code()
    assert "_anon_convert_duration_block" in b, "D7 块须调两档时长判定 helper"
    assert "_anon_convert_duration_error_response" in b, "渲染须经可测的纯 helper（#3/#4）"
    assert "source_duration_seconds" in b, "须用 resolver 重探的源全长"
    assert "get_effective_plan_gate" in b, "cap 取自 plan-tier（trial-aware）"
    assert "max_duration_minutes" in b, "cap = plan max_duration_minutes"
    assert "max_self_serve_duration_minutes" in b, "须算最高自助套餐阈值用于分流（CodeX P1）"
    assert "minimum_self_serve_plan_for" in b, "升级须具名推荐能处理该时长的最低套餐（P1 延伸）"


def test_max_self_serve_duration_minutes():
    """plan_catalog.max_self_serve_duration_minutes = self_serve 套餐 cap 最大值（Pro=180）。"""
    import plan_catalog as pc

    assert pc.max_self_serve_duration_minutes() == 180
    # 语义：free（self_serve=False）不计入；plus(45)/pro(180) 计入 → 180。
    assert pc.PLANS["free"].self_serve is False
    assert pc.PLANS["pro"].max_duration_minutes == 180


def test_minimum_self_serve_plan_for():
    """能处理给定时长的**最低**自助套餐——具名推荐，避免误导买仍跑不了的套餐（CodeX 评审 #1）。"""
    import plan_catalog as pc

    assert pc.minimum_self_serve_plan_for(5) == ("Plus", 45), "5min → 最低 Plus"
    assert pc.minimum_self_serve_plan_for(45) == ("Plus", 45), "恰好 45min → Plus（cap≥）"
    # 关键：45<源≤180 只有 Pro 能处理 → 推荐 Pro 而非 Plus（P1 误导根因）
    assert pc.minimum_self_serve_plan_for(46) == ("Pro", 180), "46min → 跳过 Plus(45)，Pro"
    assert pc.minimum_self_serve_plan_for(100) == ("Pro", 180), "100min → Pro"
    assert pc.minimum_self_serve_plan_for(180) == ("Pro", 180), "恰好 180 → Pro"
    # 超过最高自助套餐 → None（与 max_self_serve_duration_minutes 边界一致）
    assert pc.minimum_self_serve_plan_for(181) is None
    assert pc.minimum_self_serve_plan_for(pc.max_self_serve_duration_minutes() + 1) is None


def test_anon_convert_duration_block_two_tier():
    """_anon_convert_duration_block 两档分流（CodeX P1）：≤ 最高自助套餐=可升级；
    > 最高自助套餐=升无可升；admin / 未知 / 未超 = 放行。"""
    import job_intercept as ji

    SS = 180  # 最高自助套餐 cap（Pro）
    # free 用户(cap 10)、30min → 超 cap 但 ≤180 → 可升级
    r = ji._anon_convert_duration_block(1800.0, 10, SS, is_admin=False)
    assert r is not None and r[0] == "duration_upgrade_required" and r[1] == 30.0 and r[2] == 10.0
    # CodeX 截图场景：free 用户、198.4min（11905.71s）> 180 → 升无可升
    r2 = ji._anon_convert_duration_block(11905.71, 10, SS, is_admin=False)
    assert r2 is not None and r2[0] == "duration_over_max_plan" and round(r2[1], 1) == 198.4
    # pro 用户(cap 180)、198min > 180 → 同样升无可升（pro 已是最高自助，无可升）
    r3 = ji._anon_convert_duration_block(11905.71, 180, SS, is_admin=False)
    assert r3 is not None and r3[0] == "duration_over_max_plan"
    # 恰好等于最高自助 cap（180min）→ 未超严格 >，但 user cap 也要看：free 用户 180>10
    # 超 user cap、且 180 不 > 180（严格）→ 可升级（升到 pro 恰好够）
    r4 = ji._anon_convert_duration_block(180 * 60.0, 10, SS, is_admin=False)
    assert r4 is not None and r4[0] == "duration_upgrade_required"
    # 未超 user cap（5min < 10）→ None
    assert ji._anon_convert_duration_block(300.0, 10, SS, is_admin=False) is None
    # admin → None（豁免）
    assert ji._anon_convert_duration_block(99999.0, 10, SS, is_admin=True) is None
    # 源时长未知 → None（探测失败放行）
    assert ji._anon_convert_duration_block(None, 10, SS, is_admin=False) is None
    # max_self_serve 不可信（0 / None）→ 超 cap 时保守归类为可升级（给 /pricing 路径）
    assert ji._anon_convert_duration_block(11905.71, 10, 0, is_admin=False)[0] == "duration_upgrade_required"
    assert ji._anon_convert_duration_block(11905.71, 10, None, is_admin=False)[0] == "duration_upgrade_required"


def test_anon_convert_duration_error_response_emits_contract():
    """行为级锁定 helper→emitted body.error 字面量契约（CodeX 评审 #4）+ 具名推荐文案
    （CodeX 评审 #1）。前端 readDurationBlockReason 正是 key 这两个 error 字面量。"""
    import json
    import job_intercept as ji

    # over_max（198min > 180）：error=duration_over_max_plan、文案提"联系客服"、不含套餐名/recommended_plan。
    resp = ji._anon_convert_duration_error_response(
        ("duration_over_max_plan", 198.4, 10),
        max_self_serve_minutes=180,
        recommended_plan=None,
        plan_code="free",
        requested_mode="express",
    )
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["error"] == "duration_over_max_plan", "前端 readDurationBlockReason 据此 key"
    assert "联系客服" in body["message"] and "180" in body["message"]
    assert "recommended_plan" not in body["detail"], "升无可升不推荐套餐"

    # upgrade 且只有 Pro 能处理（100min，free cap 10）：具名 Pro/180，**不**提 Plus（P1 误导根因）。
    resp2 = ji._anon_convert_duration_error_response(
        ("duration_upgrade_required", 100.0, 10),
        max_self_serve_minutes=180,
        recommended_plan=("Pro", 180),
        plan_code="free",
        requested_mode="express",
    )
    assert resp2.status_code == 403
    body2 = json.loads(resp2.body)
    assert body2["error"] == "duration_upgrade_required"
    assert "Pro" in body2["message"] and "180" in body2["message"]
    assert "Plus" not in body2["message"], "100min Plus(45) 处理不了 → 文案不得提 Plus（CodeX P1）"
    assert body2["detail"]["recommended_plan"] == "Pro"
    assert body2["detail"]["recommended_plan_minutes"] == 180

    # upgrade 且 Plus 够（30min）：具名 Plus/45。
    resp3 = ji._anon_convert_duration_error_response(
        ("duration_upgrade_required", 30.0, 10),
        max_self_serve_minutes=180,
        recommended_plan=("Plus", 45),
        plan_code="free",
        requested_mode="express",
    )
    body3 = json.loads(resp3.body)
    assert body3["error"] == "duration_upgrade_required"
    assert "Plus" in body3["message"] and "45" in body3["message"]

    # recommended_plan=None（配置异常兜底）：通用文案，不崩、不具名。
    resp4 = ji._anon_convert_duration_error_response(
        ("duration_upgrade_required", 100.0, 10),
        max_self_serve_minutes=0,
        recommended_plan=None,
        plan_code="free",
        requested_mode="express",
    )
    body4 = json.loads(resp4.body)
    assert body4["error"] == "duration_upgrade_required"
    assert "升级套餐" in body4["message"]
    assert "recommended_plan" not in body4["detail"]


def test_anon_convert_duration_error_response_via_param():
    """``via`` 默认 ``anonymous_preview_convert``（D7），smart 预览转完整路径可覆盖为
    ``smart_preview_convert``——两路径复用同一两档渲染（reason 字面量契约一致），仅 detail
    的 ``via`` 区分来源（观测/审计）。这是 finding #2 两路径对称复用的契约锚点。"""
    import json
    import job_intercept as ji

    block = ("duration_over_max_plan", 198.4, 10)
    # 默认（D7）→ anonymous_preview_convert
    resp = ji._anon_convert_duration_error_response(
        block, max_self_serve_minutes=180, recommended_plan=None,
        plan_code="free", requested_mode="express",
    )
    assert json.loads(resp.body)["detail"]["via"] == "anonymous_preview_convert"
    # smart 预览转完整覆盖 → smart_preview_convert；reason 字面量不变（前端单一 mapper）
    resp2 = ji._anon_convert_duration_error_response(
        block, max_self_serve_minutes=180, recommended_plan=None,
        plan_code="free", requested_mode="smart", via="smart_preview_convert",
    )
    body2 = json.loads(resp2.body)
    assert body2["detail"]["via"] == "smart_preview_convert"
    assert body2["error"] == "duration_over_max_plan", "reason 契约与 D7 一致"

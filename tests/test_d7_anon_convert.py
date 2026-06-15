"""D7 — 匿名预览转完整（resolve_anonymous_preview_reuse + intercept_create_job 块）.

plan 2026-06-15-anonymous-preview-claim-binding-plan.md §6.5 / D7。

认领后用**完整原始源** audit.stored_upload_path（非 teaser）建正式计费 job。复用既有
smart-preview-reuse 模式：前端送 reuse_anonymous_preview_id，server 校验所有权
（claim_user_id==user）+ 路径/hash（在上传根内/非 teaser/sha256 匹配 source_hash）→
覆盖 source → 走既有计费 create。**不**强制 service_mode、不复用 voice、无 600 结转。
**categorically 不自动克隆**：系统性中和三条 auto-clone lane（smart 强制 no-clone
consent / express 剥 express_consent / free 剥 voice_strategy+free_consent）。

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


def _resolver(monkeypatch, *, root: Path):
    """import resolver + monkeypatch _anon_upload_root → root（使 tmp 文件视为在上传根内）。"""
    import preview_reuse_service as prs

    monkeypatch.setattr(prs, "_anon_upload_root", lambda: root.resolve())
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
    src = _JI.read_text(encoding="utf-8")
    start = src.find("D7 匿名预览转完整")
    assert start != -1, "intercept_create_job 未找到 D7 块"
    end = src.find("PR#3C-b3g", start)
    assert end != -1, "D7 块结束锚点未找到"
    return src[start:end]


def _d7_block_code() -> str:
    """剥注释后的 D7 块代码（本块字符串内无 #，故按行 split('#') 安全）——
    用于负向断言（避免注释提及 smart_consent/voice_a 等被误判）。"""
    return "\n".join(line.split("#", 1)[0] for line in _d7_block_src().splitlines())


def test_d7_block_reads_field_and_overrides_source():
    b = _d7_block_src()
    assert "reuse_anonymous_preview_id" in b
    assert "resolve_anonymous_preview_reuse" in b
    assert 'request_data["source"]' in b
    assert 'request_data.pop("reuse_anonymous_preview_id"' in b


def test_d7_block_neutralizes_preview_and_clone():
    """D7 **categorically 不自动克隆**：系统性中和全部三条 auto-clone lane（复审 HIGH×2
    + CodeX express P1）。扫剥注释后代码。"""
    b = _d7_block_code()
    assert 'service_mode"] = "smart"' not in b, "D7 不得强制 smart（用户自选 mode）"
    # ① preview_mode（HIGH#1：否则 smart+preview_mode 跳分钟预扣跑 full→透支）
    assert 'pop("preview_mode"' in b, "D7 必须剥 preview_mode（非预览）"
    # ② smart MiniMax：强制 no-clone consent（HIGH#2）
    assert '"auto_voice_clone": False' in b, "D7 smart 模式必须强制 no-clone consent"
    assert '"auto_voice_clone": True' not in b, "D7 绝不授权自动克隆"
    assert "smart_state" not in b, "D7 无 600 结转 marker"
    # ③ express CosyVoice：剥 express_consent（CodeX P1）
    assert 'pop("express_consent"' in b, "D7 必须剥 express_consent（防 express 自动克隆）"
    # ④ free MiMo voiceclone：剥 voice_strategy（→ preset_mapping）+ free_consent + ref
    assert 'pop("voice_strategy"' in b, "D7 必须剥 voice_strategy（防 free voiceclone）"
    assert 'pop("free_consent"' in b
    assert 'pop("voiceclone_reference_path"' in b
    # 不复用 voice（用户自选 preset）
    assert 'pop("voice_a"' in b


def test_d7_block_gate_auth_ambiguity_singleflight():
    b = _d7_block_src()
    assert "anonymous_preview_claim_enabled" in b, "gate 同认领旗"
    assert "reuse_request_ambiguous" in b, "同时指定两 reuse key → 拒"
    assert "user is None" in b and "auth_required" in b, "未登录 → 401"
    assert "anon_convert:" in b, "确定性 job_id seed 用 anon_convert 前缀（与 smart 隔离）"
    assert "_acquire_convert_singleflight_lock" in b
    assert "_idempotent_convert_job_response" in b


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

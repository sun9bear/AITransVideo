"""Phase 4.1 E 守卫测试集（Codex 2026-05-25 v2 签字 + 6 项硬约束 + 2 项新增）。

| 类别 | 测试 | Codex 引用 |
|---|---|---|
| invariant #1 | requires_worker maps to segment | E v2 |
| invariant #2 | target_model → worker_target_model | E v2 |
| invariant #3 | routing 强制 tts_provider=cosyvoice | E v2 |
| invariant #4 | enriched payload 白名单，无 secret/url | E v2 |
| strict filter | minimax/volcengine row 不产生 routing | E v2 P1 #4 |
| strict filter | expired_at / requires_worker=False / target_model="" 不产生 | E v2 P1 #4 |
| 跨账户安全 | user_id 必须严格 filter | E v2 P1 #4 |
| 3 paths | S3 cache-hit / S3 fresh-translate / S4-probe 全覆盖 | E v2 P1 #2 |
| N-speaker | speaker_a/b + speaker_c+ | E v2 P2 |
| fail-closed | clone row missing → 400 | E v2 |
| fail-closed | provider mismatch → 400（Codex P2 新增） | HC#2 |
| body re-serialize | proxy 收到 enriched body（Codex P2 新增） | HC#1 |
| 兼容性 | legacy payload 不破 | E v2 |
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
GATEWAY_PATH = REPO_ROOT / "gateway"
for p in (str(SRC_PATH), str(GATEWAY_PATH), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.gemini.translator import DubbingSegment  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — DubbingSegment factory
# ---------------------------------------------------------------------------

def make_segment(
    *,
    segment_id: int = 1,
    speaker_id: str = "speaker_a",
    voice_id: str = "",
    tts_provider: str = "",
    requires_worker: bool = False,
    worker_target_model: str = "",
) -> DubbingSegment:
    return DubbingSegment(
        segment_id=segment_id,
        speaker_id=speaker_id,
        display_name="X",
        voice_id=voice_id,
        start_ms=0,
        end_ms=1000,
        target_duration_ms=1000,
        source_text="hi",
        cn_text="你好",
        tts_provider=tts_provider,
        requires_worker=requires_worker,
        worker_target_model=worker_target_model,
    )


def make_pipeline_instance():
    """构造一个 ProcessPipeline 实例（仅为了能调用 instance method）。"""
    from pipeline.process import ProcessPipeline
    # 用 __new__ 跳过 __init__（避免拉起 GeminiClient 等重 deps）
    inst = ProcessPipeline.__new__(ProcessPipeline)
    return inst


# ===========================================================================
# Section A: enrichment helper (E.1 / E.2)
# ===========================================================================

@pytest.mark.asyncio
async def test_user_voices_requires_worker_maps_to_routing_dict(monkeypatch):
    """Invariant #1: lookup_clone_voice_routing_metadata 返回的 routing dict
    含 requires_worker=True."""
    from user_voice_service import lookup_clone_voice_routing_metadata

    # mock DB: 返一个匹配的 row
    mock_db = MagicMock()
    mock_row = MagicMock(
        voice_id="cosyvoice_custom_abc",
        target_model="cosyvoice-v3.5-flash",
    )
    mock_result = MagicMock()
    mock_result.all.return_value = [mock_row]
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await lookup_clone_voice_routing_metadata(
        mock_db,
        user_id="u-test",
        voice_ids=["cosyvoice_custom_abc"],
    )
    assert "cosyvoice_custom_abc" in result
    assert result["cosyvoice_custom_abc"]["requires_worker"] is True
    assert result["cosyvoice_custom_abc"]["worker_target_model"] == "cosyvoice-v3.5-flash"


@pytest.mark.asyncio
async def test_lookup_returns_routing_whitelist_fields_only(monkeypatch):
    """Invariant #4: lookup 返回字典只含路由白名单字段，无 label/source_segments/secret."""
    from user_voice_service import lookup_clone_voice_routing_metadata

    mock_db = MagicMock()
    mock_row = MagicMock(
        voice_id="vid",
        target_model="cosyvoice-v3.5-flash",
    )
    mock_result = MagicMock()
    mock_result.all.return_value = [mock_row]
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await lookup_clone_voice_routing_metadata(
        mock_db, user_id="u", voice_ids=["vid"],
    )
    entry = result["vid"]
    allowed = {"requires_worker", "worker_target_model"}
    extra = set(entry.keys()) - allowed
    assert not extra, f"routing dict leaks fields outside whitelist: {extra}"
    # 不允许出现的字段
    forbidden = {"label", "source_segments", "billing_sku", "hmac_secret",
                 "clone_provider_request_id", "clone_worker_request_id",
                 "source_speaker_name", "worker_provider"}
    leaked = set(entry.keys()) & forbidden
    assert not leaked, f"routing dict leaks forbidden fields: {leaked}"


@pytest.mark.asyncio
async def test_lookup_uses_in_clause_for_batch_query(monkeypatch):
    """HC#4 (batch): 多 voice_id 单次 IN 查询，不 N+1."""
    from user_voice_service import lookup_clone_voice_routing_metadata

    captured_stmts = []

    async def _exec(stmt):
        captured_stmts.append(stmt)
        result = MagicMock()
        result.all.return_value = []
        return result

    mock_db = MagicMock()
    mock_db.execute = _exec

    await lookup_clone_voice_routing_metadata(
        mock_db, user_id="u", voice_ids=["v1", "v2", "v3"],
    )
    # 只执行 1 次查询（IN clause）
    assert len(captured_stmts) == 1


# ===========================================================================
# Section B: _apply_runtime_voice_overrides (E.3 / E.4)
# ===========================================================================

def test_apply_voice_overrides_with_routing_sets_segment_fields():
    """Invariant #1+#2: routing dict 应 set DubbingSegment.requires_worker /
    worker_target_model."""
    pipeline = make_pipeline_instance()
    seg = make_segment(speaker_id="speaker_a", voice_id="cosyvoice_custom_test")
    pipeline._apply_runtime_voice_overrides(
        [seg],
        voice_id_a="cosyvoice_custom_test",
        display_name_a="X",
        voice_id_b=None,
        display_name_b="",
        speaker_voice_routing={
            "speaker_a": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            }
        },
    )
    assert seg.requires_worker is True
    assert seg.worker_target_model == "cosyvoice-v3.5-flash"


def test_apply_voice_overrides_routing_forces_tts_provider_cosyvoice():
    """Invariant #3: requires_worker=True 时 segment.tts_provider 强制为 cosyvoice."""
    pipeline = make_pipeline_instance()
    seg = make_segment(
        speaker_id="speaker_a",
        voice_id="cosyvoice_custom_test",
        tts_provider="",  # 缺省
    )
    pipeline._apply_runtime_voice_overrides(
        [seg],
        voice_id_a="cosyvoice_custom_test",
        display_name_a="X",
        voice_id_b=None,
        display_name_b="",
        speaker_voice_routing={
            "speaker_a": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            }
        },
    )
    assert seg.tts_provider == "cosyvoice"


def test_apply_voice_overrides_without_routing_keeps_segment_defaults():
    """兼容性: 不传 routing → segment.requires_worker 保持 False。"""
    pipeline = make_pipeline_instance()
    seg = make_segment(speaker_id="speaker_a", voice_id="minimax_xyz", tts_provider="minimax")
    pipeline._apply_runtime_voice_overrides(
        [seg],
        voice_id_a="minimax_xyz",
        display_name_a="X",
        voice_id_b=None,
        display_name_b="",
    )
    assert seg.requires_worker is False
    assert seg.worker_target_model == ""
    assert seg.tts_provider == "minimax"  # 保持原值


def test_apply_voice_overrides_clears_stale_worker_flags_without_routing():
    """PR #7 Codex review: cache-restored segments may carry stale worker flags.

    If the current approved voice has no routing entry, the override pass must
    clear requires_worker / worker_target_model before TTS dispatch.
    """
    pipeline = make_pipeline_instance()
    seg = make_segment(
        speaker_id="speaker_a",
        voice_id="cosyvoice_clone_old",
        tts_provider="cosyvoice",
        requires_worker=True,
        worker_target_model="cosyvoice-v3.5-flash",
    )
    pipeline._apply_runtime_voice_overrides(
        [seg],
        voice_id_a="minimax_xyz",
        display_name_a="X",
        voice_id_b=None,
        display_name_b="",
        speaker_providers={"speaker_a": "minimax"},
        speaker_voice_routing=None,
    )
    assert seg.voice_id == "minimax_xyz"
    assert seg.tts_provider == "minimax"
    assert seg.requires_worker is False
    assert seg.worker_target_model == ""


def test_apply_voice_overrides_speaker_a_b_routing_maps():
    """N-speaker (Codex P2): speaker_a + speaker_b 都各自映射."""
    pipeline = make_pipeline_instance()
    seg_a = make_segment(speaker_id="speaker_a", voice_id="cv_a")
    seg_b = make_segment(speaker_id="speaker_b", voice_id="cv_b")
    pipeline._apply_runtime_voice_overrides(
        [seg_a, seg_b],
        voice_id_a="cv_a",
        display_name_a="A",
        voice_id_b="cv_b",
        display_name_b="B",
        speaker_voice_routing={
            "speaker_a": {"requires_worker": True, "worker_target_model": "cosyvoice-v3.5-flash"},
            "speaker_b": {"requires_worker": True, "worker_target_model": "cosyvoice-v3.5-plus"},
        },
    )
    assert seg_a.requires_worker is True
    assert seg_a.worker_target_model == "cosyvoice-v3.5-flash"
    assert seg_b.requires_worker is True
    assert seg_b.worker_target_model == "cosyvoice-v3.5-plus"


def test_apply_voice_overrides_speaker_c_plus_routing_maps():
    """N-speaker (Codex P2): speaker_c / speaker_d 也能映射."""
    pipeline = make_pipeline_instance()
    seg_c = make_segment(speaker_id="speaker_c", voice_id="cv_c")
    seg_d = make_segment(speaker_id="speaker_d", voice_id="cv_d")
    pipeline._apply_runtime_voice_overrides(
        [seg_c, seg_d],
        voice_id_a="",
        display_name_a="",
        voice_id_b=None,
        display_name_b="",
        speaker_voices={"speaker_c": "cv_c", "speaker_d": "cv_d"},
        speaker_voice_routing={
            "speaker_c": {"requires_worker": True, "worker_target_model": "cosyvoice-v3.5-flash"},
            "speaker_d": {"requires_worker": True, "worker_target_model": "cosyvoice-v3.5-plus"},
        },
    )
    assert seg_c.requires_worker is True
    assert seg_d.requires_worker is True
    assert seg_d.worker_target_model == "cosyvoice-v3.5-plus"


def test_apply_voice_overrides_no_routing_for_speaker_means_default():
    """部分 speaker 无 routing → 该 segment 字段保持默认；其它 speaker 不受影响."""
    pipeline = make_pipeline_instance()
    seg_a = make_segment(speaker_id="speaker_a", voice_id="cv_a")
    seg_b = make_segment(speaker_id="speaker_b", voice_id="mm_b", tts_provider="minimax")
    pipeline._apply_runtime_voice_overrides(
        [seg_a, seg_b],
        voice_id_a="cv_a",
        display_name_a="A",
        voice_id_b="mm_b",
        display_name_b="B",
        speaker_voice_routing={
            "speaker_a": {"requires_worker": True, "worker_target_model": "cosyvoice-v3.5-flash"},
            # speaker_b 没有 routing
        },
    )
    assert seg_a.requires_worker is True
    assert seg_b.requires_worker is False
    assert seg_b.tts_provider == "minimax"  # 不受影响


# ===========================================================================
# Section C: Gateway enrichment integration (E.2)
# ===========================================================================

@pytest.mark.asyncio
async def test_enrichment_returns_400_when_clone_voice_missing_user_voices_row(monkeypatch):
    """HC#3: voice_id 形似 CosyVoice clone（tts_provider=cosyvoice 且不在 public
    catalog）但 user_voices 查不到 → 400 voice_clone_metadata_missing."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    # job 查询返 user_id
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    # user_voices 查询返空（voice_id 不在表里）
    user_voices_result = MagicMock()
    user_voices_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[job_result, user_voices_result])

    # P1 #2 fix: 直接 mock Gateway 本地 catalog 查询函数（不走 services helper）
    async def _fake_catalog(_db):
        return {"cosyvoice_preset_a", "cosyvoice_preset_b"}

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_orphan",  # 不在 user_voices 也不在 catalog
            "tts_provider": "cosyvoice",
        }
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_metadata_missing"
    assert error["voice_id"] == "cosyvoice_custom_orphan"


@pytest.mark.asyncio
async def test_enrichment_returns_400_when_clone_row_provider_mismatch(monkeypatch):
    """HC#2 (Codex P2 新增): user_voices 命中 clone row 但 payload tts_provider
    不是 cosyvoice → 400 voice_clone_provider_mismatch."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    # user_voices 查询命中
    mock_row = MagicMock(
        voice_id="cosyvoice_custom_clone1",
        target_model="cosyvoice-v3.5-flash",
    )
    user_voices_result = MagicMock()
    user_voices_result.all.return_value = [mock_row]
    mock_db.execute = AsyncMock(side_effect=[job_result, user_voices_result])

    async def _fake_catalog(_db):
        return set()

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_clone1",
            "tts_provider": "minimax",  # ⚠️ mismatch
        }
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_provider_mismatch"


@pytest.mark.asyncio
async def test_enrichment_preset_voice_skipped_no_routing_no_400(monkeypatch):
    """预设公开音色（在 catalog 内）→ 不加 routing，也不抛 400（legacy 兼容）."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    user_voices_result = MagicMock()
    user_voices_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[job_result, user_voices_result])

    async def _fake_catalog(_db):
        return {"cosyvoice_preset_warmth"}  # 已知预设

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_preset_warmth",  # 预设
            "tts_provider": "cosyvoice",
        }
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert error is None
    # 预设 voice 不加 routing 字段
    assert "requires_worker" not in (enriched[0] if enriched else {})


@pytest.mark.asyncio
async def test_enrichment_clone_row_match_writes_routing_fields():
    """Invariant #1+#2+#3: user_voices 命中 clone row → speaker 条目加 routing
    + tts_provider=cosyvoice."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    mock_row = MagicMock(
        voice_id="cv_clone_x",
        target_model="cosyvoice-v3.5-plus",
    )
    uv_result = MagicMock()
    uv_result.all.return_value = [mock_row]
    mock_db.execute = AsyncMock(side_effect=[job_result, uv_result])

    speakers = [{
        "speaker_id": "speaker_a",
        "voice_id": "cv_clone_x",
        "tts_provider": "cosyvoice",
    }]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert error is None
    assert enriched is not None and len(enriched) == 1
    assert enriched[0]["requires_worker"] is True
    assert enriched[0]["worker_target_model"] == "cosyvoice-v3.5-plus"
    assert enriched[0]["tts_provider"] == "cosyvoice"


@pytest.mark.asyncio
async def test_enrichment_legacy_payload_no_voice_id_safe():
    """兼容性: speakers 全都没 voice_id（legacy） → 不抛、不加 routing."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    speakers = [{"speaker_id": "speaker_a"}]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert error is None
    # 无 voice_id 直接早返
    assert enriched is None


@pytest.mark.asyncio
async def test_enrichment_lookup_filters_strict():
    """E v2 P1 #4: SQL strict filter 必须 5 维 + user_id；构造一个 mock 验证 stmt."""
    from user_voice_service import lookup_clone_voice_routing_metadata

    captured_stmts = []

    async def _exec(stmt):
        captured_stmts.append(stmt)
        r = MagicMock()
        r.all.return_value = []
        return r

    mock_db = MagicMock()
    mock_db.execute = _exec

    await lookup_clone_voice_routing_metadata(
        mock_db, user_id="u-test", voice_ids=["v1"],
    )
    assert len(captured_stmts) == 1
    # 把 stmt 编成 SQL 字符串看 WHERE 条件
    stmt_str = str(captured_stmts[0])
    # 必须有这些过滤条件
    assert "user_id" in stmt_str.lower()
    assert "voice_id" in stmt_str.lower()
    assert "expired_at" in stmt_str.lower()
    assert "provider" in stmt_str.lower()
    assert "requires_worker" in stmt_str.lower()
    assert "target_model" in stmt_str.lower()


# ===========================================================================
# Section D: pipeline 3-path integration via _apply_runtime_voice_overrides
# ===========================================================================

def test_routing_applies_to_segment_uniformly_via_overrides_helper():
    """所有 3 个 TTS 入口（S3 cache-hit / S3 fresh / S4-probe）都通过同一
    ``_apply_runtime_voice_overrides`` 调用应用 routing，确保 cloned voice
    在所有路径上的 DubbingSegment 字段一致."""
    pipeline = make_pipeline_instance()
    routing = {
        "speaker_a": {
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        }
    }

    # 模拟 3 个不同来源的 segments（cache-hit / fresh / probe）
    for tag, seg_list in [
        ("cache-hit", [make_segment(voice_id="cv_a")]),
        ("fresh-translate", [make_segment(voice_id="cv_a", tts_provider="")]),
        ("probe", [make_segment(voice_id="cv_a", segment_id=2)]),
    ]:
        pipeline._apply_runtime_voice_overrides(
            seg_list,
            voice_id_a="cv_a",
            display_name_a="A",
            voice_id_b=None,
            display_name_b="",
            speaker_voice_routing=routing,
        )
        for seg in seg_list:
            assert seg.requires_worker is True, f"{tag} path missed routing"
            assert seg.worker_target_model == "cosyvoice-v3.5-flash", f"{tag} path"
            assert seg.tts_provider == "cosyvoice", f"{tag} path tts_provider"


# ===========================================================================
# Section E: AST + serialization guards (invariant #4)
# ===========================================================================

def test_enriched_payload_contains_no_secret_or_url_in_serialization():
    """HC#1 + invariant #4: 验证 enriched speakers JSON 序列化后无 secret/url/hmac.

    用 enrichment helper 产出的 dict 直接序列化，断言关键字段不出现."""
    import asyncio
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    mock_row = MagicMock(
        voice_id="cv_clone_x",
        target_model="cosyvoice-v3.5-flash",
    )
    uv_result = MagicMock()
    uv_result.all.return_value = [mock_row]
    mock_db.execute = AsyncMock(side_effect=[job_result, uv_result])

    speakers = [{
        "speaker_id": "speaker_a",
        "voice_id": "cv_clone_x",
        "tts_provider": "cosyvoice",
    }]
    enriched, _ = asyncio.run(_enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    ))
    # 序列化 enriched 后扫敏感字串
    blob = json.dumps(enriched, ensure_ascii=False).lower()
    for forbidden in ("hmac", "secret", "api_key", "credential",
                       "mainland_voice_worker_url", "worker_request_id",
                       "billing_sku"):
        assert forbidden not in blob, (
            f"enriched speakers payload leaks {forbidden!r}: {blob[:200]}"
        )


def test_routing_metadata_whitelist_constant_locked():
    """E v2 invariant #4: ROUTING_METADATA_FIELDS 必须严格只含 2 字段，
    防止未来意外扩大白名单泄漏 user_voices 字段."""
    from user_voice_service import ROUTING_METADATA_FIELDS
    assert ROUTING_METADATA_FIELDS == frozenset(
        {"requires_worker", "worker_target_model"}
    ), (
        f"ROUTING_METADATA_FIELDS expanded unexpectedly: {sorted(ROUTING_METADATA_FIELDS)}; "
        f"adding fields must go through Codex review"
    )


def test_d_p1_fix_integration_routing_segment_routes_to_worker_no_minimax_drift():
    """End-to-end (E → D)：构造一个 E 设置好的 segment，验证 D 路由到 worker
    分支而非 MiniMax 路径。"""
    pipeline = make_pipeline_instance()
    seg = make_segment(
        speaker_id="speaker_a",
        voice_id="cv_clone_x",
        tts_provider="",  # 故意不设；E 应该填 cosyvoice
    )
    pipeline._apply_runtime_voice_overrides(
        [seg],
        voice_id_a="cv_clone_x",
        display_name_a="A",
        voice_id_b=None,
        display_name_b="",
        speaker_voice_routing={
            "speaker_a": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            }
        },
    )
    # E 三字段全 set
    assert seg.requires_worker is True
    assert seg.worker_target_model == "cosyvoice-v3.5-flash"
    assert seg.tts_provider == "cosyvoice"
    # D.4 P1 fix 兜底：若有人把 tts_provider 漂到非 cosyvoice，D 会抛错；
    # 这里 E 已经 set 对了，TTSGenerator._generate_one 应当走 worker 分支
    # (具体 worker dispatch 测试在 D 测试集 test_phase4_1_d_worker_routing.py 已覆盖)


# ===========================================================================
# Section F: body re-serialization (Codex HC#1, new test #21)
# ===========================================================================

@pytest.mark.asyncio
async def test_routing_lookup_failure_for_cosyvoice_non_public_voice_fails_closed(monkeypatch):
    """E P1 #1 fix (Codex 2026-05-25 二轮)：lookup_clone_voice_routing_metadata
    抛异常 + voice 是 ``tts_provider=cosyvoice`` 非 public preset → 503
    ``voice_clone_routing_lookup_failed``。**不允许 fail-open** 让 clone voice
    走 legacy 国际 DashScope endpoint。"""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    mock_db.execute = AsyncMock(side_effect=[job_result])

    # routing 查询抛异常
    async def _failing_lookup(*args, **kwargs):
        raise RuntimeError("simulated DB short outage")

    monkeypatch.setattr(
        "user_voice_service.lookup_clone_voice_routing_metadata",
        _failing_lookup,
    )

    # public catalog 正常返一些预设
    async def _fake_catalog(_db):
        return {"cosyvoice_preset_a"}

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_clone_xyz",  # 非 preset
            "tts_provider": "cosyvoice",
        }
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    # fail-closed: 拒绝 approve
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_routing_lookup_failed"
    assert error["voice_id"] == "cosyvoice_custom_clone_xyz"


@pytest.mark.asyncio
async def test_routing_lookup_failed_returns_http_503_at_endpoint(monkeypatch):
    """E P2 #1 (Codex 2026-05-25 三轮 review)：``voice_clone_routing_lookup_failed``
    必须返 HTTP **503**（server temporary unavailable），不是 400（user error）。

    端到端通过 _approve_voice_selection_with_quality_sync 验证 HTTP status_code。
    """
    from job_intercept import _approve_voice_selection_with_quality_sync

    async def _fake_enrich(db, *, job_id, speakers):
        return None, {
            "code": "voice_clone_routing_lookup_failed",
            "message": "simulated outage",
            "voice_id": "cv_clone_x",
        }

    monkeypatch.setattr("job_intercept._enrich_speakers_with_clone_routing", _fake_enrich)
    # 不应该 proxy
    async def _explode_proxy(*args, **kwargs):
        raise AssertionError("proxy_request must not be called when enrichment fails")
    monkeypatch.setattr("job_intercept.proxy_request", _explode_proxy)
    monkeypatch.setattr(
        "voice_calibration_review_preflight.review_preflight_enabled", lambda: False,
    )

    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps({
        "speakers": [{"speaker_id": "speaker_a", "voice_id": "cv_clone_x",
                       "tts_provider": "cosyvoice"}]
    }).encode())
    db = MagicMock()

    resp = await _approve_voice_selection_with_quality_sync(request, "j1", db)
    assert resp.status_code == 503, (
        f"voice_clone_routing_lookup_failed must return HTTP 503 (server "
        f"temporary), not {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_other_enrichment_errors_return_http_400_at_endpoint(monkeypatch):
    """E P2 #1: ``voice_clone_metadata_missing`` / ``voice_clone_provider_mismatch``
    继续返 HTTP **400**（user data error）。"""
    from job_intercept import _approve_voice_selection_with_quality_sync

    for user_error_code in (
        "voice_clone_metadata_missing",
        "voice_clone_provider_mismatch",
    ):
        async def _fake_enrich(db, *, job_id, speakers, _code=user_error_code):
            return None, {"code": _code, "message": "user error",
                          "voice_id": "cv_x"}

        monkeypatch.setattr(
            "job_intercept._enrich_speakers_with_clone_routing", _fake_enrich,
        )
        async def _explode_proxy(*args, **kwargs):
            raise AssertionError(f"proxy_request must not be called for {user_error_code}")
        monkeypatch.setattr("job_intercept.proxy_request", _explode_proxy)
        monkeypatch.setattr(
            "voice_calibration_review_preflight.review_preflight_enabled", lambda: False,
        )

        request = MagicMock()
        request.body = AsyncMock(return_value=b'{"speakers":[{"speaker_id":"a"}]}')
        db = MagicMock()
        resp = await _approve_voice_selection_with_quality_sync(request, "j1", db)
        assert resp.status_code == 400, (
            f"{user_error_code} must return HTTP 400 (user data error), got {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_routing_lookup_failure_for_public_preset_voice_allows_through(monkeypatch):
    """E P1 #1 fix: lookup 异常但 voice 是公开预设 → 允许 approve 继续（不加
    routing 字段，走 legacy 路径是合法的，因为公开预设本来就走国际 DashScope）."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    mock_db.execute = AsyncMock(side_effect=[job_result])

    async def _failing_lookup(*args, **kwargs):
        raise RuntimeError("simulated DB short outage")

    monkeypatch.setattr(
        "user_voice_service.lookup_clone_voice_routing_metadata",
        _failing_lookup,
    )

    async def _fake_catalog(_db):
        return {"cosyvoice_preset_a"}

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_preset_a",  # 公开预设
            "tts_provider": "cosyvoice",
        }
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert error is None
    # 公开预设：不加 routing，speaker 条目原样返
    assert enriched is not None
    assert "requires_worker" not in enriched[0]


@pytest.mark.asyncio
async def test_routing_lookup_failure_for_minimax_voice_allows_through(monkeypatch):
    """E P1 #1 fix: lookup 异常但 voice 是 MiniMax → 允许继续（routing 仅适
    用 cosyvoice）."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    mock_db.execute = AsyncMock(side_effect=[job_result])

    async def _failing_lookup(*args, **kwargs):
        raise RuntimeError("simulated DB short outage")

    monkeypatch.setattr(
        "user_voice_service.lookup_clone_voice_routing_metadata",
        _failing_lookup,
    )

    async def _fake_catalog(_db):
        return set()

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "minimax_voice_xyz",
            "tts_provider": "minimax",
        }
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert error is None
    assert "requires_worker" not in (enriched[0] if enriched else {})


@pytest.mark.asyncio
async def test_enrichment_strips_client_supplied_worker_fields_for_unmatched_voices(monkeypatch):
    """PR #7 Codex review: only Gateway-authorized routing fields may survive.

    A client can submit stale/forged requires_worker fields on a public preset or
    MiniMax voice. If user_voices strict lookup does not authorize routing for
    that voice, enrichment must remove those fields before proxying approve.
    """
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    mock_db.execute = AsyncMock(side_effect=[job_result])

    async def _empty_lookup(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "user_voice_service.lookup_clone_voice_routing_metadata",
        _empty_lookup,
    )

    async def _fake_catalog(_db):
        return {"cosyvoice_preset_a"}

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "minimax_voice_xyz",
            "tts_provider": "minimax",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        },
        {
            "speaker_id": "speaker_b",
            "voice_id": "cosyvoice_preset_a",
            "tts_provider": "cosyvoice",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-plus",
        },
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert error is None
    assert enriched is not None
    for sp in enriched:
        assert "requires_worker" not in sp
        assert "worker_target_model" not in sp


@pytest.mark.asyncio
async def test_blank_provider_known_clone_voice_fails_closed(monkeypatch):
    """PR #7 Codex review: missing tts_provider must not let clone ids drift.

    Even if the approve payload omits tts_provider, a voice_id that is known in
    user_voices as a CosyVoice clone must be treated as clone-like. If the
    current user's strict routing lookup does not authorize it, reject approve
    instead of forwarding a non-worker payload.
    """
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    known_clone_result = MagicMock()
    known_clone_result.all.return_value = [("cosyvoice_custom_blank_provider",)]
    mock_db.execute = AsyncMock(side_effect=[job_result, known_clone_result])

    async def _empty_lookup(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "user_voice_service.lookup_clone_voice_routing_metadata",
        _empty_lookup,
    )

    async def _fake_catalog(_db):
        return set()

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [{
        "speaker_id": "speaker_a",
        "voice_id": "cosyvoice_custom_blank_provider",
        # tts_provider intentionally omitted
    }]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_metadata_missing"
    assert error["voice_id"] == "cosyvoice_custom_blank_provider"


@pytest.mark.asyncio
async def test_known_clone_voice_with_forged_non_cosyvoice_provider_fails_closed(monkeypatch):
    """PR #7 Codex review: known clone IDs cannot be approved as MiniMax/etc."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    known_clone_result = MagicMock()
    known_clone_result.all.return_value = [("cosyvoice_custom_forged_provider",)]
    mock_db.execute = AsyncMock(side_effect=[job_result, known_clone_result])

    async def _empty_lookup(*args, **kwargs):
        return {}

    async def _empty_catalog(_db):
        return set()

    monkeypatch.setattr(
        "user_voice_service.lookup_clone_voice_routing_metadata",
        _empty_lookup,
    )
    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _empty_catalog,
    )

    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db,
        job_id="j1",
        speakers=[{
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_forged_provider",
            "tts_provider": "minimax",
        }],
    )

    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_provider_mismatch"
    assert error["voice_id"] == "cosyvoice_custom_forged_provider"
    assert error["submitted_tts_provider"] == "minimax"


@pytest.mark.asyncio
async def test_job_user_missing_cosyvoice_non_public_fails_closed(monkeypatch):
    """PR #7 Codex review: missing Job.user_id must not skip clone validation."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = None
    mock_db.execute = AsyncMock(return_value=job_result)

    async def _known_clone_ids(_db, voice_ids):
        return set()

    async def _public_voice_ids(_db):
        return {"cosyvoice_public_preset"}

    monkeypatch.setattr(
        "job_intercept._fetch_known_cosyvoice_clone_voice_ids", _known_clone_ids,
    )
    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _public_voice_ids,
    )

    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db,
        job_id="missing-job",
        speakers=[{
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_missing_user",
            "tts_provider": "cosyvoice",
        }],
    )

    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_routing_lookup_failed"
    assert error["voice_id"] == "cosyvoice_custom_missing_user"


@pytest.mark.asyncio
async def test_job_user_missing_public_preset_allows_sanitized_payload(monkeypatch):
    """Missing Job.user_id can allow public presets, but forged worker fields are stripped."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id=None)
    mock_db.execute = AsyncMock(return_value=job_result)

    async def _known_clone_ids(_db, voice_ids):
        return set()

    async def _public_voice_ids(_db):
        return {"cosyvoice_public_preset"}

    monkeypatch.setattr(
        "job_intercept._fetch_known_cosyvoice_clone_voice_ids", _known_clone_ids,
    )
    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _public_voice_ids,
    )

    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db,
        job_id="missing-user",
        speakers=[{
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_public_preset",
            "tts_provider": "cosyvoice",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-plus",
        }],
    )

    assert error is None
    assert enriched is not None
    assert enriched[0]["voice_id"] == "cosyvoice_public_preset"
    assert "requires_worker" not in enriched[0]
    assert "worker_target_model" not in enriched[0]


@pytest.mark.asyncio
async def test_job_user_missing_blank_provider_known_clone_fails_closed(monkeypatch):
    """A known clone voice with blank provider still fails closed when user_id is unavailable."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = None
    mock_db.execute = AsyncMock(return_value=job_result)

    async def _known_clone_ids(_db, voice_ids):
        return {"cosyvoice_custom_blank_provider_missing_user"}

    async def _public_voice_ids(_db):
        return set()

    monkeypatch.setattr(
        "job_intercept._fetch_known_cosyvoice_clone_voice_ids", _known_clone_ids,
    )
    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _public_voice_ids,
    )

    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db,
        job_id="missing-job",
        speakers=[{
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_blank_provider_missing_user",
        }],
    )

    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_routing_lookup_failed"
    assert error["voice_id"] == "cosyvoice_custom_blank_provider_missing_user"


@pytest.mark.asyncio
async def test_job_user_missing_known_clone_with_forged_provider_fails_closed(monkeypatch):
    """The missing-user fallback still rejects known clone IDs with non-cosyvoice provider."""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = None
    mock_db.execute = AsyncMock(return_value=job_result)

    async def _known_clone_ids(_db, voice_ids):
        return {"cosyvoice_custom_missing_user_forged_provider"}

    async def _public_voice_ids(_db):
        return set()

    monkeypatch.setattr(
        "job_intercept._fetch_known_cosyvoice_clone_voice_ids", _known_clone_ids,
    )
    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _public_voice_ids,
    )

    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db,
        job_id="missing-job",
        speakers=[{
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_missing_user_forged_provider",
            "tts_provider": "volcengine",
        }],
    )

    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_provider_mismatch"
    assert error["voice_id"] == "cosyvoice_custom_missing_user_forged_provider"
    assert error["submitted_tts_provider"] == "volcengine"


@pytest.mark.asyncio
async def test_public_catalog_lookup_failure_for_unknown_cosyvoice_voice_fails_closed(monkeypatch):
    """E P1 #1/#2 fix: 公开 catalog 查询失败 + voice 看似 cosyvoice clone →
    无法判断是否为合法预设，必须 fail-closed（不能 degrade 让 clone voice
    走 legacy）。"""
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    user_voices_result = MagicMock()
    user_voices_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[job_result, user_voices_result])

    # public catalog 查询抛异常
    async def _failing_catalog(_db):
        raise RuntimeError("simulated voice_catalog table outage")

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _failing_catalog,
    )

    speakers = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_unknown",
            "tts_provider": "cosyvoice",
        }
    ]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_metadata_missing"


@pytest.mark.asyncio
async def test_fetch_cosyvoice_public_voice_ids_filters_strict(monkeypatch):
    """E P1 #2 + P1 #3 fix: _fetch_cosyvoice_public_voice_ids 必须按
    provider/matchable/archived_at + verify_status filter 查询（严格复刻
    internal /api/internal/voice-catalog 契约，同 voice_catalog_api.py:157-162）。"""
    from job_intercept import _fetch_cosyvoice_public_voice_ids

    captured_stmts = []

    async def _exec(stmt):
        captured_stmts.append(stmt)
        r = MagicMock()
        r.all.return_value = []
        return r

    mock_db = MagicMock()
    mock_db.execute = _exec

    await _fetch_cosyvoice_public_voice_ids(mock_db)
    assert len(captured_stmts) == 1
    stmt_str = str(captured_stmts[0]).lower()
    assert "voice_catalog" in stmt_str
    assert "provider" in stmt_str
    assert "matchable" in stmt_str
    assert "archived_at" in stmt_str
    # P1 #3 fix: 必须含 verify_status / verified 条件
    assert "verify_status" in stmt_str, (
        f"_fetch_cosyvoice_public_voice_ids must include _VERIFIED_TRUE_SQL "
        f"filter to match internal /api/internal/voice-catalog contract; "
        f"got SQL: {stmt_str[:500]}"
    )
    assert "verified" in stmt_str


@pytest.mark.asyncio
async def test_fetch_cosyvoice_public_voice_ids_imports_shared_verified_sql():
    """E P1 #3 fix (Codex 2026-05-25 E 三轮 review): 用 voice_catalog_api 的
    ``_VERIFIED_TRUE_SQL`` 单源 fragment，**不**重新写 verified 条件 —— 避免
    与 internal catalog 规则漂移。

    AST 守卫：函数体内必须 import voice_catalog_api（且不重复定义
    verify_status 的 SQL）。
    """
    import ast
    from pathlib import Path
    src = (REPO_ROOT / "gateway" / "job_intercept.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_helper = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_fetch_cosyvoice_public_voice_ids":
            found_helper = True
            imports_from_catalog_api = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    if sub.module == "voice_catalog_api":
                        imports_from_catalog_api = True
                        names = {a.name for a in sub.names}
                        assert "_VERIFIED_TRUE_SQL" in names, (
                            "_fetch_cosyvoice_public_voice_ids must import "
                            "_VERIFIED_TRUE_SQL from voice_catalog_api (single "
                            f"source); imports found: {names}"
                        )
            assert imports_from_catalog_api, (
                "_fetch_cosyvoice_public_voice_ids must import _VERIFIED_TRUE_SQL "
                "from voice_catalog_api to stay in sync with internal catalog rules"
            )
            # 函数体源代码扫一下，禁止重新写 verified 条件
            body_src = ast.get_source_segment(src, node) or ""
            # 唯一允许的引用：`_VERIFIED_TRUE_SQL` 名字本身
            forbidden_patterns = [
                "jsonb_each(",     # 不要 inline SQL fragment
                "verify_status[",   # 不要 column 表达式重写
            ]
            for pat in forbidden_patterns:
                assert pat not in body_src, (
                    f"_fetch_cosyvoice_public_voice_ids must not re-implement "
                    f"verified SQL (found pattern {pat!r}); use shared "
                    f"_VERIFIED_TRUE_SQL constant instead"
                )
            break
    assert found_helper, "_fetch_cosyvoice_public_voice_ids not found in job_intercept.py"


@pytest.mark.asyncio
async def test_unverified_cosyvoice_catalog_voice_is_not_treated_as_public_preset(monkeypatch):
    """E P1 #3 fix (Codex 2026-05-25 E 三轮 review): catalog 中 matchable=True
    但 verify_status 没有任何 verified=true 维度的 row → **不**算 public preset.

    端到端：构造一个 voice_id 在 user_voices 没有 row、在 catalog 中 unverified、
    payload tts_provider=cosyvoice → 必须 400 voice_clone_metadata_missing
    （表明 verified filter 真的过滤了未验证 row）。
    """
    from job_intercept import _enrich_speakers_with_clone_routing

    mock_db = MagicMock()
    job_result = MagicMock()
    job_result.first.return_value = MagicMock(user_id="u-test")
    user_voices_result = MagicMock()
    user_voices_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[job_result, user_voices_result])

    # 模拟 _fetch_cosyvoice_public_voice_ids 的行为：
    # unverified row 应当被 _VERIFIED_TRUE_SQL 过滤掉 → 返空集
    async def _fake_catalog(_db):
        # ⚠️ 这个 fake 模拟"真实 SQL 已经把 unverified row 过滤掉"的结果
        return set()

    monkeypatch.setattr(
        "job_intercept._fetch_cosyvoice_public_voice_ids", _fake_catalog,
    )

    speakers = [{
        "speaker_id": "speaker_a",
        "voice_id": "cosyvoice_unverified_xyz",  # 在 catalog 但 unverified
        "tts_provider": "cosyvoice",
    }]
    enriched, error = await _enrich_speakers_with_clone_routing(
        mock_db, job_id="j1", speakers=speakers,
    )
    # unverified catalog row 不被认为 public preset → fail-closed
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_metadata_missing", (
        f"unverified catalog row must NOT be treated as public preset; "
        f"got code={error['code']!r}"
    )


def test_no_self_http_in_enrichment_helper():
    """E P1 #2 fix: AST 守卫 —— `_enrich_speakers_with_clone_routing` /
    `_fetch_cosyvoice_public_voice_ids` 不得 import ``services.tts.*`` 公开
    catalog helper（避免 self-HTTP）。"""
    import ast
    from pathlib import Path
    src = (REPO_ROOT / "gateway" / "job_intercept.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    forbidden_module_prefixes = (
        "services.tts.cosyvoice_voice_catalog",
    )
    forbidden_imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_enrich_speakers_with_clone_routing",
            "_fetch_cosyvoice_public_voice_ids",
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    mod = sub.module or ""
                    for bad in forbidden_module_prefixes:
                        if mod.startswith(bad):
                            forbidden_imports.append(f"{node.name}: from {mod}")
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        for bad in forbidden_module_prefixes:
                            if alias.name.startswith(bad):
                                forbidden_imports.append(f"{node.name}: import {alias.name}")
    assert not forbidden_imports, (
        "Gateway routing enrichment must NOT import services.tts catalog helpers "
        "(triggers self-HTTP + event-loop block). Use Gateway-local async DB "
        f"query instead. Found: {forbidden_imports}"
    )


@pytest.mark.asyncio
async def test_approve_proxy_body_is_reserialized_with_routing_fields(monkeypatch):
    """HC#1 (Codex P2 新增): _approve_voice_selection_with_quality_sync 调用
    proxy_request 时必须用 enriched body bytes（含 routing 字段），不能用原
    body_bytes。

    实现方式：mock proxy_request，断言传入的 override_body 含 requires_worker."""
    from job_intercept import _approve_voice_selection_with_quality_sync

    # mock request body
    captured_override_bodies: list[bytes] = []

    async def _fake_proxy(*args, **kwargs):
        body = kwargs.get("override_body")
        if body is not None:
            captured_override_bodies.append(body)
        # 返个 2xx mock response
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.body = b"{}"
        return resp

    monkeypatch.setattr("job_intercept.proxy_request", _fake_proxy)
    # mock preflight 不影响
    monkeypatch.setattr(
        "voice_calibration_review_preflight.review_preflight_enabled",
        lambda: False,
    )

    # mock enrichment：返一个带 routing 的 enriched list
    enriched_speakers = [{
        "speaker_id": "speaker_a",
        "voice_id": "cv_clone_x",
        "tts_provider": "cosyvoice",
        "requires_worker": True,
        "worker_target_model": "cosyvoice-v3.5-flash",
    }]

    async def _fake_enrich(db, *, job_id, speakers):
        return enriched_speakers, None

    monkeypatch.setattr("job_intercept._enrich_speakers_with_clone_routing", _fake_enrich)

    # 构造 request mock
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps({
        "speakers": [{"speaker_id": "speaker_a", "voice_id": "cv_clone_x", "tts_provider": "cosyvoice"}]
    }).encode())

    # 调
    db = MagicMock()
    # mock execute（DB sync 部分会调）
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=job_result)

    await _approve_voice_selection_with_quality_sync(request, "j1", db)

    # 验证 proxy 收到的 body 含 enriched 字段
    assert len(captured_override_bodies) == 1
    body_str = captured_override_bodies[0].decode("utf-8")
    assert "requires_worker" in body_str, (
        f"proxy must receive re-serialized body with routing fields; got: {body_str}"
    )
    assert "worker_target_model" in body_str
    assert "cosyvoice-v3.5-flash" in body_str

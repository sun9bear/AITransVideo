"""Phase 4.3a PR1-D1 — is_temporary 隔离矩阵行为测试（spec §6.4 / Codex P1-2）。

用真 in-memory aiosqlite DB 验证 4 个函数对 ``is_temporary=True`` 行的处置：

| 函数 | 默认 (include_temporary=False) | 关键不变量 |
|---|---|---|
| list_user_voices | **隐藏**临时音色 | UI "我的音色" 不显示 |
| count_active_voices_for_user_and_provider | **不计**临时音色 | 长期库配额不被挤占 |
| match_user_voices | **不复用**临时音色 | Smart 跨任务不误用 |
| lookup_clone_voice_routing_metadata | **必须包含**临时音色 | 本任务 segment TTS 正确路由（**不动**） |

D1 最关键的反向 sanity：``lookup_clone_voice_routing_metadata`` **绝不**
过滤 ``is_temporary`` —— 否则 Express 刚克隆出的临时音色在 segment
持久化时查不到 routing，TTS 会回退官方预设音色（Codex D1 review 重点）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import UserVoice  # noqa: E402
from user_voice_service import (  # noqa: E402
    count_active_voices_for_user_and_provider,
    list_user_voices,
    lookup_clone_voice_routing_metadata,
    match_user_voices,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_session() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: UserVoice.__table__.create(sync))
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# 固定 user / source hash，方便 match_user_voices 同源命中
_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_SOURCE_HASH = "youtube:phase43a_d1_fixture"


def _make_voice(
    *,
    voice_id: str,
    is_temporary: bool,
    provider: str = "cosyvoice_voice_clone",
    tts_provider: str = "cosyvoice",
    requires_worker: bool = True,
    target_model: str = "cosyvoice-v3.5-flash",
    source_content_hash: str | None = _SOURCE_HASH,
    source_speaker_name: str | None = "Alice",
    source_speaker_name_key: str | None = "alice",
    expired_at: datetime | None = None,
) -> UserVoice:
    return UserVoice(
        id=uuid.uuid4(),
        user_id=_USER_ID,
        voice_id=voice_id,
        voice_type="cloned",
        provider=provider,
        tts_provider=tts_provider,
        platform="dashscope_mainland",
        label=voice_id,
        source_speaker_id="speaker_a",
        source_content_hash=source_content_hash,
        source_speaker_name=source_speaker_name,
        source_speaker_name_key=source_speaker_name_key,
        created_from="express_auto" if is_temporary else "studio_manual",
        expired_at=expired_at,
        region_constraint="mainland_only",
        requires_worker=requires_worker,
        target_model=target_model,
        worker_provider="cosyvoice",
        worker_region="cn-wuhan",
        is_temporary=is_temporary,
        temporary_expires_at=(
            datetime(2026, 6, 4, tzinfo=timezone.utc) if is_temporary else None
        ),
        created_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
    )


async def _seed_three_voices(session_maker) -> None:
    """V1 长期 cosyvoice / V2 临时 cosyvoice / V3 长期 minimax。"""
    async with session_maker() as db:
        db.add(_make_voice(voice_id="v1_longterm_cosy", is_temporary=False))
        db.add(_make_voice(voice_id="v2_temp_cosy", is_temporary=True))
        db.add(
            _make_voice(
                voice_id="v3_longterm_minimax",
                is_temporary=False,
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                requires_worker=False,
                target_model=None,
            )
        )
        await db.commit()


# ---------------------------------------------------------------------------
# list_user_voices
# ---------------------------------------------------------------------------


def test_list_user_voices_default_hides_temporary():
    async def _t():
        sm = await _make_session()
        await _seed_three_voices(sm)
        async with sm() as db:
            voices = await list_user_voices(db, _USER_ID)
        ids = {v.voice_id for v in voices}
        # V2 临时音色不出现
        assert "v2_temp_cosy" not in ids, "临时音色不应出现在默认列表"
        assert "v1_longterm_cosy" in ids
        assert "v3_longterm_minimax" in ids
    _run(_t())


def test_list_user_voices_include_temporary_returns_all():
    async def _t():
        sm = await _make_session()
        await _seed_three_voices(sm)
        async with sm() as db:
            voices = await list_user_voices(db, _USER_ID, include_temporary=True)
        ids = {v.voice_id for v in voices}
        assert ids == {"v1_longterm_cosy", "v2_temp_cosy", "v3_longterm_minimax"}, (
            "include_temporary=True 应返回全部 active 音色"
        )
    _run(_t())


# ---------------------------------------------------------------------------
# count_active_voices_for_user_and_provider
# ---------------------------------------------------------------------------


def test_count_active_cosyvoice_default_excludes_temporary():
    async def _t():
        sm = await _make_session()
        await _seed_three_voices(sm)
        async with sm() as db:
            count = await count_active_voices_for_user_and_provider(
                db, _USER_ID, provider="cosyvoice_voice_clone"
            )
        # 只 V1（长期），不含 V2（临时）
        assert count == 1, f"默认应只数 1 个长期 cosyvoice 音色，实际 {count}"
    _run(_t())


def test_count_active_cosyvoice_include_temporary_counts_both():
    async def _t():
        sm = await _make_session()
        await _seed_three_voices(sm)
        async with sm() as db:
            count = await count_active_voices_for_user_and_provider(
                db, _USER_ID, provider="cosyvoice_voice_clone",
                include_temporary=True,
            )
        # V1 + V2 = 2
        assert count == 2, f"include_temporary=True 应数 2 个，实际 {count}"
    _run(_t())


# ---------------------------------------------------------------------------
# match_user_voices
# ---------------------------------------------------------------------------


def test_match_user_voices_default_excludes_temporary():
    async def _t():
        sm = await _make_session()
        await _seed_three_voices(sm)
        async with sm() as db:
            matches = await match_user_voices(
                db,
                user_id=_USER_ID,
                source_content_hash=_SOURCE_HASH,
                source_speaker_id="speaker_a",
                source_speaker_name="Alice",
                provider="cosyvoice_voice_clone",
                tts_provider="cosyvoice",
                platform="dashscope_mainland",
            )
        matched_ids = {m.voice.voice_id for m in matches}
        assert "v2_temp_cosy" not in matched_ids, (
            "Smart auto-reuse / candidate 默认不应复用临时音色（跨任务隔离）"
        )
    _run(_t())


def test_match_user_voices_include_temporary_can_match():
    async def _t():
        sm = await _make_session()
        await _seed_three_voices(sm)
        async with sm() as db:
            matches = await match_user_voices(
                db,
                user_id=_USER_ID,
                source_content_hash=_SOURCE_HASH,
                source_speaker_id="speaker_a",
                source_speaker_name="Alice",
                provider="cosyvoice_voice_clone",
                tts_provider="cosyvoice",
                platform="dashscope_mainland",
                include_temporary=True,
            )
        matched_ids = {m.voice.voice_id for m in matches}
        assert "v2_temp_cosy" in matched_ids, (
            "include_temporary=True 应能匹配临时音色"
        )
    _run(_t())


def test_match_user_voices_cross_source_default_excludes_temporary():
    """跨源路径也必须排除临时音色（spec §6.4 两个 SELECT 子句都要过滤）。"""
    async def _t():
        sm = await _make_session()
        # 一个临时音色，hash 与查询不同（强制走 cross-source 路径）
        async with sm() as db:
            db.add(_make_voice(
                voice_id="v_temp_crosssource",
                is_temporary=True,
                source_content_hash="youtube:DIFFERENT_source",
                source_speaker_name="Bob",
                source_speaker_name_key="bob",
            ))
            await db.commit()
        async with sm() as db:
            matches = await match_user_voices(
                db,
                user_id=_USER_ID,
                source_content_hash="youtube:current_job",
                source_speaker_name="Bob",
                source_speaker_name_key="bob",
                provider="cosyvoice_voice_clone",
                tts_provider="cosyvoice",
                platform="dashscope_mainland",
                include_cross_source=True,
            )
        matched_ids = {m.voice.voice_id for m in matches}
        assert "v_temp_crosssource" not in matched_ids, (
            "跨源 candidate 默认也不应复用临时音色"
        )
    _run(_t())


# ---------------------------------------------------------------------------
# lookup_clone_voice_routing_metadata — D1 关键反向 sanity
# ---------------------------------------------------------------------------


def test_lookup_clone_voice_routing_metadata_INCLUDES_temporary():
    """**D1 最关键不变量**（Codex D1 review 重点）：routing lookup **必须**
    返回临时音色的 routing 元数据。

    Express 刚克隆出的临时音色（is_temporary=True）正是当前任务 segment
    需要走 worker 路由的音色。如果这里像 list/count/match 一样过滤掉临时
    音色，segment 持久化时查不到 routing → requires_worker 落 False →
    TTS 回退官方预设音色 → 用户的克隆音色白克隆了。
    """
    async def _t():
        sm = await _make_session()
        await _seed_three_voices(sm)
        async with sm() as db:
            routing = await lookup_clone_voice_routing_metadata(
                db,
                user_id=_USER_ID,
                voice_ids=["v1_longterm_cosy", "v2_temp_cosy"],
            )
        # 两个 cosyvoice clone（长期 + 临时）都必须能查到 routing
        assert "v2_temp_cosy" in routing, (
            "routing lookup 必须包含临时音色 —— 否则 Express clone 后 TTS 回退预设"
        )
        assert "v1_longterm_cosy" in routing
        # routing payload 只含白名单 2 字段
        assert routing["v2_temp_cosy"] == {
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        }
    _run(_t())


def test_lookup_routing_metadata_signature_has_no_include_temporary_param():
    """守卫：``lookup_clone_voice_routing_metadata`` **不应**有
    ``include_temporary`` 参数。

    D1 只给 list / count / match 加该参数；routing lookup 故意不加（永远
    包含临时音色）。如果有人未来给它加了该参数（哪怕默认 True），也是
    误导性的，本守卫挡住。
    """
    import inspect
    sig = inspect.signature(lookup_clone_voice_routing_metadata)
    assert "include_temporary" not in sig.parameters, (
        "lookup_clone_voice_routing_metadata 不应有 include_temporary 参数 —— "
        "routing 决策永远要看到临时音色"
    )


# ---------------------------------------------------------------------------
# 新 kwarg 默认值守卫（防 caller 漂移）
# ---------------------------------------------------------------------------


def test_new_kwargs_default_to_false():
    """守卫：三函数的 include_temporary 默认值必须是 False（fail-safe 隔离）。"""
    import inspect
    for fn in (list_user_voices, count_active_voices_for_user_and_provider, match_user_voices):
        sig = inspect.signature(fn)
        assert "include_temporary" in sig.parameters, (
            f"{fn.__name__} 缺少 include_temporary 参数"
        )
        assert sig.parameters["include_temporary"].default is False, (
            f"{fn.__name__}.include_temporary 默认值必须是 False（默认隔离临时音色）"
        )

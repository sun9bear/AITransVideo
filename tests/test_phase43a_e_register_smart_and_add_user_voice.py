"""Phase 4.3a PR1-E — /register-smart 扩展 + add_user_voice 临时字段合同 + 防漂移 400。

锁定 spec §6.3 / §6.3.1 三块合约：

1. **add_user_voice 临时字段合同**（§6.3.1 / Codex 三轮 P1-2）：
   - insert 路径写入 is_temporary / temporary_expires_at
   - existing revive 路径**显式覆盖**（不走 _set_if_empty）
   - is_temporary=False 时**强制** temporary_expires_at=None（防 stale）

2. **/register-smart routing 9 + temporary 2 字段 pass-through**（§6.3）：
   - Express caller 传全部 11 字段 → 写进 user_voices 行
   - Smart MiniMax 旧 caller 不传 → 全默认（backward-compat 字节级）

3. **防漂移 400**（§6.3 / Codex 二轮 P1-6）：
   - cosyvoice_voice_clone provider + created_from 默认 smart_auto → 400
   - Smart MiniMax provider + 默认 smart_auto → 仍正常（不破旧路径）

测试分两层：
- add_user_voice：真 in-memory aiosqlite DB（行为级）
- register-smart endpoint：AST/字面量 静态守卫（endpoint 有深 import 链，
  行为级集成测试留 PR1-H）
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
from user_voice_service import add_user_voice, list_user_voices  # noqa: E402


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


_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ee")
_EXPIRES = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _same_instant(stored, expected) -> bool:
    """Compare datetimes tolerating sqlite tz-naive roundtrip.

    sqlite (test in-memory DB) does NOT persist tzinfo even for
    DateTime(timezone=True) columns; real Postgres does. So a value
    written as 2026-06-04T12:00+00:00 comes back naive 2026-06-04T12:00.
    Compare on the wall-clock instant, stripping tzinfo on both sides.
    """
    if stored is None or expected is None:
        return stored is expected
    s = stored.replace(tzinfo=None) if stored.tzinfo else stored
    e = expected.replace(tzinfo=None) if expected.tzinfo else expected
    return s == e


# ===========================================================================
# add_user_voice 临时字段合同（§6.3.1）
# ===========================================================================


def test_add_user_voice_insert_writes_is_temporary_true_with_expires_at():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            voice = await add_user_voice(
                db,
                user_id=_USER_ID,
                voice_id="v_temp",
                label="Temp",
                provider="cosyvoice_voice_clone",
                created_from="express_auto",
                is_temporary=True,
                temporary_expires_at=_EXPIRES,
            )
        assert voice.is_temporary is True
        assert _same_instant(voice.temporary_expires_at, _EXPIRES)
    _run(_t())


def test_add_user_voice_insert_non_temp_keeps_expires_at_none():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            voice = await add_user_voice(
                db,
                user_id=_USER_ID,
                voice_id="v_longterm",
                label="LongTerm",
                provider="cosyvoice_voice_clone",
                created_from="studio_manual",
                is_temporary=False,
            )
        assert voice.is_temporary is False
        assert voice.temporary_expires_at is None
    _run(_t())


def test_add_user_voice_non_temp_forces_expires_at_none_even_if_caller_passes_ts():
    """防御性：caller bug 同时传 is_temporary=False + ts → 强制清成 None。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            voice = await add_user_voice(
                db,
                user_id=_USER_ID,
                voice_id="v_bug",
                label="Bug",
                provider="cosyvoice_voice_clone",
                created_from="studio_manual",
                is_temporary=False,
                temporary_expires_at=_EXPIRES,  # caller bug
            )
        assert voice.is_temporary is False
        assert voice.temporary_expires_at is None, (
            "is_temporary=False 时必须强制 temporary_expires_at=None（防 stale）"
        )
    _run(_t())


def test_add_user_voice_existing_revive_overwrites_is_temporary_to_true():
    """existing revive：原 row is_temporary=False，新 caller 传 True → 覆盖。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            await add_user_voice(
                db, user_id=_USER_ID, voice_id="v_revive",
                label="orig", provider="cosyvoice_voice_clone",
                created_from="studio_manual", is_temporary=False,
            )
        async with sm() as db:
            voice = await add_user_voice(
                db, user_id=_USER_ID, voice_id="v_revive",
                label="updated", provider="cosyvoice_voice_clone",
                created_from="express_auto", is_temporary=True,
                temporary_expires_at=_EXPIRES,
            )
        assert voice.is_temporary is True
        assert _same_instant(voice.temporary_expires_at, _EXPIRES)
    _run(_t())


def test_add_user_voice_existing_revive_clears_stale_temporary_expires_at():
    """existing revive：原 row is_temporary=True+ts，新 caller 传 False
    → 升级为长期，temporary_expires_at 必须清成 None（spec §6.3.1）。
    """
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            await add_user_voice(
                db, user_id=_USER_ID, voice_id="v_upgrade",
                label="temp", provider="cosyvoice_voice_clone",
                created_from="express_auto", is_temporary=True,
                temporary_expires_at=_EXPIRES,
            )
        async with sm() as db:
            voice = await add_user_voice(
                db, user_id=_USER_ID, voice_id="v_upgrade",
                label="saved", provider="cosyvoice_voice_clone",
                created_from="studio_manual", is_temporary=False,
            )
        assert voice.is_temporary is False
        assert voice.temporary_expires_at is None, (
            "临时音色升级为长期时必须清 temporary_expires_at（防 stale 触发 sweeper）"
        )
    _run(_t())


def test_add_user_voice_default_is_temporary_false_backward_compat():
    """守卫：不传 is_temporary 时默认 False（Smart MiniMax 旧 caller 行为不变）。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            voice = await add_user_voice(
                db, user_id=_USER_ID, voice_id="v_legacy",
                label="legacy", provider="minimax_voice_clone",
                # 不传 is_temporary / temporary_expires_at
            )
        assert voice.is_temporary is False
        assert voice.temporary_expires_at is None
    _run(_t())


def test_temporary_voice_hidden_from_list_after_add():
    """端到端：add 临时音色后，list_user_voices 默认不返（D1 + E 联动）。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            await add_user_voice(
                db, user_id=_USER_ID, voice_id="v_e2e_temp",
                label="temp", provider="cosyvoice_voice_clone",
                created_from="express_auto", is_temporary=True,
                temporary_expires_at=_EXPIRES,
            )
        async with sm() as db:
            visible = await list_user_voices(db, _USER_ID)
            all_voices = await list_user_voices(db, _USER_ID, include_temporary=True)
        assert "v_e2e_temp" not in {v.voice_id for v in visible}
        assert "v_e2e_temp" in {v.voice_id for v in all_voices}
    _run(_t())


# ===========================================================================
# /register-smart endpoint 静态守卫（§6.3 + 防漂移 400）
# ===========================================================================


def _read_user_voice_api() -> str:
    return (Path(_gateway_dir) / "user_voice_api.py").read_text(encoding="utf-8")


def test_register_smart_passes_routing_and_temporary_fields():
    """守卫：register-smart endpoint 必须把 routing 9 + temporary 2 字段
    pass-through 给 add_user_voice。
    """
    src = _read_user_voice_api()
    # NB: requires_worker / is_temporary / target_model 在 E-fix 后改为
    # 校验后变量（requires_worker=requires_worker / target_model=target_model_raw
    # / is_temporary=is_temporary），不再是内联 bool(body.get(...))。
    # 严格 bool + target_model 校验由 test_phase43a_e_fix_*.py 行为测试覆盖。
    required_passthrough = [
        'region_constraint=str(body.get("region_constraint")',
        'requires_worker=requires_worker,',
        'target_model=target_model_raw,',
        'worker_provider=body.get("worker_provider")',
        'worker_region=body.get("worker_region")',
        'clone_api_model=body.get("clone_api_model")',
        'billing_sku=body.get("billing_sku")',
        'clone_provider_request_id=body.get("clone_provider_request_id")',
        'clone_worker_request_id=body.get("clone_worker_request_id")',
        'is_temporary=is_temporary,',
        # review-fix-2 P2-2：temporary_expires_at 改用校验后变量
        # （is_temporary=true 时强制合法 datetime，否则 400）
        'temporary_expires_at=parsed_temporary_expires_at,',
    ]
    for snippet in required_passthrough:
        assert snippet in src, (
            f"register-smart endpoint 缺少 pass-through: {snippet!r}"
        )


def test_register_smart_anti_drift_400_for_cosyvoice_with_default_created_from():
    """守卫：防漂移 400 必须存在（cosyvoice_voice_clone + smart_auto → 400）。"""
    src = _read_user_voice_api()
    assert 'provider == "cosyvoice_voice_clone" and created_from == "smart_auto"' in src, (
        "register-smart 缺少防漂移条件（cosyvoice provider + 默认 smart_auto 应 400）"
    )
    assert '"created_from_required_for_cosyvoice_clone"' in src, (
        "register-smart 缺少防漂移 400 error code"
    )


def test_register_smart_minimax_default_smart_auto_still_allowed():
    """守卫：Smart MiniMax 旧 caller（minimax_voice_clone provider）默认
    smart_auto **不**被防漂移 400 拦住。

    验证防漂移条件只针对 ``provider == "cosyvoice_voice_clone"``，
    minimax_voice_clone 走默认 smart_auto 仍然合法。
    """
    src = _read_user_voice_api()
    # 防漂移条件必须 AND provider==cosyvoice，不能只看 created_from
    assert 'provider == "cosyvoice_voice_clone" and created_from == "smart_auto"' in src, (
        "防漂移条件必须同时检查 provider==cosyvoice_voice_clone —— "
        "否则会误伤 Smart MiniMax 默认 smart_auto 路径"
    )
    # created_from 默认值仍是 smart_auto（不破 Smart 旧 caller）
    assert 'str(body.get("created_from") or "smart_auto")' in src, (
        "created_from 默认值应保持 smart_auto（Smart MiniMax 旧 caller backward-compat）"
    )


def test_add_user_voice_signature_has_temporary_params():
    """守卫：add_user_voice 函数签名必须含 is_temporary / temporary_expires_at。"""
    import inspect
    sig = inspect.signature(add_user_voice)
    assert "is_temporary" in sig.parameters
    assert "temporary_expires_at" in sig.parameters
    assert sig.parameters["is_temporary"].default is False, (
        "is_temporary 默认值必须 False（Smart MiniMax / Studio backward-compat）"
    )
    assert sig.parameters["temporary_expires_at"].default is None

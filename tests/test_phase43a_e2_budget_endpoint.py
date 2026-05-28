"""Phase 4.3a PR1-E2 — Express auto-clone budget endpoint + counters。

锁定 spec §2.5 成本闸两层：
- daily_count：今天发生过的 express_auto clone（不过滤 expired_at / is_temporary）
- active_temp_count：is_temporary=true AND expired_at IS NULL

GET /api/internal/express-auto-clone-budget 返 can_clone + deny_reason。

测试两层：
- count 函数：真 in-memory aiosqlite（行为级）
- endpoint：直接调 handler + fake Request + monkeypatch admin_settings + DB

Codex E2 review 要求覆盖：daily cap / active temp cap / 二者都超优先级 /
soft-deleted temporary 不计 active / expired old daily rows 不计 today。
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    count_active_temporary_voices,
    count_express_auto_clones_today,
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


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
_OTHER_USER = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


def _voice(
    *,
    voice_id: str,
    user_id: uuid.UUID = _USER,
    provider: str = "cosyvoice_voice_clone",
    created_from: str = "express_auto",
    is_temporary: bool = True,
    expired_at: datetime | None = None,
    created_at: datetime | None = None,
) -> UserVoice:
    now = datetime.now(timezone.utc)
    return UserVoice(
        id=uuid.uuid4(),
        user_id=user_id,
        voice_id=voice_id,
        voice_type="cloned",
        provider=provider,
        tts_provider="cosyvoice",
        platform="dashscope_mainland",
        label=voice_id,
        created_from=created_from,
        expired_at=expired_at,
        region_constraint="mainland_only",
        requires_worker=True,
        target_model="cosyvoice-v3.5-flash",
        is_temporary=is_temporary,
        temporary_expires_at=(now + timedelta(days=7)) if is_temporary else None,
        created_at=created_at or now,
        updated_at=now,
    )


# ===========================================================================
# count_express_auto_clones_today
# ===========================================================================


def test_daily_count_counts_today_express_auto():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="t1"))
            db.add(_voice(voice_id="t2"))
            await db.commit()
        async with sm() as db:
            n = await count_express_auto_clones_today(db, _USER)
        assert n == 2
    _run(_t())


def test_daily_count_includes_soft_deleted_and_longterm():
    """daily_count 不过滤 expired_at / is_temporary —— '曾经发生过即算'
    （防用户删临时音色绕过每日限额）。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="active_temp", is_temporary=True, expired_at=None))
            db.add(_voice(  # soft-deleted 仍算
                voice_id="soft_deleted",
                is_temporary=True,
                expired_at=datetime.now(timezone.utc),
            ))
            db.add(_voice(  # 长期（is_temporary=False）也算
                voice_id="longterm", is_temporary=False,
            ))
            await db.commit()
        async with sm() as db:
            n = await count_express_auto_clones_today(db, _USER)
        assert n == 3, "soft-deleted + 长期都应计入 daily_count"
    _run(_t())


def test_daily_count_excludes_yesterday_rows():
    """昨天的 express_auto clone 不计入今天。"""
    async def _t():
        sm = await _make_session()
        yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=2)
        async with sm() as db:
            db.add(_voice(voice_id="today1"))
            db.add(_voice(voice_id="yesterday1", created_at=yesterday))
            await db.commit()
        async with sm() as db:
            n = await count_express_auto_clones_today(db, _USER)
        assert n == 1, "昨天的行不应计入今天 daily_count"
    _run(_t())


def test_daily_count_excludes_other_created_from():
    """只数 created_from='express_auto'，不数 smart_auto / studio_manual。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="express", created_from="express_auto"))
            db.add(_voice(voice_id="smart", created_from="smart_auto"))
            db.add(_voice(voice_id="studio", created_from="studio_manual"))
            await db.commit()
        async with sm() as db:
            n = await count_express_auto_clones_today(db, _USER)
        assert n == 1
    _run(_t())


def test_daily_count_excludes_other_user():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="mine"))
            db.add(_voice(voice_id="theirs", user_id=_OTHER_USER))
            await db.commit()
        async with sm() as db:
            n = await count_express_auto_clones_today(db, _USER)
        assert n == 1
    _run(_t())


def test_daily_count_excludes_non_cosyvoice_provider():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="cosy", provider="cosyvoice_voice_clone"))
            db.add(_voice(voice_id="mm", provider="minimax_voice_clone"))
            await db.commit()
        async with sm() as db:
            n = await count_express_auto_clones_today(db, _USER)
        assert n == 1
    _run(_t())


# ===========================================================================
# count_active_temporary_voices
# ===========================================================================


def test_active_temp_count_only_active_temporary():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="active1", is_temporary=True, expired_at=None))
            db.add(_voice(voice_id="active2", is_temporary=True, expired_at=None))
            await db.commit()
        async with sm() as db:
            n = await count_active_temporary_voices(db, _USER)
        assert n == 2
    _run(_t())


def test_active_temp_count_excludes_soft_deleted():
    """soft-deleted（expired_at 非空）临时音色不计入 active。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="active", is_temporary=True, expired_at=None))
            db.add(_voice(
                voice_id="deleted", is_temporary=True,
                expired_at=datetime.now(timezone.utc),
            ))
            await db.commit()
        async with sm() as db:
            n = await count_active_temporary_voices(db, _USER)
        assert n == 1, "soft-deleted 临时音色不应计入 active_temp_count"
    _run(_t())


def test_active_temp_count_excludes_longterm():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            db.add(_voice(voice_id="temp", is_temporary=True, expired_at=None))
            db.add(_voice(voice_id="longterm", is_temporary=False, expired_at=None))
            await db.commit()
        async with sm() as db:
            n = await count_active_temporary_voices(db, _USER)
        assert n == 1
    _run(_t())


# ===========================================================================
# Endpoint 行为（直接调 handler + fake Request + monkeypatch）
# ===========================================================================


_TEST_KEY = "phase43a-e2-internal-key"


def _make_request(user_id_qs: str, *, internal_key: str = _TEST_KEY):
    return SimpleNamespace(
        headers={"X-Internal-Key": internal_key},
        client=SimpleNamespace(host="127.0.0.1"),
        query_params={"user_id": user_id_qs},
    )


def _call_budget(
    monkeypatch, *, user_id_qs, daily_count=0, active_temp_count=0,
    daily_cap=5, active_temp_cap=3, internal_key=_TEST_KEY,
    admin_raises=False,
):
    import config
    monkeypatch.setattr(config.settings, "internal_api_key", _TEST_KEY, raising=False)

    import user_voice_api
    import admin_settings as _admin_mod

    if admin_raises:
        def _raise():
            raise RuntimeError("admin_settings load failed")
        monkeypatch.setattr(_admin_mod, "load_settings", _raise, raising=True)
    else:
        monkeypatch.setattr(
            _admin_mod, "load_settings",
            lambda: SimpleNamespace(
                express_cosyvoice_auto_clone_per_user_daily_cap=daily_cap,
                express_cosyvoice_auto_clone_per_user_active_temp_cap=active_temp_cap,
            ),
            raising=True,
        )

    monkeypatch.setattr(
        user_voice_api, "count_express_auto_clones_today",
        AsyncMock(return_value=daily_count), raising=True,
    )
    monkeypatch.setattr(
        user_voice_api, "count_active_temporary_voices",
        AsyncMock(return_value=active_temp_count), raising=True,
    )

    request = _make_request(user_id_qs, internal_key=internal_key)
    db = AsyncMock()
    resp = _run(user_voice_api.internal_express_auto_clone_budget(request, db=db))
    return resp.status_code, json.loads(resp.body)


_VALID_UID = "00000000-0000-0000-0000-0000000000e2"


def test_budget_401_wrong_internal_key(monkeypatch):
    status, _ = _call_budget(monkeypatch, user_id_qs=_VALID_UID, internal_key="WRONG")
    assert status == 403  # _internal_access_error 返 403 invalid_internal_key


def test_budget_400_invalid_user_id(monkeypatch):
    status, parsed = _call_budget(monkeypatch, user_id_qs="not-a-uuid")
    assert status == 400
    assert parsed["error"] == "invalid_user_id"


def test_budget_can_clone_when_under_both_caps(monkeypatch):
    status, parsed = _call_budget(
        monkeypatch, user_id_qs=_VALID_UID,
        daily_count=2, active_temp_count=1, daily_cap=5, active_temp_cap=3,
    )
    assert status == 200
    assert parsed["can_clone"] is True
    assert parsed["deny_reason"] is None
    assert parsed["daily_remaining"] == 3
    assert parsed["active_temp_remaining"] == 2


def test_budget_daily_cap_exceeded(monkeypatch):
    status, parsed = _call_budget(
        monkeypatch, user_id_qs=_VALID_UID,
        daily_count=5, active_temp_count=0, daily_cap=5, active_temp_cap=3,
    )
    assert parsed["can_clone"] is False
    assert parsed["deny_reason"] == "daily_cap_exceeded"
    assert parsed["daily_remaining"] == 0


def test_budget_active_temp_cap_exceeded(monkeypatch):
    status, parsed = _call_budget(
        monkeypatch, user_id_qs=_VALID_UID,
        daily_count=0, active_temp_count=3, daily_cap=5, active_temp_cap=3,
    )
    assert parsed["can_clone"] is False
    assert parsed["deny_reason"] == "active_temp_cap_exceeded"


def test_budget_both_exceeded_daily_takes_priority(monkeypatch):
    """二者都超时 → deny_reason 优先 daily_cap_exceeded（固定优先级）。"""
    status, parsed = _call_budget(
        monkeypatch, user_id_qs=_VALID_UID,
        daily_count=10, active_temp_count=10, daily_cap=5, active_temp_cap=3,
    )
    assert parsed["can_clone"] is False
    assert parsed["deny_reason"] == "daily_cap_exceeded", (
        "二者都超时 daily_cap_exceeded 必须优先于 active_temp_cap_exceeded"
    )


def test_budget_cap_zero_means_hard_disabled(monkeypatch):
    """daily_cap=0 → 任何 count 都 >= 0 → 永远 daily_cap_exceeded（admin 紧急关停）。"""
    status, parsed = _call_budget(
        monkeypatch, user_id_qs=_VALID_UID,
        daily_count=0, daily_cap=0, active_temp_cap=3,
    )
    assert parsed["can_clone"] is False
    assert parsed["deny_reason"] == "daily_cap_exceeded"


def test_budget_admin_settings_unavailable(monkeypatch):
    """admin_settings load 失败 → can_clone=false + admin_settings_unavailable（fail-closed）。"""
    status, parsed = _call_budget(
        monkeypatch, user_id_qs=_VALID_UID, admin_raises=True,
    )
    assert status == 200
    assert parsed["can_clone"] is False
    assert parsed["deny_reason"] == "admin_settings_unavailable"


def test_budget_response_shape_complete(monkeypatch):
    """正常响应 shape 含全部 spec §2.5 字段。"""
    status, parsed = _call_budget(
        monkeypatch, user_id_qs=_VALID_UID,
        daily_count=1, active_temp_count=1,
    )
    for key in (
        "ok", "daily_count", "daily_cap", "daily_remaining",
        "active_temp_count", "active_temp_cap", "active_temp_remaining",
        "can_clone", "deny_reason",
    ):
        assert key in parsed, f"budget response 缺字段 {key}"


def test_budget_endpoint_docstring_marks_advisory_not_atomic():
    """守卫（Codex GitHub PR #17 P2-2）：budget endpoint docstring 必须明确
    标注它是 advisory，不是 atomic gate；并指向 PR2 atomic reservation。

    防止后续维护者（含 PR2 实施）误把 read-only GET 当付费前最终成本闸。
    """
    import inspect
    import user_voice_api
    doc = inspect.getdoc(user_voice_api.internal_express_auto_clone_budget) or ""
    assert "advisory" in doc.lower(), (
        "budget endpoint docstring 必须含 'advisory' —— 标注它不是 atomic gate"
    )
    assert "atomic" in doc.lower(), (
        "budget endpoint docstring 必须提到 atomic（说明并发竞态 + PR2 reservation）"
    )
    # 必须指向 PR2 / reservation 作为真正的 gate
    assert "reservation" in doc.lower() or "PR2" in doc, (
        "budget endpoint docstring 必须指向 PR2 atomic reservation 作为最终成本闸"
    )

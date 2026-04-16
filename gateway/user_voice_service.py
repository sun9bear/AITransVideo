"""Personal voice library service — per-user CRUD for cloned voices."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserVoice


async def list_user_voices(
    db: AsyncSession,
    user_id: object,
    *,
    include_expired: bool = False,
) -> list[UserVoice]:
    stmt = select(UserVoice).where(UserVoice.user_id == user_id)
    if not include_expired:
        stmt = stmt.where(UserVoice.expired_at.is_(None))
    stmt = stmt.order_by(UserVoice.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def add_user_voice(
    db: AsyncSession,
    *,
    user_id: object,
    voice_id: str,
    label: str,
    provider: str = "minimax_voice_clone",
    tts_provider: str | None = "minimax_tts",
    platform: str | None = "minimax_domestic",
    source_speaker_id: str | None = None,
    notes: str | None = None,
) -> UserVoice:
    # Check existing (including expired — revive if re-cloned)
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.label = label
        existing.provider = provider
        existing.tts_provider = tts_provider
        existing.platform = platform
        existing.source_speaker_id = source_speaker_id
        existing.notes = notes
        existing.expired_at = None
        existing.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return existing

    voice = UserVoice(
        user_id=user_id,
        voice_id=voice_id,
        label=label,
        provider=provider,
        tts_provider=tts_provider,
        platform=platform,
        source_speaker_id=source_speaker_id,
        notes=notes,
    )
    db.add(voice)
    await db.commit()
    await db.refresh(voice)
    return voice


async def delete_user_voice(
    db: AsyncSession,
    user_id: object,
    voice_id: str,
) -> bool:
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
            UserVoice.expired_at.is_(None),
        )
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        return False
    voice.expired_at = datetime.now(timezone.utc)
    voice.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def mark_voice_expired(
    db: AsyncSession,
    user_id: object,
    voice_id: str,
) -> bool:
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
            UserVoice.expired_at.is_(None),
        )
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        return False
    voice.expired_at = datetime.now(timezone.utc)
    voice.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def update_user_voice_label(
    db: AsyncSession,
    voice: UserVoice,
    *,
    label: str,
) -> UserVoice:
    """Update an already-fetched UserVoice's display label.

    Caller must pass the row (e.g. from :func:`fetch_user_voice`) so
    there's no double-SELECT on PATCH.
    """
    now = datetime.now(timezone.utc)
    voice.label = label
    voice.updated_at = now
    await db.commit()
    await db.refresh(voice)
    return voice


async def fetch_user_voice(
    db: AsyncSession,
    user_id: object,
    voice_id: str,
) -> UserVoice | None:
    """Look up a single voice owned by ``user_id``. Returns None if not found
    or if the voice has been expired (mark_voice_expired)."""
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
            UserVoice.expired_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def update_voice_speed_calibration(
    db: AsyncSession,
    voice: UserVoice,
    *,
    cps: float,
    model_key: str | None = None,
) -> UserVoice:
    """Persist a speed-calibration result onto an already-fetched user voice.

    Caller must pass the ``UserVoice`` row (e.g. from :func:`fetch_user_voice`)
    so we don't re-SELECT the same row twice on every calibrate call.

    ``model_key`` (e.g. "speech-2.8-turbo") is recorded in the per-model
    JSONB; ``chars_per_second`` scalar always gets the latest value.
    Re-calibrating one model preserves the other models' values.
    """
    now = datetime.now(timezone.utc)
    voice.chars_per_second = float(cps)
    voice.speed_calibrated_at = now
    voice.updated_at = now
    if model_key:
        existing = dict(voice.chars_per_second_by_model or {})
        existing[model_key] = float(cps)
        voice.chars_per_second_by_model = existing
    await db.commit()
    await db.refresh(voice)
    return voice

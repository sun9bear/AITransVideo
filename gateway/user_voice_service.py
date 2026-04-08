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

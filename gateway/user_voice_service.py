"""Personal voice library service — per-user CRUD for cloned voices."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserVoice


class VoiceNotFoundError(LookupError):
    """Raised by ``update_user_voice_speed_calibration`` /
    ``update_catalog_voice_speed_calibration`` when the (user_id, voice_id)
    or (provider, voice_id) row is gone (deleted between the calling code
    fetching it and the writer's SELECT FOR UPDATE)."""


def _merged_by_model(
    existing: dict | None, *, model_key: str, cps: float
) -> dict[str, float]:
    """Read-modify-write merge of ``chars_per_second_by_model``.

    Done inside a SELECT FOR UPDATE row lock by the helpers below so two
    concurrent calibrations (e.g. T1's parallel turbo + hd) cannot lose
    each other's keys (plan v4.1 codex F-v4.1-1).
    """
    merged = dict(existing or {})
    merged[model_key] = float(cps)
    return merged


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


async def update_user_voice_speed_calibration(
    db: AsyncSession,
    *,
    voice_id: str,
    user_id: object,
    cps: float,
    model_key: str,
) -> UserVoice:
    """Atomically persist a per-model calibration result onto a user voice.

    Plan v4.1 codex F-v4.1-1 fix for the JSONB-key-loss race: when T1
    parallel-calibrates ``speech-2.8-turbo`` and ``speech-2.8-hd`` against
    the same row, the legacy implementation read the dict, set its key,
    and replaced the whole field — the second commit overwrote the first
    commit's key. This helper holds a ``SELECT ... FOR UPDATE`` row lock
    across the read-modify-write so concurrent tasks serialize and merge.

    Plan v4.2 codex F-v4.2-1 fix: query MUST use ``UserVoice.voice_id``
    (provider-side string), NOT ``UserVoice.id`` (UUID primary key).
    Together with ``user_id`` they form the uniqueness constraint
    ``uq_user_voices_user_voice``.

    Plan v4.2 codex F-v4.2-3 fix: helper takes primitive (voice_id, user_id)
    NOT a caller-fetched row object. The caller's row may be stale; we
    re-fetch under the lock so the merge sees the freshest dict.

    Parameters
    ----------
    db:
        AsyncSession. Helper opens its own ``async with db.begin()`` block;
        caller MUST NOT have an open transaction or the begin nests.
    voice_id:
        Provider-side voice id (e.g. MiniMax voice id string), NOT the
        UUID primary key.
    user_id:
        Owner user_id (UUID); accepts ``str`` or ``uuid.UUID`` since the
        SQLAlchemy column auto-coerces.
    cps:
        Calibrated chars-per-second value for ``model_key``.
    model_key:
        Canonical model id (e.g. ``"speech-2.8-turbo"``). Required —
        the per-model JSONB is the only authoritative storage; the scalar
        ``chars_per_second`` field becomes the cross-model mean for
        tooltip display only.

    Raises
    ------
    VoiceNotFoundError
        When no row matches ``(user_id, voice_id)`` — usually means the
        voice was deleted between the caller's intent and our SELECT.
    """
    if not model_key:
        raise ValueError("model_key is required (plan v4 T0-D)")

    async with db.begin():
        # codex v4.4 P1-2: filter expired_at IS NULL so we never write
        # calibration back to a soft-deleted row. This is a defense-
        # in-depth pair with the resolve-time filter in
        # voice_calibration_review_preflight._resolve_targets_user_first;
        # protects against the race where a voice expires between
        # T2's read snapshot and the write.
        result = await db.execute(
            select(UserVoice)
              .where(
                  UserVoice.voice_id == voice_id,    # F-v4.2-1: provider id, NOT UUID PK
                  UserVoice.user_id == user_id,
                  UserVoice.expired_at.is_(None),
              )
              .with_for_update()
        )
        voice = result.scalar_one_or_none()
        if voice is None:
            raise VoiceNotFoundError(f"user_voices missing/expired: voice_id={voice_id!r} user_id={user_id!r}")

        merged = _merged_by_model(voice.chars_per_second_by_model, model_key=model_key, cps=cps)
        voice.chars_per_second_by_model = merged
        # Cross-model mean for tooltip display only — Pre-TTS rewrite
        # reads chars_per_second_by_model[tts_model] preferentially via
        # voice_speed_catalog.resolve_chars_per_second; the scalar is
        # only the fallback for that resolver.
        voice.chars_per_second = sum(merged.values()) / len(merged)
        now = datetime.now(timezone.utc)
        voice.speed_calibrated_at = now
        voice.updated_at = now
    return voice


# Backward-compat alias for the legacy single-helper signature. The manual
# /calibrate-speed endpoint and any pre-T0 callers still import the old
# name; the new behaviour (atomic merge under FOR UPDATE) applies regardless.
#
# v4.3 follow-up: callers should migrate to ``update_user_voice_speed_calibration``
# directly, which makes the (voice_id, user_id) primitive contract explicit.
# This wrapper accepts the legacy "voice row passed in" form and delegates.
async def update_voice_speed_calibration(
    db: AsyncSession,
    voice: UserVoice,
    *,
    cps: float,
    model_key: str | None = None,
) -> UserVoice:
    """Legacy wrapper kept for the manual endpoint's existing call site
    until the T0 endpoint refactor lands. New callers should use
    :func:`update_user_voice_speed_calibration` directly.
    """
    if not model_key:
        # Pre-T0-D callers that didn't track model — set scalar only.
        # This path is unreachable post-T0-D; kept for migration safety.
        now = datetime.now(timezone.utc)
        voice.chars_per_second = float(cps)
        voice.speed_calibrated_at = now
        voice.updated_at = now
        await db.commit()
        await db.refresh(voice)
        return voice

    # New atomic path
    return await update_user_voice_speed_calibration(
        db,
        voice_id=voice.voice_id,
        user_id=voice.user_id,
        cps=cps,
        model_key=model_key,
    )

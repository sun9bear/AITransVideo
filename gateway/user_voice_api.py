"""Personal voice library API — per-user voice CRUD + internal expire endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_auth
from database import get_db
from models import User, UserVoice
from user_voice_service import (
    add_user_voice,
    delete_user_voice,
    fetch_user_voice,
    list_user_voices,
    mark_voice_expired,
    update_user_voice_label,
    update_voice_speed_calibration,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gateway", tags=["user-voices"])
internal_router = APIRouter(prefix="/internal", tags=["user-voices-internal"])

# Calibration uses the cheaper turbo tier per provider (MiniMax CNY 2/万
# vs HD 3.5/万) — cps precision is for the speed_decision estimator and
# doesn't need HD-grade accuracy. Centralised so adding a future provider
# is a one-liner instead of a nested ternary.
_DEFAULT_CALIBRATION_MODEL: dict[str, str] = {
    "minimax": "speech-2.8-turbo",
    "cosyvoice": "cosyvoice-v3-flash",
    "volcengine": "seed-tts-2.0",
}


def _normalize_tts_provider(stored: str | None) -> str | None:
    """Normalize a UserVoice.tts_provider value to one of the canonical
    provider keys ("minimax" / "cosyvoice" / "volcengine"), or None when
    the stored value isn't recognised. Caller decides whether to reject
    or fall back — we don't silently coerce unknown providers to "minimax"
    since that would route a paid call to the wrong API."""
    if not stored:
        return None
    s = stored.strip().lower()
    # Existing rows store tts_provider as e.g. "minimax_tts" or
    # "minimax_voice_clone". Map them all back to the canonical key.
    if s in ("minimax", "minimax_tts", "minimax_voice_clone"):
        return "minimax"
    if s in ("cosyvoice", "cosyvoice_tts", "cosyvoice_voice_clone"):
        return "cosyvoice"
    if s in ("volcengine", "volcengine_tts", "doubao", "doubao_tts"):
        return "volcengine"
    return None


def _voice_to_dict(v) -> dict:
    return {
        "id": str(v.id),
        "voice_id": v.voice_id,
        "voice_type": v.voice_type,
        "provider": v.provider,
        "tts_provider": v.tts_provider,
        "platform": v.platform,
        "label": v.label,
        "source_speaker_id": v.source_speaker_id,
        "notes": v.notes,
        "expired_at": v.expired_at.isoformat() if v.expired_at else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "chars_per_second": v.chars_per_second,
        "chars_per_second_by_model": v.chars_per_second_by_model,
        "speed_calibrated_at": v.speed_calibrated_at.isoformat() if v.speed_calibrated_at else None,
    }


@router.get("/user-voices")
async def get_user_voices(
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user is None:
        return _json(401, {"error": "unauthorized"})
    voices = await list_user_voices(db, user.id)
    return _json(200, {"voices": [_voice_to_dict(v) for v in voices]})


@router.post("/user-voices")
async def create_user_voice(
    request: Request,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user is None:
        return _json(401, {"error": "unauthorized"})
    body = await _read_body(request)
    voice_id = str(body.get("voice_id", "")).strip()
    if not voice_id:
        return _json(400, {"error": "voice_id is required"})
    voice = await add_user_voice(
        db,
        user_id=user.id,
        voice_id=voice_id,
        label=str(body.get("label", voice_id)),
        provider=str(body.get("provider", "minimax_voice_clone")),
        tts_provider=body.get("tts_provider", "minimax_tts"),
        platform=body.get("platform", "minimax_domestic"),
        source_speaker_id=body.get("source_speaker_id"),
        notes=body.get("notes"),
    )
    return _json(200, {"ok": True, "voice": _voice_to_dict(voice)})


@router.patch("/user-voices/{voice_id}")
async def patch_user_voice(
    voice_id: str,
    request: Request,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Update a user voice's mutable fields (currently: label only)."""
    if user is None:
        return _json(401, {"error": "unauthorized"})
    body = await _read_body(request)
    label = str(body.get("label", "")).strip()
    if not label or len(label) > 200:
        return _json(400, {"error": "label must be 1-200 chars"})
    voice = await fetch_user_voice(db, user.id, voice_id)
    if voice is None:
        return _json(404, {"error": "voice_not_found"})
    updated = await update_user_voice_label(db, voice, label=label)
    return _json(200, {"ok": True, "voice": _voice_to_dict(updated)})


@router.post("/user-voices/probe")
async def probe_user_voice(
    request: Request,
    user: User | None = Depends(require_auth),
) -> Response:
    """Synthesize a short sample to verify a voice_id is usable + let the
    user hear how it sounds. Returns base64-encoded WAV audio.

    Body: {voice_id, label?, tts_provider?}
    The voice does NOT need to exist in user_voices yet (supports the
    "add voice" modal pre-validation flow).
    """
    if user is None:
        return _json(401, {"error": "unauthorized"})
    body = await _read_body(request)
    voice_id = str(body.get("voice_id", "")).strip()
    if not voice_id:
        return _json(400, {"error": "voice_id is required"})
    label = str(body.get("label", "")).strip() or voice_id
    raw_provider = str(body.get("tts_provider", "")).strip() or None
    provider = _normalize_tts_provider(raw_provider) if raw_provider else "minimax"
    if provider is None:
        return _json(400, {"error": "unsupported_provider"})
    model = _DEFAULT_CALIBRATION_MODEL[provider]

    sample_text = f"你好，我是{label}，欢迎使用视频翻译服务。"

    from voice_speed_calibrator import _DEFAULT_SYNTH_FNS

    synth_fn = _DEFAULT_SYNTH_FNS.get(provider)
    if synth_fn is None:
        return _json(400, {"error": f"no synth function for provider {provider}"})

    import base64

    try:
        audio_bytes = await asyncio.to_thread(synth_fn, sample_text, voice_id, model)
    except Exception as exc:
        logger.warning("[probe] synth failed for voice %s: %s", voice_id, exc)
        return _json(502, {
            "error": "probe_failed",
            "message": str(exc)[:300],
        })

    if not audio_bytes:
        return _json(502, {"error": "probe_failed", "message": "empty audio"})

    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    return _json(200, {
        "ok": True,
        "audio_base64": audio_b64,
        "audio_format": "wav",
        "text": sample_text,
        "voice_id": voice_id,
        "provider": provider,
    })


@router.delete("/user-voices/{voice_id}")
async def remove_user_voice(
    voice_id: str,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user is None:
        return _json(401, {"error": "unauthorized"})
    deleted = await delete_user_voice(db, user.id, voice_id)
    return _json(200, {"ok": True, "deleted": deleted})


@router.post("/user-voices/{voice_id}/calibrate-speed")
async def calibrate_voice_speed(
    voice_id: str,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Calibrate one user voice's chars-per-second by running 3 standard
    Chinese texts through the provider's TTS, then persist the result.

    - 401 if not authenticated
    - 404 if the voice doesn't exist or doesn't belong to ``user``
    - 400 if the voice's tts_provider can't be mapped to a supported one
    - 502 if the provider call fails / produces invalid audio
    - 200 with the new cps + per-text breakdown on success
    """
    if user is None:
        return _json(401, {"error": "unauthorized"})

    voice = await fetch_user_voice(db, user.id, voice_id)
    if voice is None:
        return _json(404, {"error": "voice_not_found"})

    provider = _normalize_tts_provider(voice.tts_provider)
    if provider is None:
        logger.warning(
            "[calibrate-speed] voice %s has unsupported tts_provider %r — refusing",
            voice_id, voice.tts_provider,
        )
        return _json(400, {
            "error": "unsupported_provider",
            "message": (
                f"音色的 tts_provider {voice.tts_provider!r} 暂不支持自动标定。"
                "请联系管理员手动校准。"
            ),
        })
    model = _DEFAULT_CALIBRATION_MODEL[provider]

    from voice_speed_calibrator import calibrate_voice

    try:
        result = await asyncio.to_thread(
            calibrate_voice,
            provider=provider,
            model=model,
            voice_id=voice_id,
        )
    except Exception as exc:
        logger.exception("[calibrate-speed] unexpected error for voice %s", voice_id)
        return _json(500, {"error": "calibration_failed", "message": str(exc)[:200]})

    per_text_payload = [
        {"name": t.name, "hanzi": t.hanzi, "duration_ms": t.duration_ms, "cps": t.cps}
        for t in result.per_text
    ]
    if not result.ok:
        return _json(502, {
            "error": "calibration_failed",
            "message": result.error,
            "per_text": per_text_payload,
        })

    updated = await update_voice_speed_calibration(
        db,
        voice,
        cps=result.cps,
        model_key=model,
    )

    logger.info(
        "[calibrate-speed] voice=%s provider=%s model=%s cps=%.4f total_ms=%d",
        voice_id, provider, model, result.cps, result.total_duration_ms,
    )
    return _json(200, {
        "ok": True,
        "voice": _voice_to_dict(updated),
        "calibration": {
            "cps": result.cps,
            "total_hanzi": result.total_hanzi,
            "total_duration_ms": result.total_duration_ms,
            "provider": provider,
            "model": model,
            "per_text": per_text_payload,
        },
    })


@internal_router.get("/user-voices/by-voice-ids")
async def internal_lookup_user_voices_by_ids(
    voice_ids: str = Query(..., description="Comma-separated voice_ids"),
    user_id: str = Query(..., description="Owning user UUID — REQUIRED to prevent cross-user cps leakage"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Pipeline lookup of speed calibration for cloned voices.

    Scoped by ``user_id`` to prevent cross-user data leakage: the
    ``user_voices`` table's only DB-level uniqueness is ``(user_id,
    voice_id)``, so the same ``voice_id`` can legitimately exist for
    two different users. Without the user filter, the pipeline would
    silently read (and cache) another user's cps.

    Returns only voices that:
      - belong to the given user
      - have a non-null ``chars_per_second`` (calibrated)
      - are not expired
    """
    ids = [v.strip() for v in voice_ids.split(",") if v.strip()]
    if not ids:
        return _json(200, {"voices": []})
    if len(ids) > 200:
        ids = ids[:200]

    uid = (user_id or "").strip()
    if not uid:
        return _json(400, {"error": "user_id_required"})

    try:
        user_uuid = uuid.UUID(uid)
    except (ValueError, AttributeError):
        return _json(400, {"error": "invalid_user_id"})

    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_uuid,
            UserVoice.voice_id.in_(ids),
            UserVoice.chars_per_second.isnot(None),
            UserVoice.expired_at.is_(None),
        )
    )
    voices = result.scalars().all()
    return _json(200, {
        "voices": [
            {
                "voice_id": v.voice_id,
                "chars_per_second": v.chars_per_second,
                "chars_per_second_by_model": v.chars_per_second_by_model,
                "speed_calibrated_at": (
                    v.speed_calibrated_at.isoformat() if v.speed_calibrated_at else None
                ),
                "tts_provider": v.tts_provider,
                "platform": v.platform,
            }
            for v in voices
        ],
    })


@internal_router.post("/user-voices/expire")
async def internal_expire_voice(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    body = await _read_body(request)
    user_id = body.get("user_id")
    job_id = body.get("job_id")
    voice_id = str(body.get("voice_id", "")).strip()
    if not voice_id:
        return _json(400, {"error": "voice_id required"})

    # Resolve user_id from job_id if not provided
    if not user_id and job_id:
        from sqlalchemy import select
        from models import Job
        result = await db.execute(
            select(Job.user_id).where(Job.job_id == str(job_id))
        )
        row = result.scalar_one_or_none()
        if row is not None:
            user_id = row

    if not user_id:
        return _json(400, {"error": "无法确定用户"})

    expired = await mark_voice_expired(db, user_id, voice_id)
    return _json(200, {"ok": True, "expired": expired})


async def _read_body(request: Request) -> dict:
    raw = await request.body()
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _json(status: int, body: dict) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status,
        headers={"content-type": "application/json"},
    )

"""Personal voice library API — per-user voice CRUD + internal expire endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import risk_control
from auth import require_auth
from database import get_db
from models import User, UserVoice
from services.tts.voice_speed_bounds import MAX_VALID_CPS, MIN_VALID_CPS
from user_voice_service import (
    add_user_voice,
    delete_user_voice,
    fetch_user_voice,
    list_user_voices,
    mark_voice_expired,
    update_user_voice_label,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gateway", tags=["user-voices"])
# P0-2b (audit 2026-05-07): prefix changed from /internal → /api/internal so the
# Caddyfile @internal_block (which only blocks /api/internal/*) properly shields
# these endpoints from public ingress. Callers in src/services/tts/voice_speed_catalog.py
# and src/pipeline/process.py have been updated to match.
internal_router = APIRouter(prefix="/api/internal", tags=["user-voices-internal"])

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


def _internal_access_error(request: Request) -> Response | None:
    from config import settings as _settings

    key = _settings.internal_api_key
    if not key:
        return _json(503, {"error": "internal_endpoint_misconfigured"})
    if request.headers.get("X-Internal-Key", "") != key:
        return _json(403, {"error": "invalid_internal_key"})
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

    P2-23 (audit 2026-05-07): per-user rate limited to 10/min + 100/day.
    Each probe synthesises against MiniMax / CosyVoice / VolcEngine
    paid TTS, so without a limit a logged-in attacker could spend the
    platform's TTS budget at line-rate.

    P2-23 follow-up (Codex review 2a9c529): the v0 "check before /
    record after" split was vulnerable to concurrent burst — N
    coroutines all passed the check before any record stamped, so all
    N hit the paid provider in parallel. v1 closes the hole with
    reserve-before-paid-call + refund-on-failure semantics: the
    reservation lands atomically inside the rate-limit lock BEFORE
    the paid call, so concurrent reservers correctly observe each
    other's slots; refund rolls the slot back when the provider
    returns 5xx / empty audio so flaky providers don't consume the
    user's daily quota.
    """
    if user is None:
        return _json(401, {"error": "unauthorized"})

    # Reserve a probe slot atomically BEFORE the paid call. Concurrent
    # callers see each other's reservations under the lock — the check
    # + append are inseparable, so a burst of 100 simultaneous requests
    # admits exactly the limit (10) and rejects the rest with 429.
    try:
        reservation = risk_control.reserve_voice_probe(str(user.id))
    except risk_control.RateLimitExceeded as exc:
        return _json(429, {
            "error": "rate_limited",
            "scope": exc.scope,
            "message": exc.message,
        })

    try:
        body = await _read_body(request)
        voice_id = str(body.get("voice_id", "")).strip()
        if not voice_id:
            # Refund: bad-input rejections shouldn't tick the user's
            # quota — they didn't reach the paid provider.
            risk_control.refund_voice_probe(str(user.id), reservation)
            return _json(400, {"error": "voice_id is required"})
        label = str(body.get("label", "")).strip() or voice_id
        raw_provider = str(body.get("tts_provider", "")).strip() or None
        provider = (
            _normalize_tts_provider(raw_provider) if raw_provider else "minimax"
        )
        if provider is None:
            risk_control.refund_voice_probe(str(user.id), reservation)
            return _json(400, {"error": "unsupported_provider"})
        model = _DEFAULT_CALIBRATION_MODEL[provider]

        sample_text = f"你好，我是{label}，欢迎使用视频翻译服务。"

        from voice_speed_calibrator import _DEFAULT_SYNTH_FNS

        synth_fn = _DEFAULT_SYNTH_FNS.get(provider)
        if synth_fn is None:
            risk_control.refund_voice_probe(str(user.id), reservation)
            return _json(
                400, {"error": f"no synth function for provider {provider}"}
            )

        import base64

        try:
            audio_bytes = await asyncio.to_thread(
                synth_fn, sample_text, voice_id, model
            )
        except Exception as exc:
            logger.warning(
                "[probe] synth failed for voice %s: %s", voice_id, exc
            )
            risk_control.refund_voice_probe(str(user.id), reservation)
            return _json(502, {
                "error": "probe_failed",
                "message": str(exc)[:300],
            })

        if not audio_bytes:
            risk_control.refund_voice_probe(str(user.id), reservation)
            return _json(
                502, {"error": "probe_failed", "message": "empty audio"}
            )

        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        return _json(200, {
            "ok": True,
            "audio_base64": audio_b64,
            "audio_format": "wav",
            "text": sample_text,
            "voice_id": voice_id,
            "provider": provider,
        })
    except Exception:
        # Defensive: any unexpected exception escaping the body refunds
        # the slot so a code-bug doesn't burn the user's quota.
        risk_control.refund_voice_probe(str(user.id), reservation)
        raise


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


# Canonical models per provider for the v4 default-dual-model policy
# (plan T0-D / F-v4-8). When the manual endpoint is called without an
# explicit model_key, calibrate ALL canonical models for the voice's
# provider in parallel — same behaviour T1 clone-after will use, so that
# users picking turbo vs hd later in review have CPS data ready for
# whichever they choose.
_CANONICAL_MODELS_BY_PROVIDER: dict[str, list[str]] = {
    "minimax": ["speech-2.8-turbo", "speech-2.8-hd"],
    "cosyvoice": ["cosyvoice-v3-flash"],
    "volcengine": ["seed-tts-2.0"],
}


async def _run_one_user_voice_calibration(
    *,
    user_id: str,
    voice_id: str,
    provider: str,
    model_key: str,
):
    """Single (user_voice, model_key) calibration: factory body for run_calibration_task.

    Plan v4.3 T0-D contract:
    - Factory ALWAYS returns CalibrationResult (never raises). All errors
      packed into CalibrationResult fields.
    - paid_call_count reflects calls actually issued.
    - DB write uses an INDEPENDENT short session (with SELECT FOR UPDATE
      inside update_user_voice_speed_calibration) — no nested transactions
      with the route db.
    """
    from voice_speed_calibrator import CalibrationResult, calibrate_voice
    from database import async_session
    from user_voice_service import (
        VoiceNotFoundError,
        update_user_voice_speed_calibration,
    )

    # T0-C bounded TTS calls; calibrate_voice never raises per T0-D contract.
    result = await asyncio.to_thread(
        calibrate_voice,
        provider=provider,
        model=model_key,
        voice_id=voice_id,
        total_timeout_seconds=60.0,
    )
    if not result.ok:
        return result

    # DB write — independent short session, atomic merge.
    try:
        async with async_session() as db_write:
            await update_user_voice_speed_calibration(
                db_write,
                voice_id=voice_id,
                user_id=user_id,
                cps=result.cps,
                model_key=model_key,
            )
    except VoiceNotFoundError:
        # Voice was deleted between the existence check above and the write.
        # paid_call_count is preserved (TTS already happened) so refund won't
        # fire — that's the correct semantic.
        return CalibrationResult(
            ok=False,
            error="voice_not_found at write time",
            error_class="voice_not_found",
            paid_call_count=result.paid_call_count,
            per_text=result.per_text,
            cps=result.cps,
            model_key=model_key,
        )
    except Exception as exc:
        logger.exception(
            "[calibrate-speed] DB write failed after paid TTS — preserving paid_call_count=%d",
            result.paid_call_count,
        )
        return CalibrationResult(
            ok=False,
            error=f"db_write_failed: {exc!r}"[:300],
            error_class="db_write_failed",
            paid_call_count=result.paid_call_count,
            per_text=result.per_text,
            cps=result.cps,
            model_key=model_key,
        )

    return result


@router.post("/user-voices/{voice_id}/calibrate-speed")
async def calibrate_voice_speed(
    voice_id: str,
    request: Request,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Calibrate a user voice's chars-per-second across one or more models.

    Plan v4.3 T0-A.2 / F-v4.3-2 contract:
    - Optional ``{"model_key": "..."}`` body. None / missing = calibrate
      all canonical models for the voice's provider in parallel
      (matches T1 clone-after default).
    - Budget reservation (``reserve_voice_calibration``) lands on the
      atomic-claim path inside ``run_calibration_task``, so a UI button
      double-click joins the in-flight future without consuming a second
      slot.
    - Route DB session is rolled back BEFORE paid TTS so connection pool
      isn't held for ~30s × N models. Writes happen via independent short
      sessions inside the factory.

    Returns
    -------
    200 with per-model results when at least one model succeeded.
    202 when all models hit ``rate_limited`` (caller can retry later).
    400 / 404 unchanged from prior behaviour.
    502 when all models failed at the provider layer.
    """
    if user is None:
        return _json(401, {"error": "unauthorized"})

    # Step 1 (route db, short-lived): existence + provider validation.
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

    # Parse optional model_key body. We tolerate empty body / non-JSON
    # gracefully — UI's existing "校准语速" button currently sends no body.
    model_key_override: str | None = None
    try:
        raw_body = await request.body()
        if raw_body:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                mk = parsed.get("model_key")
                if isinstance(mk, str) and mk.strip():
                    model_key_override = mk.strip()
    except (json.JSONDecodeError, ValueError):
        # Bad JSON is not fatal — fall through to default-all-models.
        pass

    if model_key_override is not None:
        models_to_run = [model_key_override]
    else:
        models_to_run = _CANONICAL_MODELS_BY_PROVIDER.get(
            provider, [_DEFAULT_CALIBRATION_MODEL[provider]],
        )

    # Step 2 (codex F-v4.3-2): release route db connection BEFORE paid call.
    # The connection went into use for fetch_user_voice + (auth's prior
    # SELECTs); rollback ends any implicit transaction and returns the
    # connection to the pool for other requests during the ~30s × N models
    # paid TTS work.
    user_id_str = str(user.id)
    await db.rollback()

    # Step 3: parallel calibration via the shared run_calibration_task helper.
    # claim_or_join + budget + factory + refund-on-paid_count==0 + release
    # all live in that one helper so the manual endpoint, T1 clone hook,
    # T2 review preflight, and T3 admin batch share identical semantics.
    from voice_calibration_inflight import (
        CalibrationKey,
        run_calibration_task,
    )

    async def _run_one(model_key: str):
        key = CalibrationKey(
            scope="user",
            owner=user_id_str,
            provider=provider,
            voice_id=voice_id,
            model_key=model_key,
        )

        async def _factory():
            return await _run_one_user_voice_calibration(
                user_id=user_id_str,
                voice_id=voice_id,
                provider=provider,
                model_key=model_key,
            )

        try:
            result = await run_calibration_task(
                key=key,
                user_id_for_budget=user_id_str,
                factory=_factory,
            )
            return model_key, result, None
        except risk_control.RateLimitExceeded as exc:
            return model_key, None, exc

    outcomes = await asyncio.gather(
        *(_run_one(mk) for mk in models_to_run),
        return_exceptions=False,
    )

    # Aggregate response.
    results_payload: list[dict] = []
    any_ok = False
    all_rate_limited = True
    rate_limit_message = None
    for model_key, result, rate_limit_exc in outcomes:
        if rate_limit_exc is not None:
            all_rate_limited = all_rate_limited and True
            rate_limit_message = rate_limit_exc.message
            results_payload.append({
                "model_key": model_key,
                "ok": False,
                "error_class": "rate_limited",
                "message": rate_limit_exc.message,
            })
            continue
        all_rate_limited = False
        if result is None:
            results_payload.append({
                "model_key": model_key,
                "ok": False,
                "error_class": "internal_error",
                "message": "no result from run_calibration_task",
            })
            continue

        per_text_payload = [
            {"name": t.name, "hanzi": t.hanzi, "duration_ms": t.duration_ms, "cps": t.cps}
            for t in result.per_text
        ]
        if result.ok:
            any_ok = True
        results_payload.append({
            "model_key": model_key,
            "ok": result.ok,
            "cps": result.cps,
            "total_hanzi": result.total_hanzi,
            "total_duration_ms": result.total_duration_ms,
            "error_class": result.error_class or ("" if result.ok else "unknown"),
            "error": result.error,
            "paid_call_count": result.paid_call_count,
            "per_text": per_text_payload,
        })
        logger.info(
            "[calibrate-speed] voice=%s provider=%s model=%s ok=%s cps=%.4f paid_calls=%d",
            voice_id, provider, model_key,
            result.ok, result.cps, result.paid_call_count,
        )

    if all_rate_limited:
        return _json(429, {
            "error": "rate_limited",
            "message": rate_limit_message or "calibration budget exhausted",
            "results": results_payload,
        })

    # Refresh the voice row from a fresh session so the response reflects
    # the writes that just happened in independent factory sessions.
    from database import async_session
    async with async_session() as db_read:
        refreshed = await fetch_user_voice(db_read, user.id, voice_id)

    if not any_ok:
        return _json(502, {
            "error": "calibration_failed",
            "message": "all models failed; see results for details",
            "voice": _voice_to_dict(refreshed) if refreshed else None,
            "results": results_payload,
        })

    return _json(200, {
        "ok": True,
        "voice": _voice_to_dict(refreshed) if refreshed else None,
        "results": results_payload,
        "provider": provider,
    })


@internal_router.get("/user-voices/by-voice-ids")
async def internal_lookup_user_voices_by_ids(
    request: Request,
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
    internal_error = _internal_access_error(request)
    if internal_error is not None:
        return internal_error

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


@internal_router.post("/user-voices/speed-profiles")
async def internal_ingest_user_voice_speed_profiles(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Persist conservative cloned-voice speed profiles observed by pipeline.

    This endpoint only fills missing per-model/scalar calibration fields for
    existing ``user_voices`` rows. It deliberately does not create voices and
    does not overwrite an already calibrated model, so production jobs cannot
    degrade a user's explicit calibration.
    """
    internal_error = _internal_access_error(request)
    if internal_error is not None:
        return internal_error

    body = await _read_body(request)
    uid = str(body.get("user_id", "") or "").strip()
    if not uid:
        return _json(400, {"error": "user_id_required"})
    try:
        user_uuid = uuid.UUID(uid)
    except (ValueError, AttributeError):
        return _json(400, {"error": "invalid_user_id"})

    profiles = body.get("profiles")
    if not isinstance(profiles, list):
        return _json(400, {"error": "profiles_required"})
    profiles = profiles[:50]
    voice_ids = [
        str(item.get("voice_id", "")).strip()
        for item in profiles
        if isinstance(item, dict) and str(item.get("voice_id", "")).strip()
    ]
    if not voice_ids:
        return _json(200, {"ok": True, "updated_count": 0, "skipped_count": 0})

    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_uuid,
            UserVoice.voice_id.in_(voice_ids),
            UserVoice.expired_at.is_(None),
        )
    )
    voices_by_id = {voice.voice_id: voice for voice in result.scalars().all()}
    updated: list[dict] = []
    skipped: list[dict] = []
    now = datetime.now(timezone.utc)

    for item in profiles:
        if not isinstance(item, dict):
            skipped.append({"voice_id": "", "reason": "invalid_profile"})
            continue
        voice_id = str(item.get("voice_id", "") or "").strip()
        if not voice_id:
            skipped.append({"voice_id": "", "reason": "missing_voice_id"})
            continue
        voice = voices_by_id.get(voice_id)
        if voice is None:
            skipped.append({"voice_id": voice_id, "reason": "missing_user_voice"})
            continue

        stored_provider = _normalize_tts_provider(voice.tts_provider)
        profile_provider = _normalize_tts_provider(str(item.get("tts_provider", "") or ""))
        if stored_provider is None:
            skipped.append({"voice_id": voice_id, "reason": "unsupported_voice_provider"})
            continue
        if profile_provider is None:
            skipped.append({"voice_id": voice_id, "reason": "unsupported_profile_provider"})
            continue
        if stored_provider != profile_provider:
            skipped.append({"voice_id": voice_id, "reason": "provider_mismatch"})
            continue

        try:
            cps = float(item.get("chars_per_second"))
        except (TypeError, ValueError):
            skipped.append({"voice_id": voice_id, "reason": "invalid_cps"})
            continue
        if not (MIN_VALID_CPS <= cps <= MAX_VALID_CPS):
            skipped.append({"voice_id": voice_id, "reason": "cps_out_of_range"})
            continue

        model_key = str(item.get("model_key", "") or "").strip()
        by_model = dict(voice.chars_per_second_by_model or {})
        if model_key:
            if model_key in by_model:
                skipped.append({"voice_id": voice_id, "reason": "existing_model_calibration"})
                continue
            by_model[model_key] = cps
            voice.chars_per_second_by_model = by_model
        elif voice.chars_per_second is not None:
            skipped.append({"voice_id": voice_id, "reason": "existing_scalar_calibration"})
            continue

        if voice.chars_per_second is None:
            voice.chars_per_second = cps
        voice.speed_calibrated_at = now
        voice.updated_at = now
        updated.append({
            "voice_id": voice_id,
            "chars_per_second": cps,
            "model_key": model_key,
        })

    if updated:
        await db.commit()

    logger.info(
        "[user-voice-speed-profile] job=%s user=%s updated=%d skipped=%d",
        body.get("job_id"), uid, len(updated), len(skipped),
    )
    return _json(200, {
        "ok": True,
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "updated": updated,
        "skipped": skipped,
    })


@internal_router.post("/user-voices/expire")
async def internal_expire_voice(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    internal_error = _internal_access_error(request)
    if internal_error is not None:
        return internal_error

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

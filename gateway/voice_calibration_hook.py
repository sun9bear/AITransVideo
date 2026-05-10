"""T1 clone-after auto-calibration hook.

When a user successfully clones a voice via ``/job-api/jobs/{id}/voice-clone``,
the gateway immediately enqueues background calibration tasks (one per
canonical TTS model — turbo + hd for MiniMax) so the next translation job
that uses this voice has accurate ``chars_per_second`` data for Pre-TTS
text rewrite.

Design contract (plan v4.3 §3.1 + §5.1, codex F2 v3 hardening):

1. **Primitive-only params.** ``calibrate_after_clone`` accepts
   ``(voice_id, user_id, provider, model_key)`` strings — NEVER an ORM
   row or a request-scoped DB session. Background tasks outlive the
   request that spawned them; passing a row that becomes stale (or a
   session that gets closed by FastAPI dependency teardown) is a known
   crash class.

2. **Owned DB session.** We open a fresh ``async_session()`` inside the
   factory only when the actual write is needed. The caller (clone
   endpoint) is free to close its own ``db`` immediately after returning
   the 200 response — we don't share connections.

3. **Goes through run_calibration_task.** All four T0-B contracts apply:
   atomic claim_or_join, budget reservation, factory-only-returns
   (never raises), refund-on-paid_call_count==0, identity-checked
   release. This means a clone that fires immediately after a manual
   ``/calibrate-speed`` for the same voice JOINS the in-flight future
   instead of issuing a duplicate paid TTS call.

4. **Silent on every failure path.** The user has just paid for a clone;
   they expect 200 with the new voice_id. Auto-calibration is a "nice
   to have" backfill — failure (rate limit, provider error, DB error,
   anything) MUST NOT propagate. The hook logs at ERROR (operations
   surface) and returns None.

5. **Feature flag gate.** ``AVT_AUTO_CALIBRATE_AFTER_CLONE=false`` (any
   case-insensitive falsey) disables the entire hook — useful as a
   kill switch if MiniMax has an outage and we don't want every clone
   to also burn calibration retries.
"""
from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

# Default ON. Operator can flip to "false" / "0" / "no" to disable.
_ENV_GATE: Final[str] = "AVT_AUTO_CALIBRATE_AFTER_CLONE"
_FALSEY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})

# T0 phase 1: only MiniMax has bounded primitives. CosyVoice / VolcEngine
# helpers don't yet have provider-specific timeouts; auto-calibrating them
# from clone-after would burn budget on calls that may run 5+ minutes.
# Mirror the manual endpoint's whitelist (see user_voice_api.py
# _CANONICAL_MODELS_BY_PROVIDER + codex T0-review F-T0-5).
CANONICAL_MODELS_BY_PROVIDER: Final[dict[str, tuple[str, ...]]] = {
    "minimax": ("speech-2.8-turbo", "speech-2.8-hd"),
    # cosyvoice / volcengine deliberately omitted.
}


def auto_calibrate_enabled() -> bool:
    """Resolve the env gate at call time so docker-compose changes take
    effect without restarting just to re-import this module.

    Default ON: missing env var or empty string → enabled. Explicit
    falsey values disable.
    """
    raw = os.environ.get(_ENV_GATE, "").strip().lower()
    if raw == "":
        return True  # default ON
    return raw not in _FALSEY


async def calibrate_after_clone(
    *,
    voice_id: str,
    user_id: str,
    provider: str,
    model_key: str,
) -> None:
    """Run a single (voice_id, user_id, provider, model_key) calibration
    in the background after a successful clone.

    NEVER raises. Logs at ERROR on failure and returns None. Caller
    should ``asyncio.create_task(calibrate_after_clone(...))`` and
    then forget — no result is meaningful to the clone request flow.

    Parameters
    ----------
    voice_id:
        Provider-side voice id from the clone response. For MiniMax this
        is the ``moss_audio_*`` string.
    user_id:
        Owner user_id (UUID as string). Captured from ``str(user.id)``
        BEFORE the route session is rolled back / closed (matches the
        manual endpoint's pattern, see commit 3484132 fix for the
        ``MissingGreenlet`` lazy-load bug).
    provider:
        Canonical lowercase provider key. T0 phase 1 only ``"minimax"``;
        anything else is silently rejected (logged WARNING).
    model_key:
        Canonical TTS model id, e.g. ``"speech-2.8-turbo"``. MUST be in
        ``CANONICAL_MODELS_BY_PROVIDER[provider]`` — anything else is
        silently rejected (logged WARNING).
    """
    if not auto_calibrate_enabled():
        logger.info(
            "[auto-calibrate-clone] disabled by env (%s) — skipping voice_id=%s",
            _ENV_GATE, voice_id,
        )
        return

    # Validate provider + model_key against the same whitelist the manual
    # endpoint uses. Caller (voice_selection_api) only fans out to MiniMax
    # canonical models, so this is a defensive belt-and-braces check.
    allowed_models = CANONICAL_MODELS_BY_PROVIDER.get(provider)
    if allowed_models is None:
        logger.warning(
            "[auto-calibrate-clone] unsupported provider=%s voice_id=%s — skipping",
            provider, voice_id,
        )
        return
    if model_key not in allowed_models:
        logger.warning(
            "[auto-calibrate-clone] model_key=%s not in whitelist %s — skipping voice_id=%s",
            model_key, allowed_models, voice_id,
        )
        return

    # Lazy imports — keep cold-start cheap and avoid pulling pydub-adjacent
    # modules into gateway boot path until the first clone fires.
    try:
        from voice_calibration_inflight import CalibrationKey, run_calibration_task
    except Exception:
        # Module import failure is unrecoverable — just log and bail.
        logger.exception(
            "[auto-calibrate-clone] import failed — skipping voice_id=%s", voice_id,
        )
        return

    key = CalibrationKey(
        scope="user",
        owner=user_id,
        provider=provider,
        voice_id=voice_id,
        model_key=model_key,
    )

    async def _factory():
        """Identical contract to the manual endpoint's
        ``_run_one_user_voice_calibration``: ALWAYS returns
        CalibrationResult, NEVER raises. Errors get packed into result
        fields so the run_calibration_task helper can decide
        refund/release correctly.

        Critical: opens a FRESH ``async_session()`` for the DB write —
        this hook is invoked from a background task that outlives the
        clone route's ``db`` session. Reusing that session would crash
        with ``MissingGreenlet`` (closed connection) or worse (write
        racing with the route's commit).
        """
        from voice_speed_calibrator import CalibrationResult, calibrate_voice
        from database import async_session
        from user_voice_service import (
            VoiceNotFoundError,
            update_user_voice_speed_calibration,
        )
        import asyncio

        # T0-C bounded primitives — calibrate_voice never raises per T0-D.
        result = await asyncio.to_thread(
            calibrate_voice,
            provider=provider,
            model=model_key,
            voice_id=voice_id,
            total_timeout_seconds=60.0,
        )
        if not result.ok:
            return result

        # DB write — owned short session, atomic merge inside.
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
            # Voice was deleted between clone success and our write
            # (extremely unlikely but possible if user spam-deletes).
            # paid_call_count is preserved so the budget refund won't
            # fire — TTS already happened, that cost is real.
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
                "[auto-calibrate-clone] DB write failed after paid TTS "
                "voice_id=%s model_key=%s paid_call_count=%d",
                voice_id, model_key, result.paid_call_count,
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

    # Run through the shared helper. Anything that escapes (RateLimitExceeded
    # or some unexpected programmer error) gets caught here so the caller
    # never sees it.
    try:
        result = await run_calibration_task(
            key=key,
            user_id_for_budget=user_id,
            factory=_factory,
        )
    except Exception:
        logger.exception(
            "[auto-calibrate-clone] task wrapper raised "
            "voice_id=%s model_key=%s — swallowed",
            voice_id, model_key,
        )
        return

    # Result observability — we don't escalate failures, but we want them
    # visible in logs for ops dashboards.
    if result is None:
        logger.warning(
            "[auto-calibrate-clone] no result voice_id=%s model_key=%s",
            voice_id, model_key,
        )
        return

    if result.ok:
        logger.info(
            "[auto-calibrate-clone] success voice_id=%s model_key=%s cps=%.2f paid_call_count=%d",
            voice_id, model_key, result.cps, result.paid_call_count,
        )
    else:
        logger.warning(
            "[auto-calibrate-clone] failed voice_id=%s model_key=%s "
            "error_class=%s paid_call_count=%d error=%s",
            voice_id, model_key,
            getattr(result, "error_class", None),
            result.paid_call_count,
            (getattr(result, "error", None) or "")[:200],
        )

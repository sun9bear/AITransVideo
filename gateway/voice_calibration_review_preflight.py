"""T2 review-submit voice calibration preflight.

When a user clicks "approve voice selection" on the Studio review UI, this
module pre-flights the chosen voices' Chars-Per-Second (CPS) calibration
*before* the request is forwarded to Job-API to start the TTS pipeline.
Voices that lack ``chars_per_second_by_model[<final_minimax_model>]`` get
calibrated in parallel with a hard 50s upper bound; pending tasks at that
boundary keep running in background and the proxy_request still fires.

Plan: docs/plans/2026-05-09-voice-cps-auto-calibration-plan.md §3.2 T2.

Hard contracts (read these first):

1. **Final job-level model.** MiniMax voices are calibrated against the
   *job-level* final model, NOT each speaker's per-spec model_hint. This
   matches ``_aggregate_quality_tier_from_speakers`` in job_intercept.py:
   any speaker selecting hd → entire job runs hd → all minimax voices
   need hd CPS. Speaker A=turbo + Speaker B=hd → final='speech-2.8-hd' →
   we calibrate hd CPS for both A and B's voices.
   (codex F-v4-2)

2. **F-v4.3-1: voice_source field is unreliable.** Frontend writes
   ``voice_source: 'catalog'`` even when the user picks a voice from
   "我的音色" library that was previously cloned. We MUST NOT route
   user_voices vs voice_catalog by that field. Instead: probe
   ``user_voices(owner_id, voice_id)`` first; on miss, fall back to
   ``voice_catalog(provider='minimax', voice_id, archived_at IS NULL)``.

3. **Independent DB sessions only.** The route's ``db`` is rolled back by
   the caller before invoking this module so its connection returns to
   pool. Inside this module we open *short, owned* sessions for
   (a) the batch CPS query, then (b) per-task DB writes. We never share
   sessions with the route or with each other.
   (codex F-v4-5, F-v4.1-2)

4. **MiniMax-only phase 1.** CosyVoice / VolcEngine voices in the
   speakers payload are silently skipped with an info log — calibrate_voice
   only has bounded primitives for MiniMax (T0-C). T0-C-2 is the future
   sub-task to extend bounded primitives to other providers.
   (codex F-v4-7)

5. **50s hard upper bound, no cancellation.** ``asyncio.wait`` with
   timeout=50.0 returns done + pending. Pending tasks are NOT cancelled —
   their factory's paid TTS may already have fired. Each pending task
   self-writes its result in a background session; this module only
   attaches an observability done_callback (logs only, no DB).
   (codex F-v4-6, F-v4.1-8, F-v4.2-4)

6. **Idempotent.** Voices already calibrated for the final model_key are
   reported as ``already_calibrated`` outcomes without invoking
   calibrate_voice. T0-B's atomic claim_or_join also de-dupes against any
   in-flight T1 (clone-after) task targeting the same key, so a user who
   clones-then-immediately-submits won't fire a duplicate paid TTS.

7. **Never raises.** All errors → outcome dicts with status="error" or
   "timeout". The caller's ``proxy_request`` always fires regardless.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from typing import Final

from sqlalchemy import select

logger = logging.getLogger(__name__)


# Default OFF — operator must explicitly opt in. Mirrors the safety
# posture of T1 (which is default ON because clone is itself a paid
# action; T2 fires on every review submit, so default OFF lets ops
# observe the manual /calibrate-speed + T1 paths first.)
_ENV_GATE: Final[str] = "AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT"
_FALSEY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})


def review_preflight_enabled() -> bool:
    """Resolve env gate at call time so operator can flip without restart."""
    raw = os.environ.get(_ENV_GATE, "").strip().lower()
    if raw == "":
        return False  # default OFF
    return raw not in _FALSEY


# ---------------------------------------------------------------------------
# Speaker spec parsing
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _SpeakerSpec:
    """Parsed view of one speakers[] entry. Excludes invalid/skip entries."""
    speaker_id: str
    voice_id: str
    tts_provider: str  # canonical lowercase: 'minimax' / 'cosyvoice' / 'volcengine'
    minimax_model_hint: str | None  # 'hd' / 'turbo' / None (only for minimax)


def _parse_speakers(speakers: list[dict]) -> list[_SpeakerSpec]:
    """Coerce raw payload list into validated _SpeakerSpec entries.

    Skips entries with missing voice_id / tts_provider, or with non-dict
    shape. Logs a warn when an entry is dropped so observability isn't
    silent.
    """
    parsed: list[_SpeakerSpec] = []
    for sp in speakers or []:
        if not isinstance(sp, dict):
            continue
        voice_id = str(sp.get("voice_id") or "").strip()
        tts_provider = str(sp.get("tts_provider") or "").strip().lower()
        if not voice_id or not tts_provider:
            continue
        speaker_id = str(sp.get("speaker_id") or "").strip()
        minimax_model_hint = str(sp.get("minimax_model") or "").strip().lower() or None
        parsed.append(_SpeakerSpec(
            speaker_id=speaker_id,
            voice_id=voice_id,
            tts_provider=tts_provider,
            minimax_model_hint=minimax_model_hint,
        ))
    return parsed


# ---------------------------------------------------------------------------
# F-v4.3-1: user-first lookup + batch CPS snapshot
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    """A single speaker spec resolved against DB:
    - which scope it lives in (user_voices vs voice_catalog)
    - the ``chars_per_second_by_model`` JSONB snapshot at query time

    Built by ``_resolve_targets_user_first``.
    """
    spec: _SpeakerSpec
    scope: str  # "user" or "catalog"
    by_model_snapshot: dict | None  # JSONB content, or None if row missing


async def _resolve_targets_user_first(
    db,
    *,
    owner_id: str,
    specs: list[_SpeakerSpec],
) -> list[_ResolvedTarget]:
    """For each minimax spec, decide scope + fetch by_model snapshot.

    F-v4.3-1 fix: trust ``(owner_id, voice_id)`` against user_voices BEFORE
    falling back to voice_catalog. Frontend's ``voice_source`` field is
    NOT used for routing (see module docstring §2).

    Non-minimax specs are returned as ``scope='skipped'`` markers so the
    caller can surface them in outcomes (informational, not calibrated).
    """
    # We only T0-C-bound calibrate minimax in phase 1; route others to
    # a skipped outcome so callers can build informational entries.
    minimax_specs = [s for s in specs if s.tts_provider == "minimax"]
    other_specs = [s for s in specs if s.tts_provider != "minimax"]

    resolved: list[_ResolvedTarget] = []

    if minimax_specs:
        # Models lazy-imported to keep cold-start cheap and avoid pulling
        # ORM mapper into modules that don't need it.
        from models import UserVoice
        from voice_catalog_models import VoiceCatalog

        voice_ids = list({s.voice_id for s in minimax_specs})

        # Phase A: user_voices probe. Single query for all voice_ids
        # under this user. Match by (user_id, voice_id) — matches the
        # uq_user_voices_user_voice unique constraint exactly.
        user_rows = (await db.execute(
            select(UserVoice).where(
                UserVoice.user_id == owner_id,
                UserVoice.voice_id.in_(voice_ids),
            )
        )).scalars().all()
        user_row_map = {r.voice_id: r for r in user_rows}

        # Phase B: catalog probe for the leftovers. Filter explicitly by
        # provider='minimax' + archived_at IS NULL so cross-provider
        # voice_id collisions or retired voices don't pollute. Phase 1 is
        # MiniMax-only so this is a single SELECT.
        # (codex v4.2 F-v4.2-6)
        leftover_voice_ids = [
            vid for vid in voice_ids if vid not in user_row_map
        ]
        catalog_row_map: dict[str, "VoiceCatalog"] = {}
        if leftover_voice_ids:
            catalog_rows = (await db.execute(
                select(VoiceCatalog).where(
                    VoiceCatalog.provider == "minimax",
                    VoiceCatalog.voice_id.in_(leftover_voice_ids),
                    VoiceCatalog.archived_at.is_(None),
                )
            )).scalars().all()
            catalog_row_map = {r.voice_id: r for r in catalog_rows}

        for spec in minimax_specs:
            user_row = user_row_map.get(spec.voice_id)
            if user_row is not None:
                resolved.append(_ResolvedTarget(
                    spec=spec, scope="user",
                    by_model_snapshot=user_row.chars_per_second_by_model,
                ))
                continue
            catalog_row = catalog_row_map.get(spec.voice_id)
            if catalog_row is not None:
                resolved.append(_ResolvedTarget(
                    spec=spec, scope="catalog",
                    by_model_snapshot=catalog_row.chars_per_second_by_model,
                ))
                continue
            # Voice not found in either table — orphan. Log and mark
            # as skipped; pipeline will fall back to default CPS.
            logger.warning(
                "[t2-preflight] voice not in user_voices/voice_catalog "
                "owner=%s voice_id=%s — skipping",
                owner_id, spec.voice_id,
            )
            resolved.append(_ResolvedTarget(
                spec=spec, scope="not_found", by_model_snapshot=None,
            ))

    for spec in other_specs:
        # CosyVoice / VolcEngine: log + skip (T0-C-2 future scope).
        logger.info(
            "[t2-preflight] non-minimax provider deferred provider=%s voice_id=%s",
            spec.tts_provider, spec.voice_id,
        )
        resolved.append(_ResolvedTarget(
            spec=spec, scope="provider_deferred", by_model_snapshot=None,
        ))

    return resolved


# ---------------------------------------------------------------------------
# Per-target factory (mirrors T1's calibrate_after_clone factory shape)
# ---------------------------------------------------------------------------


async def _run_one_review_calibration(
    *,
    scope: str,
    owner_id: str,
    voice_id: str,
    model_key: str,
):
    """Factory body for run_calibration_task.

    Plan v4.3 T0-D / T1 mirror:
    - ALWAYS returns CalibrationResult, NEVER raises.
    - paid TTS via ``calibrate_voice`` (T0-C bounded, 60s total).
    - DB write via INDEPENDENT short session with FOR UPDATE merge.
      For scope='user': writes to user_voices.
      For scope='catalog': writes to voice_catalog.
    """
    from voice_speed_calibrator import CalibrationResult, calibrate_voice
    from database import async_session
    from user_voice_service import (
        VoiceNotFoundError,
        update_user_voice_speed_calibration,
    )
    from voice_catalog_service import (
        CatalogVoiceNotFoundError,
        update_catalog_voice_speed_calibration,
    )

    result = await asyncio.to_thread(
        calibrate_voice,
        provider="minimax",
        model=model_key,
        voice_id=voice_id,
        total_timeout_seconds=60.0,
    )
    if not result.ok:
        return result

    try:
        async with async_session() as db_write:
            if scope == "user":
                await update_user_voice_speed_calibration(
                    db_write,
                    voice_id=voice_id,
                    user_id=owner_id,
                    cps=result.cps,
                    model_key=model_key,
                )
            elif scope == "catalog":
                await update_catalog_voice_speed_calibration(
                    db_write,
                    provider="minimax",
                    voice_id=voice_id,
                    cps=result.cps,
                    model_key=model_key,
                )
            else:
                # Defensive: should never reach here because resolver
                # filters out scope='not_found'/'provider_deferred'
                # before launching tasks.
                logger.error(
                    "[t2-preflight] unexpected scope=%s voice_id=%s — paid TTS already fired, NOT writing DB",
                    scope, voice_id,
                )
                return CalibrationResult(
                    ok=False,
                    error=f"unexpected scope: {scope}",
                    error_class="internal_error",
                    paid_call_count=result.paid_call_count,
                    per_text=result.per_text,
                    cps=result.cps,
                    model_key=model_key,
                )
    except (VoiceNotFoundError, CatalogVoiceNotFoundError) as exc:
        # Voice was deleted between resolve and write.
        return CalibrationResult(
            ok=False,
            error=f"voice_not_found at write time: {exc!r}"[:300],
            error_class="voice_not_found",
            paid_call_count=result.paid_call_count,
            per_text=result.per_text,
            cps=result.cps,
            model_key=model_key,
        )
    except Exception as exc:
        logger.exception(
            "[t2-preflight] DB write failed scope=%s voice_id=%s model_key=%s "
            "paid_call_count=%d",
            scope, voice_id, model_key, result.paid_call_count,
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


# ---------------------------------------------------------------------------
# Main entry: pre_flight_calibrate_voices
# ---------------------------------------------------------------------------


async def pre_flight_calibrate_voices(
    *,
    job_id: str,
    speakers: list[dict],
    batch_total_timeout_seconds: float = 50.0,
    max_concurrency: int = 4,
) -> list[dict]:
    """Pre-flight calibrate voices for a review submit. NEVER raises.

    The caller (``_approve_voice_selection_with_quality_sync``) MUST have
    rolled back its route ``db`` before invoking this so the route's
    connection is returned to the pool while we open our own short
    sessions.

    Returns
    -------
    list[dict]
        Per-spec outcome entries. Each dict has ``speaker_id``,
        ``voice_id``, ``tts_provider``, plus a ``status`` field:
        - ``"already_calibrated"``   — by_model[final_model] was already filled
        - ``"calibrated"``           — task completed within 50s, DB updated
        - ``"failed"``               — task ran but provider/DB error
        - ``"still_running"``        — task still pending at 50s, kept running in bg
        - ``"timeout"``              — synonym for still_running (covers wider sense)
        - ``"not_found"``            — voice not in user_voices nor voice_catalog
        - ``"provider_deferred"``    — non-minimax provider; T0-C-2 future
        - ``"no_minimax_model"``     — final_minimax_model is None (no minimax speakers)

    The shape is intentionally informational so the frontend can render a
    progress hint ("3/5 音色已校准, 2 个仍在后台进行...") and the gateway
    log can capture per-spec timings.

    Failure modes — by design, all of these return outcomes WITHOUT raising:
    - Job row missing (job_id not in PG): returns [].
    - Job has no user_id: returns [].
    - DB query exception during resolve phase: log + return informational
      outcomes for ALL specs as 'resolve_failed'.
    - Any individual task error: per-spec outcome 'failed'.
    """
    if not job_id or not speakers:
        return []

    # ----- Phase 1: parse speakers + derive final job-level minimax model
    parsed_specs = _parse_speakers(speakers)
    if not parsed_specs:
        return []

    # Reuse the same aggregation rule the route uses post-proxy
    # (job_intercept.py:_aggregate_quality_tier_from_speakers). We
    # re-implement here to avoid an import cycle (job_intercept imports
    # this module).
    has_minimax = any(s.tts_provider == "minimax" for s in parsed_specs)
    has_hd = any(
        s.tts_provider == "minimax" and s.minimax_model_hint == "hd"
        for s in parsed_specs
    )
    final_minimax_model: str | None
    if has_minimax and has_hd:
        final_minimax_model = "speech-2.8-hd"
    elif has_minimax:
        final_minimax_model = "speech-2.8-turbo"
    else:
        final_minimax_model = None

    if final_minimax_model is None:
        # No minimax speakers — nothing to calibrate in phase 1.
        return [_outcome_for_skip(s, "no_minimax_model") for s in parsed_specs]

    # ----- Phase 2: open short session, resolve owner_id + targets
    from database import async_session
    from models import Job

    try:
        async with async_session() as db_query:
            job_row = (await db_query.execute(
                select(Job).where(Job.job_id == job_id)
            )).scalar_one_or_none()
            owner_id = (
                str(job_row.user_id)
                if job_row is not None and job_row.user_id is not None
                else None
            )
            if owner_id is None:
                logger.warning(
                    "[t2-preflight] job_id=%s not in PG or user_id is None — abort",
                    job_id,
                )
                return []

            try:
                resolved_targets = await _resolve_targets_user_first(
                    db_query, owner_id=owner_id, specs=parsed_specs,
                )
            except Exception:
                logger.exception(
                    "[t2-preflight] resolve targets failed job_id=%s — degrading all specs to resolve_failed",
                    job_id,
                )
                return [_outcome_for_skip(s, "resolve_failed") for s in parsed_specs]
        # db_query closed here (async with) — connection back to pool.
    except Exception:
        logger.exception(
            "[t2-preflight] failed to open query session job_id=%s", job_id,
        )
        return [_outcome_for_skip(s, "session_failed") for s in parsed_specs]

    # ----- Phase 3: split into already-calibrated / to-launch / skipped
    outcomes: list[dict] = []
    to_launch: list[_ResolvedTarget] = []

    for target in resolved_targets:
        if target.scope in ("not_found", "provider_deferred"):
            outcomes.append(_outcome_from_target(target, status=target.scope))
            continue

        snap = target.by_model_snapshot or {}
        cps_existing = snap.get(final_minimax_model)
        if cps_existing is not None:
            outcomes.append(_outcome_from_target(
                target, status="already_calibrated",
                model_key=final_minimax_model,
                cps=float(cps_existing),
            ))
            continue
        to_launch.append(target)

    if not to_launch:
        return outcomes

    # ----- Phase 4: launch tasks under semaphore + asyncio.wait with hard cap
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _bounded_task(target: _ResolvedTarget):
        async with sem:
            return await _run_via_inflight_registry(
                target=target,
                owner_id=owner_id,
                final_minimax_model=final_minimax_model,
            )

    tasks: list[asyncio.Task] = [
        asyncio.create_task(_bounded_task(t)) for t in to_launch
    ]
    task_to_target = dict(zip(tasks, to_launch))

    done, pending = await asyncio.wait(
        tasks,
        timeout=batch_total_timeout_seconds,
        return_when=asyncio.ALL_COMPLETED,
    )

    for task in done:
        outcomes.append(_outcome_from_completed_task(
            task, task_to_target[task],
            model_key=final_minimax_model,
        ))

    for task in pending:
        # codex v4.1 F-v4.1-8 + v4.2 F-v4.2-4: done_callback ONLY logs.
        # The factory ALREADY wrote DB inside its own short session
        # before completing. The callback is purely observability.
        target = task_to_target[task]
        task.add_done_callback(
            lambda t, tgt=target: _log_background_outcome(t, tgt, final_minimax_model)
        )
        outcomes.append(_outcome_from_target(
            target, status="still_running",
            model_key=final_minimax_model,
        ))

    return outcomes


async def _run_via_inflight_registry(
    *,
    target: _ResolvedTarget,
    owner_id: str,
    final_minimax_model: str,
):
    """Bridge a resolved target to the shared run_calibration_task helper.

    The 5-tuple key uses owner=owner_id for user scope, owner='catalog'
    for catalog scope (matches voice_calibration_inflight CalibrationKey
    semantics). The factory passes through to ``_run_one_review_calibration``.
    """
    from voice_calibration_inflight import (
        CalibrationKey,
        run_calibration_task,
    )

    key = CalibrationKey(
        scope=target.scope,
        owner=owner_id if target.scope == "user" else "catalog",
        provider="minimax",
        voice_id=target.spec.voice_id,
        model_key=final_minimax_model,
    )

    async def _factory():
        return await _run_one_review_calibration(
            scope=target.scope,
            owner_id=owner_id,
            voice_id=target.spec.voice_id,
            model_key=final_minimax_model,
        )

    # Budget is charged to the user even when scope='catalog' — review
    # submit is a user-initiated action, so the user-level rate-limit
    # bucket is the correct gate (matches manual /calibrate-speed which
    # also charges the user regardless of voice scope).
    return await run_calibration_task(
        key=key,
        user_id_for_budget=owner_id,
        factory=_factory,
    )


# ---------------------------------------------------------------------------
# Outcome builders (small helpers — easier to test than inlined dicts)
# ---------------------------------------------------------------------------


def _base_outcome(spec: _SpeakerSpec) -> dict:
    return {
        "speaker_id": spec.speaker_id,
        "voice_id": spec.voice_id,
        "tts_provider": spec.tts_provider,
    }


def _outcome_for_skip(spec: _SpeakerSpec, status: str) -> dict:
    out = _base_outcome(spec)
    out["status"] = status
    return out


def _outcome_from_target(
    target: _ResolvedTarget,
    *,
    status: str,
    model_key: str | None = None,
    cps: float | None = None,
) -> dict:
    out = _base_outcome(target.spec)
    out["status"] = status
    out["scope"] = target.scope
    if model_key is not None:
        out["model_key"] = model_key
    if cps is not None:
        out["cps"] = cps
    return out


def _outcome_from_completed_task(
    task: asyncio.Task,
    target: _ResolvedTarget,
    *,
    model_key: str,
) -> dict:
    """Translate a finished asyncio.Task into an outcome dict.

    The task may have raised — even though the factory contract says
    "never raise", we defensively handle it (e.g. RateLimitExceeded
    leaking from the helper layer)."""
    out = _base_outcome(target.spec)
    out["scope"] = target.scope
    out["model_key"] = model_key

    if task.cancelled():
        out["status"] = "cancelled"
        return out

    exc = task.exception()
    if exc is not None:
        out["status"] = "failed"
        out["error_class"] = type(exc).__name__
        out["error"] = str(exc)[:200]
        return out

    result = task.result()
    if result is None:
        out["status"] = "failed"
        out["error_class"] = "no_result"
        return out

    if result.ok:
        out["status"] = "calibrated"
        out["cps"] = float(result.cps)
        out["paid_call_count"] = int(getattr(result, "paid_call_count", 0))
    else:
        out["status"] = "failed"
        out["error_class"] = getattr(result, "error_class", None) or "unknown"
        out["error"] = (getattr(result, "error", None) or "")[:200]
        out["paid_call_count"] = int(getattr(result, "paid_call_count", 0))

    return out


def _log_background_outcome(
    task: asyncio.Task,
    target: _ResolvedTarget,
    final_minimax_model: str,
) -> None:
    """Done-callback for tasks that exceeded the 50s batch window.

    Codex v4.1 F-v4.1-8: this callback ONLY logs. DB write happened
    inside the factory's own short session before the task completed.
    """
    try:
        if task.cancelled():
            logger.warning(
                "[t2-preflight] background task cancelled scope=%s voice_id=%s model_key=%s",
                target.scope, target.spec.voice_id, final_minimax_model,
            )
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "[t2-preflight] background task error scope=%s voice_id=%s model_key=%s exc=%r",
                target.scope, target.spec.voice_id, final_minimax_model, exc,
            )
            return
        result = task.result()
        if result is None:
            logger.warning(
                "[t2-preflight] background task returned None scope=%s voice_id=%s model_key=%s",
                target.scope, target.spec.voice_id, final_minimax_model,
            )
            return
        if result.ok:
            logger.info(
                "[t2-preflight] background calibration ok scope=%s voice_id=%s model_key=%s cps=%.4f",
                target.scope, target.spec.voice_id, final_minimax_model, float(result.cps),
            )
        else:
            logger.warning(
                "[t2-preflight] background calibration failed scope=%s voice_id=%s model_key=%s error_class=%s",
                target.scope, target.spec.voice_id, final_minimax_model,
                getattr(result, "error_class", None),
            )
    except Exception:
        # Done-callbacks must not raise — Python would log + drop them.
        logger.exception(
            "[t2-preflight] _log_background_outcome itself raised — swallowed"
        )

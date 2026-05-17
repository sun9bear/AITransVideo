"""Voice Selection Review API — Gateway-native endpoints for Studio mode voice clone.

Endpoints:
- POST /job-api/jobs/{job_id}/voice-clone  — clone a speaker's voice (with credits)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
import subprocess
import uuid
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_auth
from config import settings
from user_voice_service import (
    auto_reuse_summary_dict,
    candidate_to_dict,
)
from credits_service import (
    InsufficientCreditsError,
    ensure_credit_buckets_for_user,
    reserve_credits_or_raise,
    shadow_capture,
    shadow_release,
    shadow_safe,
)
from database import get_db
from models import Job, User, UserVoice

logger = logging.getLogger(__name__)

_SPEAKER_ID_RE = re.compile(r"^speaker_[a-z0-9_]+$")
_SEGMENT_ID_RE = re.compile(r"^[1-9][0-9]*$")
_CLONE_LOCK_TIMEOUT_SECONDS = 300
_VOICE_CLONE_RESERVE_REASON = "voice_clone_reserve"


def _iso_or_none(value: object) -> str | None:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()  # type: ignore[no-any-return, attr-defined]
        except Exception:
            return None
    return None


def _user_voice_to_dict(v: UserVoice) -> dict:
    return {
        "id": str(v.id),
        "voice_id": v.voice_id,
        "voice_type": v.voice_type,
        "provider": v.provider,
        "tts_provider": v.tts_provider,
        "platform": v.platform,
        "label": v.label,
        "source_speaker_id": v.source_speaker_id,
        "source_job_id": getattr(v, "source_job_id", None),
        "source_type": getattr(v, "source_type", None),
        "source_ref": getattr(v, "source_ref", None),
        "source_content_hash": getattr(v, "source_content_hash", None),
        "source_upload_md5": getattr(v, "source_upload_md5", None),
        "source_video_title": getattr(v, "source_video_title", None),
        "source_speaker_name": getattr(v, "source_speaker_name", None),
        "source_speaker_name_key": getattr(v, "source_speaker_name_key", None),
        "source_published_at": _iso_or_none(getattr(v, "source_published_at", None)),
        "source_content_summary": getattr(v, "source_content_summary", None),
        "source_content_era": getattr(v, "source_content_era", None),
        "source_content_tags": getattr(v, "source_content_tags", None),
        "clone_sample_seconds": getattr(v, "clone_sample_seconds", None),
        "clone_sample_segment_ids": getattr(v, "clone_sample_segment_ids", None),
        "created_from": getattr(v, "created_from", None),
        "notes": v.notes,
        "expired_at": _iso_or_none(v.expired_at),
        "created_at": _iso_or_none(v.created_at),
        "updated_at": _iso_or_none(v.updated_at),
    }


def _match_to_dict(match) -> dict:
    """Serialize a :class:`UserVoiceMatch` for the legacy ``voice-match``
    endpoint. Phase 1 (plan 2026-05-17) added ``match_scope`` so the
    frontend can distinguish strong-auto-reuse from needs-confirmation;
    older frontend builds ignore the unknown field.
    """
    return {
        "matched": True,
        "confidence": match.confidence,
        "reason": match.reason,
        "score": match.score,
        "auto_reuse_allowed": match.auto_reuse_allowed,
        "match_scope": getattr(match, "match_scope", None),
        "voice": _user_voice_to_dict(match.voice),
    }


def _provider_triplet_for_selection_match(data: dict) -> tuple[str | None, str | None, str | None]:
    provider = str(data.get("provider") or "").strip() or None
    tts_provider = str(data.get("tts_provider") or "").strip() or None
    platform = str(data.get("platform") or "").strip() or None
    selected_provider = str(data.get("selected_provider") or "").strip().lower()

    if selected_provider in {"minimax", "minimax_tts", "minimax_voice_clone"}:
        return (
            provider or "minimax_voice_clone",
            tts_provider or "minimax_tts",
            platform or "minimax_domestic",
        )
    if selected_provider and selected_provider not in {"minimax", "minimax_tts", "minimax_voice_clone"}:
        return (provider, tts_provider or selected_provider, platform)
    return (
        provider or "minimax_voice_clone",
        tts_provider or "minimax_tts",
        platform or "minimax_domestic",
    )


def _resolve_speaker_display_name(rs: dict, speaker_id: str) -> str | None:
    """Resolve a speaker's friendly display name (typically Chinese) from
    review_state, used to label cloned voices in the user library.

    Returns the trimmed name string, or None if no display name found.
    Strategies are tried in order — current schema first, then legacy
    fallback (so old review_state.json layouts keep working):

    1. ``voice_selection_review.payload.speakers[i].speaker_name`` for the
       matching ``speaker_id`` (current schema as of 2026-04-15)
    2. ``translation_review.payload.segments[*].display_name`` for any
       segment whose speaker_id matches
    3. (legacy) ``payload.speaker_names`` dict in either review stage
    4. (legacy) ``payload.speaker_name_a`` / ``speaker_name_b`` for the
       binary-speaker case

    Returning to fallback strategies prevents this function from breaking
    if the review_state schema is rolled forward without updating callers.
    """
    if not isinstance(rs, dict):
        return None
    stages = rs.get("stages", {})
    if not isinstance(stages, dict):
        return None

    # Strategy 1: voice_selection_review speakers[] (current schema)
    vsr_payload = (stages.get("voice_selection_review") or {}).get("payload") or {}
    for sp in (vsr_payload.get("speakers") or []):
        if isinstance(sp, dict) and sp.get("speaker_id") == speaker_id:
            name = sp.get("speaker_name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    # Strategy 2: translation_review per-segment display_name
    tr_payload = (stages.get("translation_review") or {}).get("payload") or {}
    segments = tr_payload.get("segments")
    if isinstance(segments, dict):
        for seg in segments.values():
            if isinstance(seg, dict) and seg.get("speaker_id") == speaker_id:
                name = seg.get("display_name")
                if isinstance(name, str) and name.strip():
                    return name.strip()

    # Strategy 3 + 4: legacy fallback (older review_state schemas)
    for stage_key in ("voice_selection_review", "translation_review"):
        payload = (stages.get(stage_key) or {}).get("payload") or {}
        name_map = payload.get("speaker_names")
        if isinstance(name_map, dict):
            candidate = name_map.get(speaker_id)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        direct_a = payload.get("speaker_name_a")
        direct_b = payload.get("speaker_name_b")
        if speaker_id == "speaker_a" and isinstance(direct_a, str) and direct_a.strip():
            return direct_a.strip()
        if speaker_id == "speaker_b" and isinstance(direct_b, str) and direct_b.strip():
            return direct_b.strip()

    return None


def _get_clone_cost_credits() -> int:
    """Read voice clone cost from runtime pricing, fallback to 500."""
    try:
        from pricing_runtime import get_runtime_pricing
        return get_runtime_pricing().credits.voice_clone_cost_credits
    except Exception:
        return 500


async def _commit_shadow(db: AsyncSession, label: str) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("%s credit ledger commit failed (non-fatal)", label, exc_info=True)


async def _verify_job_ownership(
    job_id: str,
    db: AsyncSession,
    user: User | None,
) -> Job | None:
    """Verify ownership and return the Job row."""
    if not settings.auth_required or user is None:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        return result.scalar_one_or_none()
    result = await db.execute(
        select(Job).where(Job.job_id == job_id, Job.user_id == user.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        result2 = await db.execute(select(Job).where(Job.job_id == job_id))
        if result2.scalar_one_or_none() is not None:
            raise HTTPException(status_code=403, detail="无权访问此任务")
    return job


def _get_project_dir(job: Job | None) -> Path | None:
    """Extract project_dir from job metadata."""
    if job is None:
        return None
    snapshot = job.metering_snapshot or {}
    pd = snapshot.get("project_dir")
    if pd:
        return Path(pd)
    return None


def _acquire_clone_lock(project_dir: Path, speaker_id: str) -> tuple[bool, str | None]:
    """Mark a speaker as cloning in review_state, unless a fresh lock exists."""
    from services.review_state import (
        REVIEW_STATUS_PENDING,
        VOICE_SELECTION_REVIEW_STAGE,
        ReviewStateManager,
    )

    review_state_path = project_dir / "review_state.json"
    if not review_state_path.exists():
        return True, None

    manager = ReviewStateManager(review_state_path)
    stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    if not stage or stage.get("status") != REVIEW_STATUS_PENDING:
        return True, None

    payload = dict(stage.get("payload") or {})
    speakers = payload.get("speakers")
    if not isinstance(speakers, list):
        return True, None

    now = datetime.now(timezone.utc)
    for speaker in speakers:
        if str(speaker.get("speaker_id", "")).strip() != speaker_id:
            continue

        cloning = speaker.get("cloning")
        if isinstance(cloning, dict):
            started_at_raw = cloning.get("started_at")
            if isinstance(started_at_raw, str) and started_at_raw.strip():
                try:
                    started_at = datetime.fromisoformat(started_at_raw)
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    elapsed = (now - started_at).total_seconds()
                    if elapsed < _CLONE_LOCK_TIMEOUT_SECONDS:
                        return False, "该说话人正在克隆音色，请稍候重试"
                except ValueError:
                    logger.warning("Invalid clone lock timestamp for %s: %s", speaker_id, started_at_raw)

        speaker["cloning"] = {"started_at": now.isoformat()}
        manager.set_stage(
            VOICE_SELECTION_REVIEW_STAGE,
            status=stage.get("status", REVIEW_STATUS_PENDING),
            payload=payload,
        )
        return True, None

    return True, None


def _clear_clone_lock(project_dir: Path, speaker_id: str) -> None:
    """Remove the clone-in-progress marker for a speaker."""
    from services.review_state import (
        REVIEW_STATUS_PENDING,
        VOICE_SELECTION_REVIEW_STAGE,
        ReviewStateManager,
    )

    review_state_path = project_dir / "review_state.json"
    if not review_state_path.exists():
        return

    manager = ReviewStateManager(review_state_path)
    stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    if not stage or stage.get("status") != REVIEW_STATUS_PENDING:
        return

    payload = dict(stage.get("payload") or {})
    speakers = payload.get("speakers")
    if not isinstance(speakers, list):
        return

    updated = False
    for speaker in speakers:
        if str(speaker.get("speaker_id", "")).strip() != speaker_id:
            continue
        if "cloning" in speaker:
            speaker.pop("cloning", None)
            updated = True
        break

    if updated:
        manager.set_stage(
            VOICE_SELECTION_REVIEW_STAGE,
            status=stage.get("status", REVIEW_STATUS_PENDING),
            payload=payload,
        )


async def get_voice_selection_pricing(
    request: Request,
    user: User | None = Depends(require_auth),
) -> dict:
    """Return credits-per-minute rates for voice selection display.

    Values come from Gateway truth sources (pricing_runtime + DEBIT_RATES),
    never from frontend hardcoded constants.
    """
    from credits_service import _get_runtime_debit_rates

    rates = _get_runtime_debit_rates()
    return {
        "service_mode": "studio",
        "credits_per_minute": {
            "volcengine": rates.get(("studio", "standard"), 15),
            "cosyvoice": rates.get(("studio", "standard"), 15),
            "minimax_turbo": rates.get(("studio", "high"), 30),
            "minimax_hd": rates.get(("studio", "flagship"), 50),
        },
        "voice_clone_cost_credits": _get_clone_cost_credits(),
    }


async def voice_match_for_selection(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /job-api/jobs/{job_id}/voice-match

    Read-only Studio helper used before showing the paid clone action.
    It never calls the clone provider and never reserves or captures credits.
    """
    job = await _verify_job_ownership(job_id, db, user)
    if job is None:
        return _json_response(404, {"error": "job_not_found", "message": "任务不存在"})

    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        return _json_response(400, {"error": "invalid_body", "message": "请求体格式错误"})
    if not isinstance(data, dict):
        return _json_response(400, {"error": "invalid_body", "message": "请求体格式错误"})

    speaker_id = str(data.get("speaker_id", "")).strip()
    if not _SPEAKER_ID_RE.match(speaker_id):
        return _json_response(400, {"error": "invalid_speaker_id", "message": f"无效的 speaker_id: {speaker_id}"})

    user_id = getattr(user, "id", None) or getattr(job, "user_id", None)
    if user_id is None:
        return _json_response(200, {
            "matched": False,
            "confidence": None,
            "auto_reuse_allowed": False,
            "reason": "missing_user_id",
            "voice": None,
            "candidates": [],
        })

    source_content_hash = str(getattr(job, "source_content_hash", "") or "").strip()
    if not source_content_hash:
        return _json_response(200, {
            "matched": False,
            "confidence": None,
            "auto_reuse_allowed": False,
            "reason": "missing_source_content_hash",
            "voice": None,
            "candidates": [],
        })

    try:
        limit = int(data.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    provider, tts_provider, platform = _provider_triplet_for_selection_match(data)

    try:
        from user_voice_service import match_user_voices
        matches = await match_user_voices(
            db,
            user_id=user_id,
            source_content_hash=source_content_hash,
            source_speaker_id=speaker_id,
            source_speaker_name=data.get("speaker_name"),
            source_speaker_name_key=data.get("source_speaker_name_key"),
            provider=provider,
            tts_provider=tts_provider,
            platform=platform,
            limit=limit,
        )
    except Exception as exc:
        logger.warning("voice-match failed for %s/%s: %s", job_id, speaker_id, exc, exc_info=True)
        return _json_response(500, {"error": "match_failed", "message": "查询可复用音色失败"})

    if not matches:
        return _json_response(200, {
            "matched": False,
            "confidence": None,
            "auto_reuse_allowed": False,
            "reason": "no_candidate",
            "voice": None,
            "candidates": [],
        })

    top = matches[0]
    return _json_response(200, {
        "matched": True,
        "confidence": top.confidence,
        "auto_reuse_allowed": top.auto_reuse_allowed,
        "reason": top.reason,
        "voice": _user_voice_to_dict(top.voice),
        "candidates": [_match_to_dict(match) for match in matches],
    })


async def voice_candidates_for_selection(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /job-api/jobs/{job_id}/voice-candidates

    Unified candidate endpoint for Studio + Post-edit. Returns the
    structured envelope defined in plan 2026-05-17 §Phase 1:

    - ``auto_reuse_voice``: top strong match (or ``None``).
    - ``personal_voice_candidates``: ordered list of personal-voice
      candidates (strong + needs-confirmation + cross-source named).
    - ``official_voice_candidates``: always ``[]`` in Phase 1; Phase 2
      wires the official voice picker.

    Like :func:`voice_match_for_selection` this is read-only — it
    never calls the clone provider and never reserves credits. It
    differs by defaulting ``include_cross_source=True`` so the
    frontend sees cross-video same-name candidates as well.
    """
    job = await _verify_job_ownership(job_id, db, user)
    if job is None:
        return _json_response(404, {"error": "job_not_found", "message": "任务不存在"})

    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        return _json_response(400, {"error": "invalid_body", "message": "请求体格式错误"})
    if not isinstance(data, dict):
        return _json_response(400, {"error": "invalid_body", "message": "请求体格式错误"})

    speaker_id = str(data.get("speaker_id", "")).strip()
    if not _SPEAKER_ID_RE.match(speaker_id):
        return _json_response(400, {"error": "invalid_speaker_id", "message": f"无效的 speaker_id: {speaker_id}"})

    user_id = getattr(user, "id", None) or getattr(job, "user_id", None)
    if user_id is None:
        # No user context — can't query a per-user library. Empty envelope.
        return _json_response(200, {
            "speaker_id": speaker_id,
            "source_content_hash": None,
            "auto_reuse_voice": None,
            "personal_voice_candidates": [],
            "official_voice_candidates": [],
        })

    source_content_hash = str(getattr(job, "source_content_hash", "") or "").strip() or None

    try:
        limit = int(data.get("limit") or 3)
    except (TypeError, ValueError):
        limit = 3

    include_cross_source = data.get("include_cross_source")
    if include_cross_source is None:
        include_cross_source = True
    include_cross_source = bool(include_cross_source)

    provider, tts_provider, platform = _provider_triplet_for_selection_match(data)

    try:
        from user_voice_service import match_user_voices
        matches = await match_user_voices(
            db,
            user_id=user_id,
            source_content_hash=source_content_hash,
            source_speaker_id=speaker_id,
            source_speaker_name=data.get("speaker_name"),
            source_speaker_name_key=data.get("source_speaker_name_key"),
            provider=provider,
            tts_provider=tts_provider,
            platform=platform,
            limit=limit,
            include_cross_source=include_cross_source,
        )
    except Exception as exc:
        logger.warning(
            "voice-candidates failed for %s/%s: %s",
            job_id, speaker_id, exc, exc_info=True,
        )
        return _json_response(500, {"error": "candidate_lookup_failed", "message": "查询个人音色候选失败"})

    auto_reuse_voice = None
    if matches and matches[0].auto_reuse_allowed:
        auto_reuse_voice = auto_reuse_summary_dict(matches[0])

    return _json_response(200, {
        "speaker_id": speaker_id,
        "source_content_hash": source_content_hash,
        "auto_reuse_voice": auto_reuse_voice,
        "personal_voice_candidates": [candidate_to_dict(match) for match in matches],
        # Phase 1: official voice picker integration is Phase 2 territory.
        "official_voice_candidates": [],
    })


async def voice_clone_for_selection(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /job-api/jobs/{job_id}/voice-clone

    Clone a speaker's voice from selected audio segments.
    Credits are shadow-reserved before clone, captured on success, released on failure.
    """
    job = await _verify_job_ownership(job_id, db, user)

    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        return _json_response(400, {"error": "invalid_body", "message": "请求体格式错误"})

    speaker_id = str(data.get("speaker_id", "")).strip()
    segment_ids = data.get("segment_ids", [])

    # Validate speaker_id
    if not _SPEAKER_ID_RE.match(speaker_id):
        return _json_response(400, {"error": "invalid_speaker_id", "message": f"无效的 speaker_id: {speaker_id}"})

    # Validate segment_ids
    if not isinstance(segment_ids, list) or not segment_ids:
        return _json_response(400, {"error": "invalid_segment_ids", "message": "至少选择一个音频片段"})
    for sid in segment_ids:
        if not isinstance(sid, int) or sid < 1:
            return _json_response(400, {"error": "invalid_segment_ids", "message": f"无效的 segment_id: {sid}"})

    # Get project_dir from upstream Job API
    from proxy import proxy_request as _proxy
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.job_api_upstream}/jobs/{job_id}/review-state",
                timeout=10.0,
            )
            if resp.status_code != 200:
                return _json_response(502, {"error": "upstream_error", "message": "无法获取任务状态"})
            review_data = resp.json()
    except Exception as exc:
        logger.exception("Failed to get review state for %s", job_id)
        return _json_response(502, {"error": "upstream_error", "message": str(exc)[:200]})

    project_dir_str = review_data.get("results", {}).get("project_dir")
    if not project_dir_str:
        return _json_response(400, {"error": "no_project_dir", "message": "任务没有可用的项目目录"})
    project_dir = Path(project_dir_str)

    lock_acquired = False
    try:
        lock_acquired, lock_message = _acquire_clone_lock(project_dir, speaker_id)
        if not lock_acquired:
            return _json_response(409, {"error": "clone_in_progress", "message": lock_message or "该说话人正在克隆音色，请稍候重试"})

        # Load transcript to get segment timestamps
        transcript_path = project_dir / "transcript" / "transcript.json"
        if not transcript_path.exists():
            return _json_response(400, {"error": "no_transcript", "message": "找不到转录文件"})

        try:
            transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
        except Exception:
            return _json_response(500, {"error": "transcript_read_error", "message": "读取转录文件失败"})

        lines = transcript_data if isinstance(transcript_data, list) else transcript_data.get("lines", [])

        # Filter segments for this speaker
        selected_segments = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            if str(line.get("speaker_id", "")).strip() != speaker_id:
                continue
            idx = line.get("index")
            if idx in segment_ids:
                selected_segments.append(line)

        if not selected_segments:
            return _json_response(400, {"error": "no_matching_segments", "message": "找不到匹配的音频片段"})

        # Validate total duration
        total_duration_s = sum(
            (int(seg.get("end_ms", 0)) - int(seg.get("start_ms", 0))) / 1000.0
            for seg in selected_segments
        )
        if total_duration_s < 10:
            return _json_response(400, {"error": "insufficient_duration", "message": f"选中片段总时长 {total_duration_s:.1f}s，至少需要 10s"})
        if total_duration_s >= 300:
            return _json_response(400, {"error": "excessive_duration", "message": f"选中片段总时长 {total_duration_s:.1f}s，不能超过 300s"})

        # Live reserve credits (from runtime pricing, fallback 500)
        clone_cost = _get_clone_cost_credits()
        user_id = user.id if user else None
        reserve_reason_code = f"{_VOICE_CLONE_RESERVE_REASON}_{uuid.uuid4().hex[:12]}"
        if user_id:
            await ensure_credit_buckets_for_user(db, user=user)
            try:
                await reserve_credits_or_raise(
                    db,
                    user_id=user_id,
                    job_id=job_id,
                    estimated_credits=clone_cost,
                    service_mode="studio",
                    reason_code=reserve_reason_code,
                )
                await _commit_shadow(db, "voice clone reserve")
            except InsufficientCreditsError as exc:
                await db.rollback()
                return _json_response(402, {
                    "error": "insufficient_credits",
                    "message": f"点数不足：克隆音色需要 {exc.required} 点，当前可用 {exc.available} 点。请充值或升级后再试。",
                    "detail": {
                        "required_credits": exc.required,
                        "available_credits": exc.available,
                    },
                })
            except Exception:
                logger.exception("voice clone credit reserve failed for %s", job_id)
                await db.rollback()
                return _json_response(500, {
                    "error": "credit_reserve_failed",
                    "message": "点数预扣失败，克隆流程已停止。请稍后重试。",
                })

        async def release_clone_credits(reason_code: str) -> None:
            if not user_id:
                return
            await shadow_safe(
                shadow_release,
                db,
                user_id=user_id,
                job_id=job_id,
                reason_code=reason_code,
                reserve_reason_code=reserve_reason_code,
            )
            await _commit_shadow(db, reason_code)

        # Find source audio
        source_audio = None
        for name in ("audio/speech_for_asr.wav", "audio/original.wav"):
            candidate = project_dir / name
            if candidate.exists():
                source_audio = candidate
                break
        if source_audio is None:
            await release_clone_credits("voice_clone_no_source_audio")
            return _json_response(400, {"error": "no_source_audio", "message": "找不到源音频文件"})

        # Concat selected segments via ffmpeg (run in executor to avoid blocking)
        loop = asyncio.get_event_loop()
        try:
            concat_path = await loop.run_in_executor(
                None,
                _concat_segments_ffmpeg,
                source_audio,
                selected_segments,
                project_dir,
                speaker_id,
            )
        except Exception as exc:
            logger.exception("ffmpeg concat failed for %s/%s", job_id, speaker_id)
            await release_clone_credits("voice_clone_concat_failed")
            return _json_response(500, {"error": "concat_failed", "message": f"音频拼接失败: {str(exc)[:200]}"})

        # Clone via MiniMax
        try:
            clone_result = await loop.run_in_executor(
                None,
                _clone_via_minimax,
                concat_path,
                speaker_id,
            )
        except Exception as exc:
            logger.exception("MiniMax clone failed for %s/%s", job_id, speaker_id)
            await release_clone_credits("voice_clone_failed")
            return _json_response(500, {"error": "clone_failed", "message": f"克隆失败: {str(exc)[:200]}"})

        try:
            from services.usage_meter import UsageMeter

            UsageMeter(project_dir, job_id=job_id).record_voice_clone(
                provider="minimax_voice_clone",
                model="voice_clone",
                voice_id=clone_result,
                speaker_id=speaker_id,
                source_audio_seconds=total_duration_s,
                source_audio_bytes=concat_path.stat().st_size if concat_path.exists() else 0,
                selected_segment_count=len(selected_segments),
                clone_count=1,
                billable=True,
                success=True,
                extra={
                    "billing_policy": "minimax_charges_voice_clone_on_first_tts_use",
                    "cost_estimate_timing": "clone_success",
                    "selected_segment_ids": segment_ids,
                },
            )
        except Exception:
            logger.warning("Voice clone usage metering skipped for %s/%s", job_id, speaker_id, exc_info=True)

        # Shadow capture on success
        if user_id:
            await shadow_safe(
                shadow_capture,
                db,
                user_id=user_id,
                job_id=job_id,
                actual_credits=clone_cost,
                service_mode="studio",
                reason_code="voice_clone_capture",
                reserve_reason_code=reserve_reason_code,
            )
            await _commit_shadow(db, "voice clone capture")

        # Write cloned voice to user's personal voice library.
        # Resolve a friendly Chinese display name from review_state instead of
        # the internal speaker_id (e.g. "查理·芒格 Clone" not "speaker_b Clone").
        display_speaker_name: str = speaker_id
        try:
            review_state_path = project_dir / "review_state.json"
            if review_state_path.exists():
                rs = json.loads(review_state_path.read_text(encoding="utf-8"))
                resolved = _resolve_speaker_display_name(rs, speaker_id)
                if resolved:
                    display_speaker_name = resolved
        except Exception:
            logger.debug("Could not resolve speaker display name for %s", speaker_id, exc_info=True)

        added_to_library = False
        if user_id:
            try:
                from user_voice_service import add_user_voice, build_cloned_voice_label
                await add_user_voice(
                    db,
                    user_id=user_id,
                    voice_id=clone_result,
                    label=build_cloned_voice_label(display_speaker_name),
                    provider="minimax_voice_clone",
                    tts_provider="minimax_tts",
                    platform="minimax_domestic",
                    source_speaker_id=speaker_id,
                    source_job_id=job_id,
                    source_type=getattr(job, "source_type", None) if job is not None else None,
                    source_ref=getattr(job, "source_ref", None) if job is not None else None,
                    source_content_hash=getattr(job, "source_content_hash", None) if job is not None else None,
                    source_video_title=(
                        getattr(job, "display_name", None)
                        or getattr(job, "title", None)
                        if job is not None else None
                    ),
                    source_speaker_name=display_speaker_name,
                    clone_sample_seconds=total_duration_s,
                    clone_sample_segment_ids=segment_ids,
                    created_from="studio_manual",
                    notes=f"从任务 {job_id} 克隆",
                )
                added_to_library = True
            except Exception:
                logger.exception("Failed to save cloned voice to user library")

        # Plan v4.3 §3.1 + §5.1 — T1 clone-after auto-calibration hook.
        # Only fire if (a) the voice was actually persisted to user_voices
        # (no row → no place to write chars_per_second_by_model), and
        # (b) the env gate isn't disabled. The hook itself is silent on
        # every failure path (rate limit, provider error, DB error) so
        # the clone response never blocks on calibration.
        #
        # Two background tasks (one per canonical MiniMax model) are
        # scheduled here. They run concurrently; the in-flight registry
        # de-dupes against any racing manual /calibrate-speed for the
        # same (user, voice, model) tuple.
        #
        # Background tasks own their own DB sessions — they do NOT touch
        # `db` (the route's session is closed by FastAPI dependency
        # teardown immediately after this function returns).
        if added_to_library and user_id:
            try:
                from voice_calibration_hook import (
                    CANONICAL_MODELS_BY_PROVIDER,
                    auto_calibrate_enabled,
                    calibrate_after_clone,
                )
                if auto_calibrate_enabled():
                    user_id_str = str(user_id)
                    # MiniMax voice clones always use minimax TTS; T0
                    # phase 1 only fans out for this provider (see
                    # voice_calibration_hook.CANONICAL_MODELS_BY_PROVIDER).
                    for model_key in CANONICAL_MODELS_BY_PROVIDER.get("minimax", ()):
                        asyncio.create_task(
                            calibrate_after_clone(
                                voice_id=clone_result,
                                user_id=user_id_str,
                                provider="minimax",
                                model_key=model_key,
                            )
                        )
            except Exception:
                # Even the scheduling step must not block the clone
                # response — log and move on.
                logger.exception(
                    "[auto-calibrate-clone] failed to enqueue tasks for voice_id=%s",
                    clone_result,
                )

        return _json_response(200, {
            "voice_id": clone_result,
            "status": "ready",
            "speaker_id": speaker_id,
        })
    finally:
        if lock_acquired:
            _clear_clone_lock(project_dir, speaker_id)


def _concat_segments_ffmpeg(
    source_audio: Path,
    segments: list[dict],
    project_dir: Path,
    speaker_id: str,
) -> Path:
    """Concat selected segments into a single WAV file (24kHz, mono, 16-bit PCM)."""
    # Create temp dir for intermediate files
    cache_dir = project_dir / "speaker_audio" / speaker_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Verify path is within project dir
    if not str(cache_dir.resolve()).startswith(str(project_dir.resolve())):
        raise ValueError("路径验证失败")

    # Build ffmpeg filter for segment extraction + concat
    filter_parts = []
    inputs = []
    for i, seg in enumerate(segments):
        start_s = int(seg["start_ms"]) / 1000.0
        end_s = int(seg["end_ms"]) / 1000.0
        filter_parts.append(
            f"[0:a]atrim=start={start_s}:end={end_s},asetpts=PTS-STARTPTS[s{i}]"
        )
        inputs.append(f"[s{i}]")

    concat_filter = ";".join(filter_parts) + ";"
    concat_filter += "".join(inputs) + f"concat=n={len(segments)}:v=0:a=1[out]"

    output_path = cache_dir / "clone_sample.wav"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(source_audio),
        "-filter_complex", concat_filter,
        "-map", "[out]",
        "-acodec", "pcm_s16le",
        "-ar", "24000",
        "-ac", "1",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg concat failed: {result.stderr.decode('utf-8', errors='replace')[:500]}"
        )

    return output_path


def _clone_via_minimax(concat_path: Path, speaker_id: str) -> str:
    """Upload + clone via MiniMax voice clone API with need_noise_reduction=true."""
    from services.voice_clone import VoiceCloneConfig, MiniMaxVoiceCloneClient
    from services import config_loader

    # IMPORTANT: VoiceCloneConfig.from_env(prefix, *, config_path) — the
    # positional arg is the env-var prefix string. Passing a Path here makes
    # the function look up env keys like "<path>CLONE_BASE_URL" which never
    # exist, then raise "Voice clone base_url is required …". Use keyword.
    clone_config = VoiceCloneConfig.from_env(config_path=config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH)
    clone_client = MiniMaxVoiceCloneClient(clone_config)
    result = clone_client.create_voice_clone(
        speaker_id=speaker_id,
        speaker_name=speaker_id,
        source_audio_path=concat_path,
        need_noise_reduction=True,
    )
    return result.voice_id


def _json_response(status_code: int, body: dict) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status_code,
        headers={"content-type": "application/json"},
    )

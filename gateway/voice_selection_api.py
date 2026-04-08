"""Voice Selection Review API — Gateway-native endpoints for Studio mode voice clone.

Endpoints:
- POST /job-api/jobs/{job_id}/voice-clone  — clone a speaker's voice (with credits)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_settings import load_settings
from auth import require_auth
from config import settings
from credits_service import shadow_capture, shadow_release, shadow_reserve, shadow_safe
from database import get_db
from models import Job, User

logger = logging.getLogger(__name__)

_SPEAKER_ID_RE = re.compile(r"^speaker_[a-z0-9_]+$")
_SEGMENT_ID_RE = re.compile(r"^[1-9][0-9]*$")


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

    # Shadow reserve credits
    admin_settings = load_settings()
    clone_cost = admin_settings.voice_clone_cost_credits
    user_id = user.id if user else None
    reserve_id: str | None = None
    if user_id:
        reserve_result = await shadow_safe(
            shadow_reserve,
            user_id=user_id,
            job_id=job_id,
            amount=clone_cost,
            reason_code="voice_clone",
            metadata_json=json.dumps({"speaker_id": speaker_id}),
            db=db,
        )
        reserve_id = reserve_result.get("reserve_id") if isinstance(reserve_result, dict) else None

    # Find source audio
    source_audio = None
    for name in ("audio/speech_for_asr.wav", "audio/original.wav"):
        candidate = project_dir / name
        if candidate.exists():
            source_audio = candidate
            break
    if source_audio is None:
        if reserve_id and user_id:
            await shadow_safe(shadow_release, reserve_id=reserve_id, db=db)
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
        if reserve_id and user_id:
            await shadow_safe(shadow_release, reserve_id=reserve_id, db=db)
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
        if reserve_id and user_id:
            await shadow_safe(shadow_release, reserve_id=reserve_id, db=db)
        return _json_response(500, {"error": "clone_failed", "message": f"克隆失败: {str(exc)[:200]}"})

    # Shadow capture on success
    if reserve_id and user_id:
        await shadow_safe(shadow_capture, reserve_id=reserve_id, db=db)

    # Write cloned voice to user's personal voice library
    if user_id:
        try:
            from user_voice_service import add_user_voice
            await add_user_voice(
                db,
                user_id=user_id,
                voice_id=clone_result,
                label=f"{speaker_id} Clone",
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                source_speaker_id=speaker_id,
                notes=f"从任务 {job_id} 克隆",
            )
        except Exception:
            logger.exception("Failed to save cloned voice to user library")

    return _json_response(200, {
        "voice_id": clone_result,
        "status": "ready",
        "speaker_id": speaker_id,
    })


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

    clone_config = VoiceCloneConfig.from_env(config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH)
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

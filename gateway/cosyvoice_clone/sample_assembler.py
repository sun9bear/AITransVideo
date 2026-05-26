"""Phase 4.2 A.2b: 从 job transcript 拼装 CosyVoice 克隆样本字节流。

替代用户上传 ``sample: UploadFile`` 的旧路径：用户在前端 modal 选 N 个
段号 → 后端验 ownership → 从 job 的源音频 + transcript 自动拼接 →
``audio_assembly.concat_segments_to_wav(..., target_sample_rate_hz=16000)``
→ 返回字节流给 endpoint 后续 validate / upload / worker 流程。

**关键安全边界 — 4 层 ownership 检查**（plan v4-followup §4.1 P1.3，
Codex 2026-05-26 v4-followup review 强调）：

1. **Job ownership**：``job.user_id == user.id``（或 admin override）。
   单层 ``source_job_id`` 校验不够 —— 攻击者可拼出别人 job_id 借声音。
2. **Project dir 存在**：``job.project_dir`` 非空且目录可读。
3. **Transcript loadable**：``project_dir/transcript/transcript.json`` 存
   在且可解析。
4. **Segment ownership**：每个 ``segment_id ∈ source_segments`` 必须
   - 存在于 transcript（``line.index`` 集合命中），且
   - ``line.speaker_id == claimed_speaker_id``（防 cross-speaker 借声音）

任一层失败 → 抛 typed exception，**绝不调 ffmpeg / OSS / worker**。
endpoint 层把 typed exception 转 HTTPException（403 / 404 / 400）。

前端筛选不算数：攻击者直接 POST endpoint 时只有后端这 4 层守得住。
守卫测试：``tests/test_cosyvoice_clone_sample_assembler.py``
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the same src/ injection pattern as api.py
for _candidate in [
    Path(__file__).resolve().parents[2] / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

# gateway/ paths already on sys.path
from audio_assembly import concat_segments_to_wav  # type: ignore[import-not-found]
from models import Job, User  # type: ignore[import-not-found]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed errors — endpoint layer maps each to a specific HTTP status
# ---------------------------------------------------------------------------


class SampleAssemblyError(Exception):
    """Base for sample assembly failures. Has a stable ``code`` for endpoint
    error response payload."""

    code: str = "sample_assembly_failed"
    http_status: int = 500


class JobNotFoundError(SampleAssemblyError):
    code = "job_not_found"
    http_status = 404


class JobOwnershipViolation(SampleAssemblyError):
    code = "job_ownership_violation"
    http_status = 403


class NoProjectDirError(SampleAssemblyError):
    code = "job_no_project_dir"
    http_status = 400


class TranscriptNotFoundError(SampleAssemblyError):
    code = "transcript_not_found"
    http_status = 400


class TranscriptParseError(SampleAssemblyError):
    code = "transcript_parse_error"
    http_status = 500


class EmptySegmentsError(SampleAssemblyError):
    code = "empty_segments"
    http_status = 400


class SegmentNotFoundError(SampleAssemblyError):
    code = "segment_not_found"
    http_status = 403  # 403 not 404 — refusing to leak whether the id exists in some other job

    def __init__(self, offending_segment_id: int):
        super().__init__(f"segment id {offending_segment_id} not found in transcript")
        self.offending_segment_id = offending_segment_id


class SpeakerOwnershipViolation(SampleAssemblyError):
    code = "segment_ownership_violation"
    http_status = 403

    def __init__(self, offending_segment_id: int, expected_speaker: str, actual_speaker: str):
        super().__init__(
            f"segment {offending_segment_id} belongs to speaker "
            f"{actual_speaker!r}, not {expected_speaker!r}"
        )
        self.offending_segment_id = offending_segment_id
        self.expected_speaker = expected_speaker
        self.actual_speaker = actual_speaker


class NoSourceAudioError(SampleAssemblyError):
    code = "no_source_audio"
    http_status = 400


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


_TARGET_SAMPLE_RATE_HZ_COSYVOICE = 16000  # CosyVoice DashScope expects 16k


async def assemble_sample_from_job_segments(
    db: AsyncSession,
    user: User,
    source_job_id: str,
    speaker_id: str,
    segment_ids: list[int],
) -> bytes:
    """Run the 4-layer ownership check + concat + read bytes.

    Args:
        db: async DB session for Job ownership lookup
        user: authenticated User; ``user.role == 'admin'`` bypasses
            cross-user ownership (consistent with other gateway endpoints
            like materials_api / pan)
        source_job_id: job id claimed by client; will be verified
        speaker_id: speaker id claimed by client; each segment must belong
            to this speaker
        segment_ids: list of segment_id (transcript ``line.index`` values)

    Returns:
        Concatenated WAV bytes (16kHz / mono / PCM s16le) ready for
        ``validate_sample_bytes`` + ``normalize_sample_for_dashscope`` in
        the endpoint pipeline.

    Raises:
        EmptySegmentsError: ``segment_ids`` empty
        JobNotFoundError: ``source_job_id`` not in DB
        JobOwnershipViolation: job belongs to a different user (and user
            is not admin)
        NoProjectDirError: job row has no ``project_dir``
        TranscriptNotFoundError: ``project_dir/transcript/transcript.json``
            missing
        TranscriptParseError: file unreadable / unparseable
        SegmentNotFoundError: any requested segment_id not in transcript
        SpeakerOwnershipViolation: any requested segment_id belongs to a
            speaker other than the claimed ``speaker_id``
        NoSourceAudioError: project_dir has no usable source audio
            (speech_for_asr.wav / original.wav)
        RuntimeError: ffmpeg concat failed (raised by audio_assembly)
    """
    # === Layer 0：input shape ===
    if not segment_ids:
        raise EmptySegmentsError("source_segments cannot be empty when using job-segments mode")

    # === Layer 1: Job ownership ===
    result = await db.execute(select(Job).where(Job.job_id == source_job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise JobNotFoundError(f"source_job_id={source_job_id!r} not found")
    if job.user_id != user.id and getattr(user, "role", "user") != "admin":
        # 403, not 404 — don't leak whether someone else's job exists
        raise JobOwnershipViolation(
            f"job {source_job_id} does not belong to user {user.id}"
        )

    # === Layer 2: project_dir ===
    project_dir_str = job.project_dir
    if not project_dir_str:
        raise NoProjectDirError(f"job {source_job_id} has no project_dir")
    project_dir = Path(project_dir_str)

    # === Layer 3: transcript loadable ===
    transcript_path = project_dir / "transcript" / "transcript.json"
    if not transcript_path.exists():
        raise TranscriptNotFoundError(
            f"transcript not found at {transcript_path}"
        )
    try:
        transcript_data: Any = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptParseError(f"failed to parse transcript: {exc}") from exc

    # Transcript may be either ``[{...}, ...]`` directly or
    # ``{"lines": [...]}`` — follow voice_selection_api convention.
    if isinstance(transcript_data, list):
        lines = transcript_data
    elif isinstance(transcript_data, dict):
        lines = transcript_data.get("lines", [])
    else:
        raise TranscriptParseError("transcript root must be list or dict")

    # === Layer 4: per-segment ownership ===
    # Build seg_id → line map for O(1) lookup.
    line_by_id: dict[int, dict] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        idx = line.get("index")
        if isinstance(idx, int):
            line_by_id[idx] = line

    selected_lines: list[dict] = []
    for seg_id in segment_ids:
        line = line_by_id.get(seg_id)
        if line is None:
            raise SegmentNotFoundError(seg_id)
        actual_speaker = str(line.get("speaker_id", "")).strip()
        if actual_speaker != speaker_id:
            raise SpeakerOwnershipViolation(
                offending_segment_id=seg_id,
                expected_speaker=speaker_id,
                actual_speaker=actual_speaker,
            )
        selected_lines.append(line)

    # === Layer 5: source audio existence ===
    source_audio: Path | None = None
    for name in ("audio/speech_for_asr.wav", "audio/original.wav"):
        candidate = project_dir / name
        if candidate.exists():
            source_audio = candidate
            break
    if source_audio is None:
        raise NoSourceAudioError(
            f"no usable source audio in {project_dir}/audio/"
        )

    # === Layer 6: concat (delegates to A.2a helper; raises RuntimeError on ffmpeg failure) ===
    output_path = concat_segments_to_wav(
        source_audio,
        selected_lines,
        project_dir,
        speaker_id,
        target_sample_rate_hz=_TARGET_SAMPLE_RATE_HZ_COSYVOICE,
    )

    # === Layer 7: read bytes + best-effort cleanup ===
    try:
        sample_bytes = output_path.read_bytes()
    finally:
        # Best effort: drop the temp file. The endpoint pipeline keeps
        # bytes in memory; leaving the file around is OK for debugging
        # but eats disk over time. unlink failure is non-fatal.
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "[sample_assembler] failed to unlink temp file %s", output_path
            )

    return sample_bytes

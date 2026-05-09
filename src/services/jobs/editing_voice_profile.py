"""Fire-and-forget voice profile inference for editing-mode speakers.

Triggered when a freshly-created editing speaker first gets a segment
assigned. Calls Pass 3 of the S2 reviewer (admin-configured multimodal
LLM, mode='studio' → see transcript_reviewer._get_prompt_model). LLM-only,
no TTS / no clone — D26 hard constraint.

Concurrency: a module-level ThreadPoolExecutor (max_workers=2). Tests
inject a dummy executor via monkeypatching ``_executor`` to run sync.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from services._file_lock import file_lock
from services.jobs.editing_speakers import (
    editing_speakers_path,
    load_speakers,
    save_speakers,
)
from services.transcript_reviewer import review_pass3_voice_profiles

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="editvp")


def _update_speaker_status(
    project_dir: str | Path,
    speaker_id: str,
    *,
    status: str,
    error: str | None = None,
    profile: dict | None = None,
) -> None:
    """Atomic in-place update of one speaker's profile_status / error / profile.

    Preserves all existing speakers' fields untouched. Caller specifies which
    of (status, error, profile) to set; profile=None means "leave as-is",
    which is what we want when transitioning to 'inferring' / 'pending_segments'.
    """
    path = editing_speakers_path(project_dir)
    with file_lock(path):
        speakers = load_speakers(project_dir)
        for sp in speakers:
            if sp.speaker_id == speaker_id:
                sp.profile_status = status
                sp.profile_error = error
                if profile is not None:
                    sp.voice_profile = profile
                break
        # Reuse save_speakers so the on-disk schema (version / speakers /
        # updated_at) stays in sync with create_speaker. Previously this
        # block hand-rolled a payload that omitted updated_at, leaving
        # speakers.json::updated_at frozen to whatever create_speaker last
        # wrote — a silent schema drift that broke any consumer keying off
        # the file-level timestamp.
        save_speakers(project_dir, speakers)


def _gather_inference_inputs(
    project_dir: Path | str, speaker_id: str,
) -> tuple[list[dict], Path | None, dict[str, dict]]:
    """Build (lines, source_audio_path, speakers) for review_pass3_voice_profiles.

    - lines: editing/segments.json → transcript-style lines (start_ms /
      end_ms / speaker_id / text). Pass 3 cuts per-speaker audio clips
      from these lines, so every line — including segments belonging to
      other speakers — contributes to the model's view of the audio.
    - source_audio_path: <project_dir>/audio/original.wav 主路径（与
      src/pipeline/process.py 中的 source_audio_path 一致）；不存在时
      退回扫 audio/*.wav 的第一个候选，仍找不到返 None（Pass 3 自己
      会做 fallback_minimal_speaker_styles）。
    - speakers: {speaker_id: {}} —— 仅为这一个 speaker 推断。Pass 3
      用空字典作 base，模型自己填 voice_description / gender / 等字段。
    """
    project_dir = Path(project_dir)
    seg_path = project_dir / "editor" / "editing" / "segments.json"
    segments_raw: list[dict] = []
    if seg_path.is_file():
        try:
            raw = json.loads(seg_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, dict):
            raw = raw.get("segments", [])
        if isinstance(raw, list):
            segments_raw = [s for s in raw if isinstance(s, dict)]

    # Pass 3 (transcript_reviewer._extract_speaker_audio_clips) 走属性访问:
    # `line.speaker_id` / `line.start_ms` / `line.end_ms` —— 直接传 dict 会
    # raise AttributeError 让 Pass 3 的 audio extraction 失败 (2026-05-09 实
    # 测过). 用 SimpleNamespace 把 dict 当对象包一层最小成本。
    lines = [
        SimpleNamespace(
            start_ms=int(s.get("start_ms", 0)),
            end_ms=int(s.get("end_ms", 0)),
            speaker_id=s.get("speaker_id"),
            text=s.get("source_text") or s.get("cn_text") or "",
            source_text=s.get("source_text") or "",
            cn_text=s.get("cn_text") or "",
            index=int(s.get("segment_id", 0)) if str(s.get("segment_id", "")).isdigit() else 0,
        )
        for s in segments_raw
    ]

    src_audio: Path | None = None
    primary = project_dir / "audio" / "original.wav"
    if primary.is_file():
        src_audio = primary
    else:
        audio_dir = project_dir / "audio"
        if audio_dir.is_dir():
            for cand in audio_dir.glob("*.wav"):
                src_audio = cand
                break

    return lines, src_audio, {speaker_id: {}}


def infer_voice_profile_for_speaker(
    project_dir: str | Path, speaker_id: str,
) -> None:
    """Synchronous body: marks 'inferring' → calls Pass 3 → 'ready'/'failed'.

    Fail-soft: any exception is caught and surfaced as profile_status='failed'
    with profile_error. Never raises so the executor thread won't surface a
    crashed callable to the calling HTTP handler thread.
    """
    project_dir = Path(project_dir)
    _update_speaker_status(project_dir, speaker_id, status="inferring")
    try:
        lines, src_audio, speakers_meta = _gather_inference_inputs(
            project_dir, speaker_id,
        )
        result = review_pass3_voice_profiles(
            lines=lines,
            source_audio_path=src_audio,
            speakers=speakers_meta,
            mode="studio",  # D3
        )
        profile = (result or {}).get(speaker_id) or {}
        _update_speaker_status(
            project_dir, speaker_id, status="ready", profile=profile,
        )
    except Exception as exc:  # fail-soft (D5)
        logger.exception(
            "editing voice profile inference failed for %s", speaker_id
        )
        _update_speaker_status(
            project_dir, speaker_id, status="failed", error=str(exc)[:200],
        )


def maybe_trigger_inference(
    project_dir: str | Path, speaker_id: str,
) -> None:
    """Idempotent: only fires if profile_status == 'pending_segments'.

    Non-blocking — submits to module-level ThreadPoolExecutor and returns
    immediately. Tests can monkeypatch ``_executor`` with a sync dummy.
    Any other status (inferring / ready / failed) is a no-op so users can't
    burn LLM quota by clicking PATCH multiple times.
    """
    speakers = load_speakers(project_dir)
    target = next(
        (s for s in speakers if s.speaker_id == speaker_id), None
    )
    if target is None or target.profile_status != "pending_segments":
        return
    _executor.submit(infer_voice_profile_for_speaker, project_dir, speaker_id)

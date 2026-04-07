"""Phase 2: Narrow review action helpers for Job API.

These functions implement the write-side review logic (approve, split, preview, clone)
using the job's verified project_dir as authority. They do NOT live inside JobService
to keep that class focused on job lifecycle.
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from services.jobs.models import JOB_STATUS_WAITING_FOR_REVIEW
from services.review_state import (
    REVIEW_STATUS_APPROVED,
    TRANSLATION_CONFIG_REVIEW_STAGE,
    TRANSLATION_REVIEW_STAGE,
    ReviewStateManager,
)


def approve_translation_config(
    *,
    project_dir: Path,
    selected_model: str | None,
    prompt_template: str | None,
) -> dict[str, object]:
    """Approve translation-config review (model + prompt selection).

    Writes the selected_model / prompt_template into the project's
    review_state.json under translation_config_review, then marks it APPROVED
    so the paused pipeline subprocess resumes.
    """
    review_state_path = Path(project_dir) / "review_state.json"
    manager = ReviewStateManager(review_state_path)
    payload: dict[str, object] = {}
    if selected_model:
        payload["selected_model"] = str(selected_model).strip()
    if prompt_template is not None:
        payload["prompt_template"] = prompt_template
    manager.set_stage(
        TRANSLATION_CONFIG_REVIEW_STAGE,
        status=REVIEW_STATUS_APPROVED,
        payload=payload,
    )
    return {"stage": TRANSLATION_CONFIG_REVIEW_STAGE, "payload": payload}


def approve_translation(
    *,
    project_dir: Path,
    segments_payload: object,
    segment_speakers: dict[str, str] | None,
) -> dict[str, object]:
    """Approve translation review for a specific project. Returns normalized payload."""
    from services.web_ui.translation_review import (
        _apply_segment_speakers_update_from_translation_review,
        _save_translation_review_submission,
    )

    if segment_speakers and isinstance(segment_speakers, dict) and len(segment_speakers) > 0:
        _apply_segment_speakers_update_from_translation_review(
            project_dir=project_dir,
            segment_speakers_update=segment_speakers,
        )

    result = _save_translation_review_submission(
        project_dir=project_dir,
        translation_segments_payload=segments_payload,
        status=REVIEW_STATUS_APPROVED,
    )
    return result


def split_segment(
    *,
    project_dir: Path,
    stage: str,
    segment_id: object,
    split_source_index: int | None,
    split_cn_index: int | None,
    speaker_a: str | None,
    speaker_b: str | None,
    pending_speaker_changes: dict[str, str] | None,
) -> dict[str, object]:
    """Split a segment in the target job's project."""
    from services.web_ui.translation_review import (
        _apply_segment_speakers_update_from_translation_review,
        _split_segment,
    )

    if pending_speaker_changes and isinstance(pending_speaker_changes, dict) and len(pending_speaker_changes) > 0:
        _apply_segment_speakers_update_from_translation_review(
            project_dir=project_dir,
            segment_speakers_update=pending_speaker_changes,
        )

    result = _split_segment(
        project_dir=project_dir,
        segment_id=segment_id,
        split_source_index=split_source_index,
        split_cn_index=split_cn_index,
        speaker_a=speaker_a,
        speaker_b=speaker_b,
    )
    return result


def preview_segment(
    *,
    project_dir: Path,
    segment_id: object,
    source_start_ms: float | None,
    source_end_ms: float | None,
    cn_text: str,
    voice_id: str,
    config_path: Path,
) -> dict[str, object]:
    """Generate preview audio for a segment: source clip + TTS preview."""
    source_audio_b64 = ""
    tts_audio_b64 = ""

    # Part 1: Extract source audio clip via ffmpeg
    if source_start_ms is not None and source_end_ms is not None:
        source_audio_path = _find_source_audio(project_dir)
        if source_audio_path is not None:
            try:
                start_s = source_start_ms / 1000.0
                end_s = source_end_ms / 1000.0
                result = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(source_audio_path),
                        "-ss", str(start_s),
                        "-to", str(end_s),
                        "-acodec", "pcm_s16le",
                        "-ar", "16000",
                        "-ac", "1",
                        "-f", "wav",
                        "pipe:1",
                    ],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0 and result.stdout:
                    source_audio_b64 = base64.b64encode(result.stdout).decode("ascii")
            except Exception:
                pass

    # Part 2: TTS preview
    if cn_text and voice_id:
        try:
            from services.voice_asset import VoiceAssetVerifier
            verifier = VoiceAssetVerifier.from_env(config_path=config_path)
            verify_result = verifier.verify_voice(
                speaker_id="preview",
                voice_id=voice_id,
                sample_text=cn_text,
            )
            output_path = getattr(verify_result, "output_path", None)
            if output_path and Path(output_path).exists():
                tts_audio_b64 = base64.b64encode(Path(output_path).read_bytes()).decode("ascii")
            else:
                import sys
                print(
                    f"[preview_segment] verify_voice returned no output_path for "
                    f"voice_id={voice_id!r} (result={verify_result!r})",
                    file=sys.stderr, flush=True,
                )
        except Exception as exc:
            import sys, traceback
            print(
                f"[preview_segment] TTS preview failed for voice_id={voice_id!r}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr, flush=True,
            )
            traceback.print_exc(file=sys.stderr)

    return {
        "source_audio_base64": source_audio_b64,
        "tts_audio_base64": tts_audio_b64,
        "source_format": "wav",
        "tts_format": "wav",
    }


def clone_voice(
    *,
    project_dir: Path,
    speaker_id: str,
    speaker_name: str | None,
    sample_path: str | None,
    config_path: Path,
    project_root: Path,
) -> dict[str, object]:
    """Clone a voice for the target job's project."""
    import re
    from services.web_ui.voice_library import _resolve_voice_registry_path

    effective_speaker_name = (speaker_name or "").strip() or speaker_id

    # Auto-extract sample if not provided
    if not sample_path or not sample_path.strip():
        sample_path = _auto_extract_voice_sample(
            project_dir=project_dir,
            speaker_id=speaker_id,
            speaker_name=effective_speaker_name,
        )

    # Clone via MiniMax
    from services.voice_clone import VoiceCloneConfig, MiniMaxVoiceCloneClient

    clone_config = VoiceCloneConfig.from_env(config_path)
    clone_client = MiniMaxVoiceCloneClient(clone_config)
    clone_result = clone_client.create_voice_clone(
        speaker_id=speaker_id,
        speaker_name=effective_speaker_name,
        source_audio_path=sample_path,
    )

    # Register in voice registry
    registry_path = _resolve_voice_registry_path(project_root=project_root, config_path=config_path)
    from services.voice_registry import VoiceRegistry
    registry = VoiceRegistry(str(registry_path))
    registry.register_voice(
        speaker_id=speaker_id,
        speaker_name=effective_speaker_name,
        voice_id=clone_result.voice_id,
        voice_type="cloned",
        provider="minimax_voice_clone",
        tts_provider="minimax_tts",
        platform="minimax_domestic",
        label=f"{effective_speaker_name} Clone",
        source_audio_path=sample_path,
        notes="从审核页面克隆",
    )
    registry.set_default_voice(speaker_id, clone_result.voice_id)

    return {
        "success": True,
        "voice_id": clone_result.voice_id,
        "speaker_id": speaker_id,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_source_audio(project_dir: Path) -> Path | None:
    """Find the best source audio file in the project."""
    for candidate_name in ("audio/speech_for_asr.wav", "audio/original.wav"):
        candidate = project_dir / candidate_name
        if candidate.exists():
            return candidate
    return None


def _auto_extract_voice_sample(
    *,
    project_dir: Path,
    speaker_id: str,
    speaker_name: str,
) -> str:
    """Extract a voice sample from transcript timestamps for auto-clone path."""
    import re

    source_audio = _find_source_audio(project_dir)
    if source_audio is None:
        raise ValueError("项目中找不到源音频文件。")

    transcript_path = project_dir / "transcript" / "transcript.json"
    if not transcript_path.exists():
        raise ValueError("项目中找不到转录文件。")

    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    lines = transcript_data if isinstance(transcript_data, list) else transcript_data.get("lines", [])
    speaker_lines = [
        line for line in lines
        if isinstance(line, dict) and str(line.get("speaker_id", "")).strip() == speaker_id
    ]
    if not speaker_lines:
        raise ValueError(f"转录文件中找不到 {speaker_id} 的语音片段。")

    # Build voice sample
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", speaker_name.lower().strip())
    samples_dir = project_dir / "voice_samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    output_path = samples_dir / f"{safe_name}_sample.wav"

    from services.voice.sample_extractor import VoiceSampleExtractor
    extractor = VoiceSampleExtractor()
    extractor.extract_sample(
        source_audio_path=str(source_audio),
        speaker_lines=speaker_lines,
        output_path=str(output_path),
    )
    return str(output_path)

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
    VOICE_SELECTION_REVIEW_STAGE,
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


_PREVIEW_SAMPLE_TEXT = "您好，很高兴能为您提供视频服务。请选择您感兴趣的音色，让我们一起开启视频翻译的奇妙之旅吧。"


def _is_volcengine_voice(voice_id: str) -> bool:
    return voice_id.startswith(("ICL_", "zh_", "en_", "saturn_"))


def preview_voice(
    *,
    voice_id: str,
    config_path: Path,
    tts_provider: str | None = None,
) -> dict[str, object]:
    """Preview a voice by synthesizing test text.

    Supports MiniMax (clone + official), CosyVoice, and VolcEngine.
    Uses explicit *tts_provider* when given; falls back to voice_id detection.
    """
    import base64

    if not voice_id or not voice_id.strip():
        raise ValueError("voice_id is required")

    voice_id = voice_id.strip()

    # --- Route 1: VolcEngine ---
    if tts_provider == "volcengine" or (not tts_provider and _is_volcengine_voice(voice_id)):
        return _preview_volcengine_voice(voice_id)

    # --- Route 2: CosyVoice ---
    if tts_provider == "cosyvoice":
        return _preview_cosyvoice_voice(voice_id)
    if not tts_provider:
        from services.tts.cosyvoice_voice_catalog import is_cosyvoice_v3_flash_builtin_voice
        if is_cosyvoice_v3_flash_builtin_voice(voice_id):
            return _preview_cosyvoice_voice(voice_id)

    # --- Route 3: MiniMax (clone + official catalog) ---
    from services.voice_asset import (
        VoiceAssetVerifier,
        VoiceAssetVerificationRuntimeError,
    )

    try:
        verifier = VoiceAssetVerifier.from_env(config_path=config_path)
        result = verifier.verify_voice(
            speaker_id="preview",
            voice_id=voice_id,
            sample_text=_PREVIEW_SAMPLE_TEXT,
        )
        output = Path(result.output_path)
        if output.exists() and output.stat().st_size > 0:
            audio_b64 = base64.b64encode(output.read_bytes()).decode("ascii")
            return {"audio_base64": audio_b64, "format": "wav", "expired": False, "error": None}
        return {"audio_base64": "", "format": "wav", "expired": False, "error": "生成的音频为空"}
    except VoiceAssetVerificationRuntimeError as exc:
        err_msg = str(exc).lower()
        is_expired = "2054" in err_msg or "voice id not exist" in err_msg or "voice_id not exist" in err_msg
        return {
            "audio_base64": "",
            "format": "wav",
            "expired": is_expired,
            "error": "音色已失效，请重新选择" if is_expired else f"试听失败: {str(exc)[:200]}",
        }
    except Exception as exc:
        return {"audio_base64": "", "format": "wav", "expired": False, "error": f"试听失败: {str(exc)[:200]}"}


def _preview_volcengine_voice(voice_id: str) -> dict[str, object]:
    """Preview a VolcEngine voice via TTS synthesis."""
    import base64

    try:
        from services.tts.volcengine_tts_provider import (
            RESOURCE_ID_1_0,
            RESOURCE_ID_2_0,
            synthesize as volc_synthesize,
        )

        # Auto-detect resource_id from voice_id pattern:
        # 2.0 voices: *_uranus_bigtts OR saturn_zh_* prefix
        # 1.0 voices: everything else (*_moon_bigtts, *_mars_bigtts, ICL_zh_*)
        is_2_0 = "uranus_bigtts" in voice_id or voice_id.startswith("saturn_zh_")
        resource_id = RESOURCE_ID_2_0 if is_2_0 else RESOURCE_ID_1_0

        wav_bytes = volc_synthesize(
            text=_PREVIEW_SAMPLE_TEXT,
            voice_id=voice_id,
            resource_id=resource_id,
        )
        if wav_bytes and len(wav_bytes) > 100:
            audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
            return {"audio_base64": audio_b64, "format": "wav", "expired": False, "error": None}
        return {"audio_base64": "", "format": "wav", "expired": False, "error": "生成的音频为空"}
    except Exception as exc:
        return {"audio_base64": "", "format": "wav", "expired": False, "error": f"试听失败: {str(exc)[:200]}"}


def _preview_cosyvoice_voice(voice_id: str) -> dict[str, object]:
    """Preview a CosyVoice voice via DashScope TTS synthesis."""
    import base64

    try:
        from services.tts.cosyvoice_provider import synthesize as cosy_synthesize

        wav_bytes = cosy_synthesize(text=_PREVIEW_SAMPLE_TEXT, voice=voice_id)
        if wav_bytes and len(wav_bytes) > 100:
            audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
            return {"audio_base64": audio_b64, "format": "wav", "expired": False, "error": None}
        return {"audio_base64": "", "format": "wav", "expired": False, "error": "生成的音频为空"}
    except Exception as exc:
        err_msg = str(exc).lower()
        # CosyVoice returns 418 for unavailable voices on certain endpoints.
        # This is NOT expiration (unlike MiniMax clone voices) — it's endpoint
        # availability.  Report as error, NOT expired, so the frontend does not
        # run user-voice cleanup logic on a builtin voice.
        is_unavailable = "418" in err_msg or "not support" in err_msg
        return {
            "audio_base64": "", "format": "wav", "expired": False,
            "error": "该音色在当前端点不可用，请切换端点或选择其他音色" if is_unavailable else f"试听失败: {str(exc)[:200]}",
        }


def _validate_voice_provider_compat(voice_id: str, tts_provider: str) -> None:
    """Reject obvious voice_id / tts_provider mismatches at approval time.

    CosyVoice and VolcEngine have recognisable voice_id patterns.
    MiniMax accepts any ID (cloned voices, official catalog with mixed
    formats like ``Wise_Woman``, ``moss_audio_xxx``, ``Chinese (Mandarin)_xxx``).
    """
    if tts_provider == "cosyvoice":
        from services.tts.cosyvoice_voice_catalog import is_cosyvoice_v3_flash_builtin_voice
        if not is_cosyvoice_v3_flash_builtin_voice(voice_id):
            raise ValueError(
                f"voice_id {voice_id!r} 不是 CosyVoice 音色。"
                f"请选择 CosyVoice 音色或切换 TTS 引擎。"
            )
    elif tts_provider == "volcengine":
        if not _is_volcengine_voice(voice_id):
            raise ValueError(
                f"voice_id {voice_id!r} 不是豆包音色。"
                f"请选择豆包音色或切换 TTS 引擎。"
            )
    # minimax / "" → no strict check (heterogeneous ID formats)


def approve_voice_selection(
    *,
    project_dir: Path,
    speakers: list[dict[str, str]],
) -> dict[str, object]:
    """Approve voice_selection_review with per-speaker voice bindings."""
    import re

    if not speakers:
        raise ValueError("至少需要一个说话人的音色选择")

    _SPEAKER_ID_RE = re.compile(r"^speaker_[a-z0-9_]+$")
    _VALID_TTS_PROVIDERS = {"minimax", "cosyvoice", "volcengine", ""}
    for sp in speakers:
        sid = sp.get("speaker_id", "")
        vid = sp.get("voice_id", "")
        if not sid or not vid:
            raise ValueError(f"每个说话人必须有 speaker_id 和 voice_id")
        if not _SPEAKER_ID_RE.match(sid):
            raise ValueError(f"无效的 speaker_id 格式: {sid}")
        sp_prov = sp.get("tts_provider", "")
        if sp_prov and sp_prov not in _VALID_TTS_PROVIDERS:
            raise ValueError(f"无效的 tts_provider: {sp_prov!r}，支持: minimax, cosyvoice, volcengine")
        # Validate voice_id / tts_provider compatibility.
        # CosyVoice and VolcEngine have deterministic voice_id patterns;
        # MiniMax is the default fallback (cloned voices + official catalog
        # with heterogeneous ID formats — no strict pattern check).
        if sp_prov and vid:
            _validate_voice_provider_compat(vid, sp_prov)

    # Check no speaker is still cloning
    review_state_path = Path(project_dir) / "review_state.json"
    manager = ReviewStateManager(review_state_path)
    stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    if stage:
        payload = stage.get("payload") or {}
        for sp_info in (payload.get("speakers") or []):
            cloning = sp_info.get("cloning")
            if isinstance(cloning, dict) and cloning.get("started_at"):
                import datetime
                started = cloning["started_at"]
                try:
                    started_dt = datetime.datetime.fromisoformat(started)
                    elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_dt).total_seconds()
                    if elapsed < 300:  # 5 minutes
                        raise ValueError("有说话人正在克隆音色，请等待完成后再确认")
                except (TypeError, ValueError) as exc:
                    if "正在克隆" in str(exc):
                        raise

    approved_payload = {
        "speakers": [
            {
                "speaker_id": sp["speaker_id"],
                "voice_id": sp["voice_id"],
                "voice_source": sp.get("voice_source", "catalog"),
                "tts_provider": sp.get("tts_provider", ""),
            }
            for sp in speakers
        ]
    }

    manager.set_stage(
        VOICE_SELECTION_REVIEW_STAGE,
        status=REVIEW_STATUS_APPROVED,
        payload=approved_payload,
    )
    return {"stage": VOICE_SELECTION_REVIEW_STAGE, "payload": approved_payload}


def get_speaker_audio_segments(
    *,
    project_dir: Path,
    speaker_id: str,
) -> dict[str, object]:
    """List audio segments for a speaker with metadata."""
    import re

    if not re.match(r"^speaker_[a-z0-9_]+$", speaker_id):
        raise ValueError(f"无效的 speaker_id 格式: {speaker_id}")

    transcript_path = project_dir / "transcript" / "transcript.json"
    if not transcript_path.exists():
        raise ValueError("项目中找不到转录文件。")

    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    lines = transcript_data if isinstance(transcript_data, list) else transcript_data.get("lines", [])

    segments = []
    total_duration = 0.0
    for line in lines:
        if not isinstance(line, dict):
            continue
        if str(line.get("speaker_id", "")).strip() != speaker_id:
            continue
        start_ms = int(line.get("start_ms", 0))
        end_ms = int(line.get("end_ms", 0))
        dur_s = round((end_ms - start_ms) / 1000, 1)
        total_duration += dur_s
        seg_id = line.get("index", len(segments) + 1)
        segments.append({
            "segment_id": seg_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_s": dur_s,
            "source_text": str(line.get("source_text", ""))[:200],
            "audio_url": f"/job-api/jobs/_/speaker-audio/{speaker_id}/{seg_id}.wav",
        })

    # Sort by duration descending
    segments.sort(key=lambda s: float(s["duration_s"]), reverse=True)

    return {
        "speaker_id": speaker_id,
        "segments": segments,
        "total_duration_s": round(total_duration, 1),
    }


def extract_speaker_audio_segment(
    *,
    project_dir: Path,
    speaker_id: str,
    segment_id: int,
) -> bytes:
    """Extract a single speaker audio segment as WAV bytes via ffmpeg."""
    import re

    if not re.match(r"^speaker_[a-z0-9_]+$", speaker_id):
        raise ValueError(f"无效的 speaker_id 格式: {speaker_id}")
    if segment_id < 1:
        raise ValueError(f"segment_id 必须为正整数")

    # Check cache first
    cache_dir = project_dir / "speaker_audio" / speaker_id
    cache_path = cache_dir / f"segment_{segment_id}.wav"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_bytes()

    # Find the segment timestamps from transcript
    transcript_path = project_dir / "transcript" / "transcript.json"
    if not transcript_path.exists():
        raise ValueError("项目中找不到转录文件。")

    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    lines = transcript_data if isinstance(transcript_data, list) else transcript_data.get("lines", [])

    target_line = None
    for line in lines:
        if not isinstance(line, dict):
            continue
        if (str(line.get("speaker_id", "")).strip() == speaker_id
                and line.get("index") == segment_id):
            target_line = line
            break

    if target_line is None:
        raise ValueError(f"找不到 {speaker_id} 的第 {segment_id} 段音频")

    source_audio = _find_source_audio(project_dir)
    if source_audio is None:
        raise ValueError("项目中找不到源音频文件。")

    start_s = int(target_line["start_ms"]) / 1000.0
    end_s = int(target_line["end_ms"]) / 1000.0

    # Verify output path is within project dir (path traversal prevention)
    cache_dir.mkdir(parents=True, exist_ok=True)
    resolved_cache = cache_path.resolve()
    resolved_project = project_dir.resolve()
    if not str(resolved_cache).startswith(str(resolved_project)):
        raise ValueError("路径验证失败")

    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(source_audio),
            "-ss", str(start_s),
            "-to", str(end_s),
            "-acodec", "pcm_s16le",
            "-ar", "24000",
            "-ac", "1",
            "-f", "wav",
            str(cache_path),
        ],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 切片失败: {result.stderr.decode('utf-8', errors='replace')[:500]}")

    return cache_path.read_bytes()


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

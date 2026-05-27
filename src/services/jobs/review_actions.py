"""Phase 2: Narrow review action helpers for Job API.

These functions implement the write-side review logic (approve, split, preview)
using the job's verified project_dir as authority. They do NOT live inside JobService
to keep that class focused on job lifecycle.
"""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import json
import subprocess
import tempfile
from pathlib import Path

from services.jobs.models import JOB_STATUS_WAITING_FOR_REVIEW
from services.review_state import (
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_PENDING,
    TRANSLATION_CONFIG_REVIEW_STAGE,
    TRANSLATION_REVIEW_STAGE,
    VOICE_SELECTION_REVIEW_STAGE,
    ReviewStateManager,
)


DUBBING_MODE_DUB = "dub"
DUBBING_MODE_KEEP_ORIGINAL = "keep_original"
VALID_DUBBING_MODES = {DUBBING_MODE_DUB, DUBBING_MODE_KEEP_ORIGINAL}
MINIMAX_TTS_MODEL_TURBO = "speech-2.8-turbo"
MINIMAX_TTS_MODEL_HD = "speech-2.8-hd"


def _normalize_dubbing_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_DUBBING_MODES:
        return normalized
    return DUBBING_MODE_DUB


def resolve_minimax_tts_model_from_voice_selection(
    speakers: list[dict[str, object]],
) -> str | None:
    """Resolve the job-level MiniMax model implied by per-speaker UI choices."""
    any_minimax = False
    any_hd = False
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        provider = str(sp.get("tts_provider", "") or "").strip().lower()
        if provider != "minimax":
            continue
        any_minimax = True
        model_hint = str(sp.get("minimax_model", "") or "").strip().lower()
        if model_hint in {"hd", MINIMAX_TTS_MODEL_HD}:
            any_hd = True
    if any_hd:
        return MINIMAX_TTS_MODEL_HD
    if any_minimax:
        return MINIMAX_TTS_MODEL_TURBO
    return None


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
    speaker_names: dict[str, str] | None = None,
) -> dict[str, object]:
    """Approve translation review for a specific project. Returns normalized payload."""
    from services.web_ui.translation_review import (
        _apply_speaker_names_update_from_translation_review,
        _apply_segment_speakers_update_from_translation_review,
        _save_translation_review_submission,
    )

    if speaker_names and isinstance(speaker_names, dict) and len(speaker_names) > 0:
        _apply_speaker_names_update_from_translation_review(
            project_dir=project_dir,
            speaker_names_update=speaker_names,
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


_PREVIEW_SAMPLE_TEXT = "您好，很高兴能为您提供视频服务。请选择您感兴趣的音色，让我们一起开启视频翻译的奇妙之旅吧。"
_MAX_PREVIEW_CHARS = 80
_MIN_PREVIEW_CHARS = 10


def _normalize_preview_text(text: str | None) -> str:
    """Truncate probe text for preview TTS; fall back to default sample."""
    if not text or len(text.strip()) < _MIN_PREVIEW_CHARS:
        return _PREVIEW_SAMPLE_TEXT
    text = text.strip()
    if len(text) <= _MAX_PREVIEW_CHARS:
        return text
    # Try to break at natural punctuation within limit
    for sep in ("。", "，", "、", "；", ",", " "):
        pos = text.rfind(sep, 0, _MAX_PREVIEW_CHARS)
        if pos >= _MIN_PREVIEW_CHARS:
            return text[: pos + 1]
    return text[:_MAX_PREVIEW_CHARS]


def _is_volcengine_voice(voice_id: str) -> bool:
    return voice_id.startswith(("ICL_", "zh_", "en_", "saturn_"))


def preview_voice(
    *,
    voice_id: str,
    config_path: Path,
    tts_provider: str | None = None,
    sample_text: str | None = None,
) -> dict[str, object]:
    """Preview a voice by synthesizing test text.

    Supports MiniMax (clone + official), CosyVoice, and VolcEngine.
    Uses explicit *tts_provider* when given; falls back to voice_id detection.
    *sample_text* overrides the default preview text (truncated to safe length).
    """
    import base64

    if not voice_id or not voice_id.strip():
        raise ValueError("voice_id is required")

    voice_id = voice_id.strip()
    effective_text = _normalize_preview_text(sample_text) if sample_text else _PREVIEW_SAMPLE_TEXT

    # --- Route 1: VolcEngine ---
    if tts_provider == "volcengine" or (not tts_provider and _is_volcengine_voice(voice_id)):
        return _preview_volcengine_voice(voice_id, text=effective_text)

    # --- Route 2: CosyVoice ---
    if tts_provider == "cosyvoice":
        return _preview_cosyvoice_voice(voice_id, text=effective_text)
    if not tts_provider:
        from services.tts.cosyvoice_voice_catalog import is_cosyvoice_v3_flash_builtin_voice
        if is_cosyvoice_v3_flash_builtin_voice(voice_id):
            return _preview_cosyvoice_voice(voice_id, text=effective_text)

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
            sample_text=effective_text,
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


def _preview_volcengine_voice(voice_id: str, *, text: str = _PREVIEW_SAMPLE_TEXT) -> dict[str, object]:
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
            text=text,
            voice_id=voice_id,
            resource_id=resource_id,
        )
        if wav_bytes and len(wav_bytes) > 100:
            audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
            return {"audio_base64": audio_b64, "format": "wav", "expired": False, "error": None}
        return {"audio_base64": "", "format": "wav", "expired": False, "error": "生成的音频为空"}
    except Exception as exc:
        return {"audio_base64": "", "format": "wav", "expired": False, "error": f"试听失败: {str(exc)[:200]}"}


def _preview_cosyvoice_voice(voice_id: str, *, text: str = _PREVIEW_SAMPLE_TEXT) -> dict[str, object]:
    """Preview a CosyVoice voice via DashScope TTS synthesis."""
    import base64

    try:
        from services.tts.cosyvoice_provider import synthesize as cosy_synthesize

        wav_bytes = cosy_synthesize(text=text, voice=voice_id)
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


def _validate_voice_provider_compat(
    voice_id: str,
    tts_provider: str,
    *,
    requires_worker: bool = False,
) -> None:
    """Reject obvious voice_id / tts_provider mismatches at approval time.

    CosyVoice and VolcEngine have recognisable voice_id patterns.
    MiniMax accepts any ID (cloned voices, official catalog with mixed
    formats like ``Wise_Woman``, ``moss_audio_xxx``, ``Chinese (Mandarin)_xxx``).

    Phase 4.2 E.1 PR #15 Codex P1 二轮 fix (2026-05-27): CosyVoice
    **user clone** voice_ids are UUID-shaped (e.g.
    ``voice-cosy-v3-flash-<uuid>``) and DO NOT match the builtin catalog.
    Strict ``is_cosyvoice_v3_flash_builtin_voice`` would reject them and
    block the E.1 file-upload clone flow at approval time.

    The ``requires_worker`` parameter carries the gateway-side
    enrichment signal (Phase 4.1 E ``enrich_speakers_with_worker_routing``)
    — when True, the speaker has been validated against ``user_voices``
    via DB lookup + ownership check on the gateway side, and the
    voice_id is a known clone row. We accept it as-is; the builtin
    pattern check is skipped because it would always fail for clones.

    Security note: ``requires_worker`` is set by gateway intercept
    BEFORE this function runs (see ``gateway/job_intercept.py::
    _approve_voice_selection_with_quality_sync`` →
    ``enrich_speakers_with_worker_routing``). The pipeline subprocess
    cannot fabricate this flag — gateway strips any client-supplied
    ``requires_worker`` before re-injecting from the DB lookup
    (``new_sp.pop("requires_worker", None)`` in job_intercept).
    """
    if tts_provider == "cosyvoice":
        if requires_worker:
            # User clone voice (gateway-side routed via DB ownership
            # check). Skip the builtin pattern check — it's by design
            # NOT a builtin voice_id.
            return
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
    speakers: list[dict[str, object]],
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
        #
        # Phase 4.2 E.1 PR #15 P1 fix: pass `requires_worker` so CosyVoice
        # user clone voice_ids (gateway-routed via user_voices DB lookup +
        # ownership check) skip the builtin pattern check. Gateway strips
        # any client-supplied `requires_worker` before injecting from the
        # DB lookup, so this flag is trustworthy here.
        if sp_prov and vid:
            requires_worker = bool(sp.get("requires_worker") or False)
            _validate_voice_provider_compat(
                vid, sp_prov, requires_worker=requires_worker
            )

    # Check no speaker is still cloning
    review_state_path = Path(project_dir) / "review_state.json"
    manager = ReviewStateManager(review_state_path)
    stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    existing_payload: dict[str, object] = {}
    if stage:
        existing_payload = stage.get("payload") or {}
        for sp_info in (existing_payload.get("speakers") or []):
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

    # Merge user's choices INTO the original payload instead of replacing it.
    # The original payload carries per-speaker recommendation context that the
    # frontend uses to surface "smart recommendation" lists in the dropdown
    # (top 1 + top backups by combined_rerank score, Task 2).  Replacing the
    # entire payload would erase that context once the user re-opens the page.
    existing_speakers_by_id: dict[str, dict] = {}
    for sp_info in (existing_payload.get("speakers") or []):
        sid = str(sp_info.get("speaker_id", "")).strip()
        if sid:
            existing_speakers_by_id[sid] = dict(sp_info)

    merged_speakers: list[dict[str, object]] = []
    for sp in speakers:
        sid = str(sp["speaker_id"]).strip()
        base = dict(existing_speakers_by_id.get(sid, {}))
        base["speaker_id"] = sid
        base["voice_id"] = sp["voice_id"]
        base["voice_source"] = sp.get("voice_source", "catalog")
        base["tts_provider"] = sp.get("tts_provider", "")
        if str(base["tts_provider"] or "").strip().lower() == "minimax":
            minimax_model = str(sp.get("minimax_model", "") or "").strip().lower()
            base["minimax_model"] = "hd" if minimax_model in {"hd", MINIMAX_TTS_MODEL_HD} else "turbo"
        else:
            base.pop("minimax_model", None)
        # Phase 4.2 E.1 PR #15 P1 fix: propagate CosyVoice worker routing
        # from the incoming (gateway-enriched) sp into the approved
        # payload. If user switches between voices in successive approves,
        # the freshly-enriched flags reflect the NEW voice_id, not stale
        # routing from a previous payload. When the voice is NOT a clone,
        # clear any stale flags so they don't leak across voice changes.
        if sp.get("requires_worker"):
            base["requires_worker"] = bool(sp["requires_worker"])
            target_model = sp.get("worker_target_model")
            if isinstance(target_model, str) and target_model.strip():
                base["worker_target_model"] = target_model.strip()
            else:
                base.pop("worker_target_model", None)
        else:
            base.pop("requires_worker", None)
            base.pop("worker_target_model", None)
        # Drop any in-progress clone marker now that the user has approved.
        base.pop("cloning", None)
        merged_speakers.append(base)

    approved_payload: dict[str, object] = dict(existing_payload)
    approved_payload["speakers"] = merged_speakers

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
            "dubbing_mode": _normalize_dubbing_mode(line.get("dubbing_mode")),
            "audio_url": f"/job-api/jobs/_/speaker-audio/{speaker_id}/{seg_id}.wav",
        })

    # The audit UI needs chronological order. The clone modal can still apply
    # its own duration-based auto-selection on the client.
    segments.sort(key=lambda s: int(s["start_ms"]))

    return {
        "speaker_id": speaker_id,
        "segments": segments,
        "total_duration_s": round(total_duration, 1),
    }


def set_speaker_audio_dubbing_mode(
    *,
    project_dir: Path,
    segment_id: int,
    speaker_id: str,
    dubbing_mode: str,
    audit_emitter: object | None = None,
    audit_context: object | None = None,
) -> dict[str, object]:
    """Persist per-transcript-line dubbing intent from voice selection.

    Optional ``audit_emitter`` / ``audit_context`` route a
    voice_selection_dubbing_mode_changed event through the JobService
    user-edit audit chokepoint. Plan 2026-05-04 §7.2.
    """
    import re

    if segment_id < 1:
        raise ValueError("segment_id 必须为正整数")
    if not re.match(r"^speaker_[a-z0-9_]+$", speaker_id):
        raise ValueError(f"无效的 speaker_id: {speaker_id}")
    raw_mode = str(dubbing_mode or "").strip().lower()
    if raw_mode not in VALID_DUBBING_MODES:
        raise ValueError(f"无效的 dubbing_mode: {dubbing_mode}")
    normalized_mode = raw_mode

    transcript_path = project_dir / "transcript" / "transcript.json"
    if not transcript_path.exists():
        raise ValueError("项目中找不到转录文件。")

    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    is_list_payload = isinstance(transcript_data, list)
    lines = transcript_data if is_list_payload else transcript_data.get("lines", [])
    if not isinstance(lines, list):
        raise ValueError("转录文件格式不正确。")

    target_line: dict[str, object] | None = None
    target_index: int | None = None
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        if int(line.get("index", 0) or 0) != segment_id:
            continue
        current_speaker_id = str(line.get("speaker_id", "")).strip()
        if current_speaker_id != speaker_id:
            raise ValueError(
                f"片段 {segment_id} 当前属于 {current_speaker_id or 'unknown'}，不是 {speaker_id}"
            )
        target_line = line
        target_index = index
        break

    if target_line is None or target_index is None:
        raise ValueError(f"找不到 {speaker_id} 的第 {segment_id} 段音频")

    previous_mode = _normalize_dubbing_mode(target_line.get("dubbing_mode"))
    changed = previous_mode != normalized_mode
    updated_at = datetime.now(timezone.utc).isoformat()
    if changed:
        updated_line = dict(target_line)
        updated_line["dubbing_mode"] = normalized_mode
        updated_line["dubbing_mode_updated_at"] = updated_at
        lines[target_index] = updated_line
        _atomic_write_json(transcript_path, transcript_data if is_list_payload else {**transcript_data, "lines": lines})
        snapshot_update_count = _sync_dubbing_mode_to_segment_snapshots(
            project_dir=project_dir,
            segment_id=segment_id,
            dubbing_mode=normalized_mode,
        )

        review_state_path = Path(project_dir) / "review_state.json"
        manager = ReviewStateManager(review_state_path)
        stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
        if stage:
            payload = dict(stage.get("payload") or {})
            history = payload.get("dubbing_mode_history")
            if not isinstance(history, list):
                history = []
            history.append({
                "segment_id": segment_id,
                "speaker_id": speaker_id,
                "previous_mode": previous_mode,
                "dubbing_mode": normalized_mode,
                "updated_at": updated_at,
            })
            payload["dubbing_mode_history"] = history[-200:]
            manager.set_stage(
                VOICE_SELECTION_REVIEW_STAGE,
                status=str(stage.get("status") or REVIEW_STATUS_PENDING),
                payload=payload,
                activate=True,
            )

    if changed and audit_emitter is not None and audit_context is not None:
        try:
            from services.jobs.user_edit_audit import (
                build_voice_selection_dubbing_mode_changed_event,
            )
            duration_ms = None
            try:
                start_ms = int(target_line.get("start_ms") or 0)
                end_ms = int(target_line.get("end_ms") or 0)
                if end_ms > start_ms:
                    duration_ms = end_ms - start_ms
            except (TypeError, ValueError):
                duration_ms = None
            audit_emitter(
                build_voice_selection_dubbing_mode_changed_event(
                    audit_context,
                    segment_id=segment_id,
                    speaker_id=speaker_id,
                    before_mode=previous_mode,
                    after_mode=normalized_mode,
                    duration_ms=duration_ms,
                )
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "segment_id": segment_id,
        "speaker_id": speaker_id,
        "dubbing_mode": normalized_mode,
        "previous_mode": previous_mode,
        "changed": changed,
        "segment_snapshot_update_count": snapshot_update_count if changed else 0,
    }


def reassign_speaker_audio_segment(
    *,
    project_dir: Path,
    segment_id: int,
    from_speaker_id: str,
    to_speaker_id: str,
    audit_emitter: object | None = None,
    audit_context: object | None = None,
) -> dict[str, object]:
    """Persist a voice-selection-stage speaker correction for one transcript line.

    Optional ``audit_emitter`` is a callable ``(event_dict) -> None`` that
    forwards the audit event through ``JobService._emit_user_edit_event``;
    callers in the Job API layer build it from the live JobRecord. Plan
    2026-05-04 §12 P0.
    """
    import re

    _speaker_id_re = re.compile(r"^speaker_[a-z0-9_]+$")
    if segment_id < 1:
        raise ValueError("segment_id 必须为正整数")
    if not _speaker_id_re.match(from_speaker_id):
        raise ValueError(f"无效的 from_speaker_id: {from_speaker_id}")
    if not _speaker_id_re.match(to_speaker_id):
        raise ValueError(f"无效的 to_speaker_id: {to_speaker_id}")
    if from_speaker_id == to_speaker_id:
        return {
            "segment_id": segment_id,
            "from_speaker_id": from_speaker_id,
            "to_speaker_id": to_speaker_id,
            "changed": False,
        }

    transcript_path = project_dir / "transcript" / "transcript.json"
    if not transcript_path.exists():
        raise ValueError("项目中找不到转录文件。")

    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    is_list_payload = isinstance(transcript_data, list)
    lines = transcript_data if is_list_payload else transcript_data.get("lines", [])
    if not isinstance(lines, list):
        raise ValueError("转录文件格式不正确。")

    if not any(
        isinstance(line, dict)
        and str(line.get("speaker_id", "")).strip() == to_speaker_id
        for line in lines
    ):
        raise ValueError(f"目标说话人不存在: {to_speaker_id}")

    target_line: dict[str, object] | None = None
    target_index: int | None = None
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        if int(line.get("index", 0) or 0) != segment_id:
            continue
        current_speaker_id = str(line.get("speaker_id", "")).strip()
        if current_speaker_id != from_speaker_id:
            raise ValueError(
                f"片段 {segment_id} 当前属于 {current_speaker_id or 'unknown'}，不是 {from_speaker_id}"
            )
        target_line = line
        target_index = index
        break

    if target_line is None or target_index is None:
        raise ValueError(f"找不到 {from_speaker_id} 的第 {segment_id} 段音频")

    target_label = _resolve_speaker_label(lines, to_speaker_id)
    updated_line = dict(target_line)
    updated_line["speaker_id"] = to_speaker_id
    updated_line["speaker_label"] = target_label
    updated_line["speaker_reassigned_from"] = from_speaker_id
    updated_line["speaker_reassigned_at"] = datetime.now(timezone.utc).isoformat()
    lines[target_index] = updated_line

    _atomic_write_json(transcript_path, transcript_data if is_list_payload else {**transcript_data, "lines": lines})

    # Keep the active review payload in sync for refreshes and audit history.
    review_state_path = Path(project_dir) / "review_state.json"
    manager = ReviewStateManager(review_state_path)
    stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    if stage:
        payload = dict(stage.get("payload") or {})
        payload["speakers"] = _recount_voice_selection_speakers(
            payload.get("speakers"),
            lines,
        )
        history = payload.get("speaker_reassignment_history")
        if not isinstance(history, list):
            history = []
        history.append({
            "segment_id": segment_id,
            "from_speaker_id": from_speaker_id,
            "to_speaker_id": to_speaker_id,
            "updated_at": updated_line["speaker_reassigned_at"],
        })
        payload["speaker_reassignment_history"] = history[-200:]
        manager.set_stage(
            VOICE_SELECTION_REVIEW_STAGE,
            status=str(stage.get("status") or REVIEW_STATUS_PENDING),
            payload=payload,
            activate=True,
        )

    # Audit hook (plan 2026-05-04 §7.2): voice_selection_speaker_reassigned.
    # Best-effort — wrapping happens in the emitter callback supplied by the
    # Job API layer (which routes through JobService._emit_user_edit_event).
    if audit_emitter is not None and audit_context is not None:
        try:
            from services.jobs.user_edit_audit import (
                build_voice_selection_speaker_reassigned_event,
            )
            duration_ms = None
            try:
                start_ms = int(target_line.get("start_ms") or 0)
                end_ms = int(target_line.get("end_ms") or 0)
                if end_ms > start_ms:
                    duration_ms = end_ms - start_ms
            except (TypeError, ValueError):
                duration_ms = None
            audit_emitter(
                build_voice_selection_speaker_reassigned_event(
                    audit_context,
                    segment_id=segment_id,
                    from_speaker_id=from_speaker_id,
                    to_speaker_id=to_speaker_id,
                    duration_ms=duration_ms,
                    is_short_segment=(duration_ms is not None and duration_ms < 2000),
                )
            )
        except Exception:  # noqa: BLE001
            # Audit must never break the user-facing reassign flow.
            pass

    return {
        "segment_id": segment_id,
        "from_speaker_id": from_speaker_id,
        "to_speaker_id": to_speaker_id,
        "changed": True,
        "from_summary": _speaker_summary(lines, from_speaker_id),
        "to_summary": _speaker_summary(lines, to_speaker_id),
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

    # Check cache only after transcript ownership is verified. Speaker
    # assignments can be edited during voice selection, so stale speaker cache
    # paths must not bypass the current transcript.
    cache_dir = project_dir / "speaker_audio" / speaker_id
    cache_path = cache_dir / f"segment_{segment_id}.wav"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_bytes()

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


def _atomic_write_json(path: Path, payload: object) -> None:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.stem}_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            json.dump(payload, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            temp_path = Path(temp_file.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _sync_dubbing_mode_to_segment_snapshots(
    *,
    project_dir: Path,
    segment_id: int,
    dubbing_mode: str,
) -> int:
    """Keep already-written segment snapshots aligned with transcript intent."""
    updated_count = 0
    for snapshot_path in (
        project_dir / "translation" / "segments.json",
        project_dir / "editor" / "segments.json",
    ):
        if not snapshot_path.exists():
            continue
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            records = payload.get("segments", [])
        elif isinstance(payload, list):
            records = payload
        else:
            continue
        if not isinstance(records, list):
            continue

        changed = False
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                record_segment_id = int(str(record.get("segment_id", "")).strip())
            except ValueError:
                continue
            if record_segment_id != segment_id:
                continue
            if _normalize_dubbing_mode(record.get("dubbing_mode")) != dubbing_mode:
                record["dubbing_mode"] = dubbing_mode
                changed = True
        if changed:
            _atomic_write_json(snapshot_path, payload)
            updated_count += 1
    return updated_count


def _resolve_speaker_label(lines: list[object], speaker_id: str) -> str:
    for line in lines:
        if not isinstance(line, dict):
            continue
        if str(line.get("speaker_id", "")).strip() != speaker_id:
            continue
        label = str(line.get("speaker_label") or "").strip()
        if label:
            return label
    suffix = speaker_id.replace("speaker_", "", 1)
    if len(suffix) == 1 and suffix.isalpha():
        return suffix.upper()
    return speaker_id


def _speaker_summary(lines: list[object], speaker_id: str) -> dict[str, object]:
    count = 0
    total_duration_s = 0.0
    for line in lines:
        if not isinstance(line, dict):
            continue
        if str(line.get("speaker_id", "")).strip() != speaker_id:
            continue
        count += 1
        start_ms = int(line.get("start_ms", 0) or 0)
        end_ms = int(line.get("end_ms", 0) or 0)
        total_duration_s += max(0, end_ms - start_ms) / 1000.0
    return {
        "speaker_id": speaker_id,
        "segment_count": count,
        "total_duration_s": round(total_duration_s, 1),
    }


def _recount_voice_selection_speakers(
    speakers_payload: object,
    lines: list[object],
) -> list[dict[str, object]]:
    if not isinstance(speakers_payload, list):
        speakers_payload = []

    speaker_ids: list[str] = []
    for speaker in speakers_payload:
        if isinstance(speaker, dict):
            sid = str(speaker.get("speaker_id", "")).strip()
            if sid and sid not in speaker_ids:
                speaker_ids.append(sid)
    for line in lines:
        if isinstance(line, dict):
            sid = str(line.get("speaker_id", "")).strip()
            if sid and sid not in speaker_ids:
                speaker_ids.append(sid)

    summary_by_id = {sid: _speaker_summary(lines, sid) for sid in speaker_ids}
    existing_by_id = {
        str(speaker.get("speaker_id", "")).strip(): dict(speaker)
        for speaker in speakers_payload
        if isinstance(speaker, dict)
    }
    result: list[dict[str, object]] = []
    for sid in speaker_ids:
        speaker = dict(existing_by_id.get(sid, {"speaker_id": sid}))
        summary = summary_by_id[sid]
        speaker["segment_count"] = summary["segment_count"]
        speaker["total_duration_s"] = summary["total_duration_s"]
        result.append(speaker)
    return result

from __future__ import annotations

import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from core.exceptions import StateError
from services import config_loader
from services.review_state import (
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_SKIPPED,
    SPEAKER_REVIEW_STAGE,
    TRANSLATION_CONFIG_REVIEW_STAGE,
    TRANSLATION_REVIEW_STAGE,
    VOICE_REVIEW_STAGE,
    ReviewStateManager,
)
from services.voice_registry import VoiceRegistry, VoiceResolver

from .config_helpers import (
    _find_translation_model_label,
    _normalize_optional_text,
    save_web_ui_settings,
)
from .output_entries import _resolve_review_state_path
from .project_resolver import (
    _resolve_allowed_project_file_download_path,
    _resolve_authoritative_review_project_dir,
    _resolve_project_dir_by_job_id,
    _resolve_public_result_download_path,
)
from .snapshot import build_web_ui_snapshot
from .speaker_review import _save_speaker_review_submission
from .translation_review import (
    _apply_segment_speakers_update_from_translation_review,
    _save_translation_review_submission,
    _split_segment,
)
from .voice_library import _find_builtin_voice_option, _resolve_voice_registry_path


def _build_web_ui_handler() -> type[BaseHTTPRequestHandler]:
    class WebUIHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            if parsed_path.path == "/":
                self._write_json(HTTPStatus.OK, {"status": "ok", "message": "Legacy HTML UI removed. Use the Next.js frontend."})
                return
            if parsed_path.path == "/api/state":
                snapshot = build_web_ui_snapshot(manager=self.server.job_manager)  # type: ignore[attr-defined]
                self._write_json(HTTPStatus.OK, snapshot)
                return
            if parsed_path.path == "/api/result-download":
                query = parse_qs(parsed_path.query)
                requested_project_dir = str((query.get("project_dir") or [""])[0]).strip()
                requested_job_id = str((query.get("job_id") or [""])[0]).strip()
                requested_key = str((query.get("key") or [""])[0]).strip()
                if not requested_key:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "key query parameter is required"},
                    )
                    return
                manager = self.server.job_manager  # type: ignore[attr-defined]
                project_root = manager.project_root.resolve(strict=False)
                resolved_project_dir_text = requested_project_dir or _resolve_project_dir_by_job_id(
                    manager=manager,
                    job_id=requested_job_id,
                )
                if not resolved_project_dir_text:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "project_dir or job_id query parameter is required",
                        },
                    )
                    return
                try:
                    download_path = _resolve_public_result_download_path(
                        project_root=project_root,
                        project_dir=Path(resolved_project_dir_text).expanduser().resolve(strict=False),
                        download_key=requested_key,
                    )
                except ValueError as exc:
                    self._write_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                    return
                if download_path is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "Requested download was not found."},
                    )
                    return
                content_type = mimetypes.guess_type(str(download_path))[0] or "application/octet-stream"
                self._write_binary(
                    HTTPStatus.OK,
                    download_path.read_bytes(),
                    content_type=content_type,
                    download_name=download_path.name,
                )
                return
            if parsed_path.path == "/api/project-file":
                query = parse_qs(parsed_path.query)
                requested_path = str((query.get("path") or [""])[0]).strip()
                if not requested_path:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "path query parameter is required"})
                    return
                try:
                    candidate_path = _resolve_allowed_project_file_download_path(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_path=requested_path,
                    )
                except FileNotFoundError:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "Requested file was not found."})
                    return
                except ValueError as exc:
                    self._write_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                    return
                content_type = mimetypes.guess_type(str(candidate_path))[0] or "application/octet-stream"
                self._write_binary(HTTPStatus.OK, candidate_path.read_bytes(), content_type=content_type)
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed_path = urlparse(self.path)
                if parsed_path.path == "/api/upload-video":
                    self._handle_video_upload()
                    return
                if parsed_path.path == "/api/run":
                    payload = self._read_json()
                    snapshot = self.server.job_manager.start_job(  # type: ignore[attr-defined]
                        youtube_url=str(payload.get("youtube_url") or ""),
                        speakers=str(payload.get("speakers") or "auto"),
                        voice_a=_normalize_optional_text(payload.get("voice_a")),
                        voice_b=_normalize_optional_text(payload.get("voice_b")),
                        translation_model_alias=str(payload.get("translation_model_alias") or ""),
                        transcription_method=_normalize_optional_text(payload.get("transcription_method")),
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/stop":
                    snapshot = self.server.job_manager.stop_job()  # type: ignore[attr-defined]
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/voice-library/set-default":
                    payload = self._read_json()
                    speaker_id = str(payload.get("speaker_id") or "").strip()
                    voice_id = str(payload.get("voice_id") or "").strip()
                    if not speaker_id or not voice_id:
                        raise ValueError("speaker_id \u548c voice_id \u4e0d\u80fd\u4e3a\u7a7a\u3002")
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    VoiceRegistry(str(registry_path)).set_default_voice(speaker_id, voice_id)
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/voice-library/register-manual":
                    payload = self._read_json()
                    speaker_id = str(payload.get("speaker_id") or "").strip()
                    voice_id = str(payload.get("voice_id") or "").strip()
                    speaker_name = _normalize_optional_text(payload.get("speaker_name")) or speaker_id
                    if not speaker_id or not voice_id:
                        raise ValueError("speaker_id \u548c voice_id \u4e0d\u80fd\u4e3a\u7a7a\u3002")
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    VoiceRegistry(str(registry_path)).register_voice(
                        speaker_id=speaker_id,
                        speaker_name=speaker_name,
                        voice_id=voice_id,
                        voice_type="cloned",
                        provider="minimax",
                        tts_provider="minimax_tts",
                        platform="minimax_domestic",
                        label=f"{speaker_name} Manual Voice ID",
                        source_audio_path=_normalize_optional_text(payload.get("sample_path")),
                        notes="Registered from Web UI manual voice review.",
                        set_default=True,
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/voice-library/set-project-default-builtin":
                    payload = self._read_json()
                    voice_id = str(payload.get("voice_id") or "").strip()
                    if not voice_id:
                        raise ValueError("voice_id \u4e0d\u80fd\u4e3a\u7a7a\u3002")
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    registry = VoiceRegistry(str(registry_path))
                    builtin_voice = _find_builtin_voice_option(registry=registry, voice_id=voice_id)
                    if builtin_voice is None:
                        raise ValueError(f"\u672a\u627e\u5230 builtin voice_id={voice_id}")
                    registry.set_project_default_builtin_voice(
                        voice_id=str(builtin_voice["voice_id"]),
                        provider=str(builtin_voice["provider"]),
                        tts_provider=_normalize_optional_text(builtin_voice.get("tts_provider")),
                        platform=_normalize_optional_text(builtin_voice.get("platform")),
                        label=str(builtin_voice["label"]),
                        created_at=_normalize_optional_text(builtin_voice.get("created_at")),
                        notes=_normalize_optional_text(builtin_voice.get("notes")),
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/review/speaker/save":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                    )
                    _save_speaker_review_submission(
                        project_dir=project_dir,
                        speaker_names_payload=payload.get("speaker_names"),
                        segment_speakers_payload=payload.get("segment_speakers"),
                        review_confirmations_payload=payload.get("confirmations"),
                        status=REVIEW_STATUS_PENDING,
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/review/speaker/approve":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=SPEAKER_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    _save_speaker_review_submission(
                        project_dir=project_dir,
                        speaker_names_payload=payload.get("speaker_names"),
                        segment_speakers_payload=payload.get("segment_speakers"),
                        review_confirmations_payload=payload.get("confirmations"),
                        status=REVIEW_STATUS_APPROVED,
                    )
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=SPEAKER_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/voice/approve":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=VOICE_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
                    voice_stage = review_state_manager.get_stage(VOICE_REVIEW_STAGE)
                    if not voice_stage or voice_stage.get("status") != REVIEW_STATUS_PENDING:
                        raise ValueError("\u5f53\u524d\u6ca1\u6709\u5f85\u786e\u8ba4\u7684\u97f3\u8272\u9636\u6bb5\u3002")
                    review_payload = voice_stage.get("payload")
                    if not isinstance(review_payload, dict):
                        review_payload = {}

                    # Accept user-confirmed voice IDs from the new voice review flow
                    user_voice_id_a = _normalize_optional_text(payload.get("voice_id_a"))
                    user_voice_id_b = _normalize_optional_text(payload.get("voice_id_b"))

                    # If user provided voice IDs directly, use those
                    if user_voice_id_a or user_voice_id_b:
                        resolved_speakers = []
                        if user_voice_id_a:
                            resolved_speakers.append({
                                "speaker_id": "speaker_a",
                                "voice_id": user_voice_id_a,
                                "voice_type": "cloned",
                                "label": "User confirmed",
                                "source": "voice_review",
                            })
                        if user_voice_id_b:
                            resolved_speakers.append({
                                "speaker_id": "speaker_b",
                                "voice_id": user_voice_id_b,
                                "voice_type": "cloned",
                                "label": "User confirmed",
                                "source": "voice_review",
                            })
                        review_state_manager.set_stage(
                            VOICE_REVIEW_STAGE,
                            status=REVIEW_STATUS_APPROVED,
                            payload={
                                **review_payload,
                                "voice_id_a": user_voice_id_a,
                                "voice_id_b": user_voice_id_b,
                                "resolved_speakers": resolved_speakers,
                            },
                        )
                    else:
                        # Legacy flow: resolve from registry
                        registry_path = _resolve_voice_registry_path(
                            project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                            config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                        )
                        registry = VoiceRegistry(str(registry_path))
                        resolver = VoiceResolver(registry)
                        unresolved: list[str] = []
                        resolved_speakers_legacy: list[dict[str, object]] = []
                        raw_speakers = review_payload.get("speakers", [])
                        if isinstance(raw_speakers, list):
                            for raw_speaker in raw_speakers:
                                if not isinstance(raw_speaker, dict):
                                    continue
                                speaker_id = str(raw_speaker.get("speaker_id") or "").strip()
                                if not speaker_id:
                                    continue
                                resolution = resolver.resolve(speaker_id)
                                if not resolution.resolved or not resolution.voice_id:
                                    unresolved.append(
                                        str(raw_speaker.get("speaker_name") or raw_speaker.get("speaker_label") or speaker_id)
                                    )
                                    continue
                                resolved_speakers_legacy.append(
                                    {
                                        "speaker_id": speaker_id,
                                        "voice_id": resolution.voice_id,
                                        "voice_type": resolution.voice_type,
                                        "label": resolution.label,
                                        "source": resolution.source,
                                    }
                                )
                        if unresolved:
                            raise ValueError(
                                f"\u4ecd\u6709 speaker \u672a\u7ed1\u5b9a\u53ef\u7528\u97f3\u8272\uff1a{', '.join(unresolved)}"
                            )
                        review_state_manager.set_stage(
                            VOICE_REVIEW_STAGE,
                            status=REVIEW_STATUS_APPROVED,
                            payload={
                                **review_payload,
                                "resolved_speakers": resolved_speakers_legacy,
                            },
                        )
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=VOICE_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/voice/preview":
                    payload = self._read_json()
                    voice_id = _normalize_optional_text(payload.get("voice_id"))
                    speaker_id = _normalize_optional_text(payload.get("speaker_id")) or "preview"
                    sample_text = _normalize_optional_text(payload.get("sample_text"))
                    if not voice_id:
                        raise ValueError("voice_id is required for preview")
                    from services.voice_asset import VoiceAssetVerifier, DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT
                    preview_text = sample_text or DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT
                    config_path = self.server.job_manager.config_path  # type: ignore[attr-defined]
                    verifier = VoiceAssetVerifier.from_env(config_path=config_path)
                    result = verifier.verify_voice(
                        speaker_id=speaker_id,
                        voice_id=voice_id,
                        sample_text=preview_text,
                    )
                    # Return base64-encoded audio for frontend to play directly
                    import base64
                    audio_b64 = ""
                    if result.output_path and Path(result.output_path).exists():
                        audio_b64 = base64.b64encode(
                            Path(result.output_path).read_bytes()
                        ).decode("ascii")
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            "success": True,
                            "audio_base64": audio_b64,
                            "audio_format": "wav",
                            "sample_text": result.sample_text,
                            "voice_id": voice_id,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/voice/clone":
                    payload = self._read_json()
                    speaker_id = _normalize_optional_text(payload.get("speaker_id"))
                    speaker_name = _normalize_optional_text(payload.get("speaker_name"))
                    sample_path = _normalize_optional_text(payload.get("sample_path"))
                    if not speaker_id:
                        raise ValueError("speaker_id is required for cloning")

                    # If no sample_path provided, auto-extract from transcript
                    if not sample_path:
                        project_dir = _resolve_authoritative_review_project_dir(
                            manager=self.server.job_manager,  # type: ignore[attr-defined]
                            requested_project_dir=payload.get("project_dir"),
                            expected_stage=VOICE_REVIEW_STAGE,
                            require_waiting_review=True,
                        )
                        # Find audio and transcript for sample extraction
                        audio_path = project_dir / "audio" / "speech_for_asr.wav"
                        if not audio_path.exists():
                            audio_path = project_dir / "audio" / "original.wav"
                        transcript_path = project_dir / "transcript" / "transcript.json"
                        if not audio_path.exists():
                            raise ValueError("\u627e\u4e0d\u5230\u539f\u59cb\u97f3\u9891\u6587\u4ef6\uff0c\u65e0\u6cd5\u63d0\u53d6\u6837\u672c\u3002")
                        if not transcript_path.exists():
                            raise ValueError("\u627e\u4e0d\u5230\u8f6c\u5f55\u6587\u4ef6\uff0c\u65e0\u6cd5\u8bc6\u522b\u53d1\u8a00\u4eba\u3002")

                        # Load transcript and filter lines for this speaker
                        transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
                        lines = transcript_data.get("lines", [])
                        from services.assemblyai.transcriber import TranscriptLine
                        speaker_lines = []
                        for idx, line in enumerate(lines):
                            if not isinstance(line, dict):
                                continue
                            line_speaker = str(line.get("speaker_id") or "").strip()
                            if line_speaker == speaker_id:
                                speaker_lines.append(TranscriptLine(
                                    index=int(line.get("index", idx)),
                                    start_ms=int(line.get("start_ms") or 0),
                                    end_ms=int(line.get("end_ms") or 0),
                                    speaker_id=line_speaker,
                                    speaker_label=str(line.get("speaker_label") or line_speaker),
                                    source_text=str(line.get("source_text") or line.get("en_text") or ""),
                                ))

                        if not speaker_lines:
                            raise ValueError(f"\u8f6c\u5f55\u4e2d\u672a\u627e\u5230\u53d1\u8a00\u4eba {speaker_id} \u7684\u5185\u5bb9\uff0c\u65e0\u6cd5\u63d0\u53d6\u6837\u672c\u3002")

                        # Extract sample
                        from services.voice.sample_extractor import VoiceSampleExtractor
                        extractor = VoiceSampleExtractor()
                        samples_dir = project_dir / "voice_samples"
                        samples_dir.mkdir(parents=True, exist_ok=True)
                        safe_name = (speaker_name or speaker_id).lower().replace(" ", "_").replace("(", "").replace(")", "")
                        output_sample = str(samples_dir / f"{safe_name}_sample.wav")
                        extractor.extract_sample(
                            audio_path=str(audio_path),
                            speaker_lines=speaker_lines,
                            output_path=output_sample,
                        )
                        sample_path = output_sample
                        print(f"[VoiceClone] \u5df2\u63d0\u53d6 {speaker_id} \u97f3\u9891\u6837\u672c: {sample_path}")

                    config_path = self.server.job_manager.config_path  # type: ignore[attr-defined]
                    from services.voice_clone import MiniMaxVoiceCloneClient, VoiceCloneConfig
                    clone_config = VoiceCloneConfig.from_env(config_path=config_path)
                    clone_client = MiniMaxVoiceCloneClient(config=clone_config)
                    project_config = config_loader.load_project_local_config(config_path)
                    tts_config_raw = project_config.get_section("tts")
                    clone_result = clone_client.create_voice_clone(
                        speaker_id=speaker_id,
                        speaker_name=speaker_name or speaker_id,
                        source_audio_path=Path(sample_path),
                    )
                    # Register in voice registry
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=config_path,
                    )
                    registry = VoiceRegistry(str(registry_path))
                    registry.register_voice(
                        speaker_id=speaker_id,
                        speaker_name=speaker_name or speaker_id,
                        voice_id=clone_result.voice_id,
                        voice_type="cloned",
                        provider="minimax_voice_clone",
                        tts_provider=tts_config_raw.get("provider_name", "minimax_tts") if isinstance(tts_config_raw, dict) else "minimax_tts",
                        platform=tts_config_raw.get("platform", "minimax_domestic") if isinstance(tts_config_raw, dict) else "minimax_domestic",
                        label=f"{speaker_name or speaker_id} Clone",
                        source_audio_path=sample_path,
                        notes="\u4ece Voice Review \u9875\u9762\u514b\u9686",
                    )
                    registry.set_default_voice(speaker_id, clone_result.voice_id)
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            "success": True,
                            "voice_id": clone_result.voice_id,
                            "speaker_id": speaker_id,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/voice/cancel":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=VOICE_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
                    voice_stage = review_state_manager.get_stage(VOICE_REVIEW_STAGE)
                    if not voice_stage or voice_stage.get("status") != REVIEW_STATUS_PENDING:
                        raise ValueError("\u5f53\u524d\u6ca1\u6709\u5f85\u53d6\u6d88\u7684\u97f3\u8272\u786e\u8ba4\u3002")
                    review_state_manager.set_stage(
                        VOICE_REVIEW_STAGE,
                        status=REVIEW_STATUS_SKIPPED,
                        activate=False,
                    )
                    snapshot = self.server.job_manager.cancel_waiting_review(  # type: ignore[attr-defined]
                        expected_stage=VOICE_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/translation/save":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                    )
                    _save_translation_review_submission(
                        project_dir=project_dir,
                        translation_segments_payload=payload.get("segments"),
                        status=REVIEW_STATUS_PENDING,
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/review/translation-config/approve":
                    payload = self._read_json()
                    # Accept both translation_config_review and voice_review stages
                    # because pipeline may report voice_review as the current stage
                    # while actually waiting for translation_config_review
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=None,  # Skip stage check -- we verify review_state instead
                        require_waiting_review=True,
                    )
                    # Save selected model and prompt to review state
                    review_state_path = Path(project_dir) / "review_state.json"
                    review_state_manager = ReviewStateManager(review_state_path)
                    review_state_manager.set_stage(
                        TRANSLATION_CONFIG_REVIEW_STAGE,
                        status=REVIEW_STATUS_APPROVED,
                        payload={
                            "selected_model": payload.get("selected_model"),
                            "prompt_template": payload.get("prompt_template"),
                        },
                    )
                    # Optionally persist prompt to config
                    if payload.get("save_prompt"):
                        try:
                            save_web_ui_settings(
                                translation_model_alias=str(payload.get("selected_model") or ""),
                                translation_prompt_template=payload.get("prompt_template"),
                                provider_api_keys={},
                            )
                        except Exception:
                            pass  # Non-critical: prompt save failure shouldn't block flow
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=None  # Skip stage check
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/translation/approve":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=TRANSLATION_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    # If segment_speakers provided, update speaker assignments
                    segment_speakers_update = payload.get("segment_speakers")
                    if isinstance(segment_speakers_update, dict) and segment_speakers_update:
                        _apply_segment_speakers_update_from_translation_review(
                            project_dir=project_dir,
                            segment_speakers_update=segment_speakers_update,
                        )
                    _save_translation_review_submission(
                        project_dir=project_dir,
                        translation_segments_payload=payload.get("segments"),
                        status=REVIEW_STATUS_APPROVED,
                    )
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=TRANSLATION_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/split-segment":
                    payload = self._read_json()
                    stage = payload.get("stage", "translation_review")
                    expected_stage = TRANSLATION_REVIEW_STAGE if stage == "translation_review" else SPEAKER_REVIEW_STAGE
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=expected_stage,
                        require_waiting_review=True,
                    )
                    # Apply any pending speaker changes before splitting
                    pending_speakers = payload.get("pending_speaker_changes")
                    if isinstance(pending_speakers, dict) and pending_speakers:
                        _apply_segment_speakers_update_from_translation_review(
                            project_dir=project_dir,
                            segment_speakers_update=pending_speakers,
                        )
                    result = _split_segment(
                        project_dir=project_dir,
                        segment_id=payload.get("segment_id"),
                        split_source_index=payload.get("split_source_index"),
                        split_cn_index=payload.get("split_cn_index"),
                        speaker_a=payload.get("speaker_a"),
                        speaker_b=payload.get("speaker_b"),
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, {**snapshot, "split_result": result})
                    return
                if parsed_path.path == "/api/settings":
                    payload = self._read_json()
                    updated_route = save_web_ui_settings(
                        translation_model_alias=str(payload.get("translation_model_alias") or ""),
                        speaker_infer_prompt_template=_normalize_optional_text(
                            payload.get("speaker_infer_prompt_template")
                        ),
                        translation_prompt_template=_normalize_optional_text(
                            payload.get("translation_prompt_template")
                        ),
                        rewrite_prompt_template=_normalize_optional_text(
                            payload.get("rewrite_prompt_template")
                        ),
                        provider_api_keys={
                            "gemini": _normalize_optional_text(payload.get("gemini_api_key")),
                            "deepseek": _normalize_optional_text(payload.get("deepseek_api_key")),
                            "openai": _normalize_optional_text(payload.get("openai_api_key")),
                            "anthropic": _normalize_optional_text(payload.get("anthropic_api_key")),
                        },
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    snapshot["settings"]["s3_translate_route"] = [
                        {
                            "alias": alias,
                            "label": _find_translation_model_label(
                                alias,
                                config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                            ),
                        }
                        for alias in updated_route
                    ]
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except (ValueError, StateError) as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive server fallback
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _handle_video_upload(self) -> None:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "\u9700\u8981 multipart/form-data \u683c\u5f0f\u4e0a\u4f20\u3002"})
                return

            content_length = int(self.headers.get("Content-Length") or "0")
            if content_length <= 0:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "\u4e0a\u4f20\u6587\u4ef6\u4e0d\u80fd\u4e3a\u7a7a\u3002"})
                return

            # 限制最大 2GB
            max_size = 2 * 1024 * 1024 * 1024
            if content_length > max_size:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "\u6587\u4ef6\u592a\u5927\uff0c\u6700\u5927\u652f\u6301 2GB\u3002"})
                return

            import cgi
            import tempfile as _tempfile

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(content_length),
                },
            )
            file_item = form["file"] if "file" in form else None
            if file_item is None or not hasattr(file_item, "file"):
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "\u672a\u627e\u5230\u4e0a\u4f20\u6587\u4ef6\u5b57\u6bb5 'file'\u3002"})
                return

            original_filename = getattr(file_item, "filename", "uploaded_video.mp4") or "uploaded_video.mp4"
            upload_dir = Path(
                getattr(self.server.job_manager, "project_root", None) or "."  # type: ignore[attr-defined]
            ) / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)

            # 用时间戳避免文件名冲突
            import time as _time
            safe_name = re.sub(r"[^\w.\-]", "_", original_filename)
            dest_path = upload_dir / f"{int(_time.time())}_{safe_name}"

            with open(dest_path, "wb") as dest_file:
                while True:
                    chunk = file_item.file.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    dest_file.write(chunk)

            file_size_mb = dest_path.stat().st_size / (1024 * 1024)
            self._write_json(HTTPStatus.OK, {
                "file_path": str(dest_path),
                "file_name": original_filename,
                "file_size_mb": round(file_size_mb, 2),
            })

        def _read_json(self) -> dict[str, object]:
            content_length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"\u8bf7\u6c42\u4f53\u4e0d\u662f\u5408\u6cd5 JSON\uff1a{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("\u8bf7\u6c42\u4f53\u5fc5\u987b\u662f JSON \u5bf9\u8c61\u3002")
            return payload

        def _write_html(self, status: HTTPStatus, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _write_binary(
            self,
            status: HTTPStatus,
            payload: bytes,
            *,
            content_type: str,
            download_name: str | None = None,
        ) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            if download_name:
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{quote(download_name)}",
                )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return WebUIHandler

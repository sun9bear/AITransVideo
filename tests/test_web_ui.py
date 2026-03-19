from __future__ import annotations

from contextlib import contextmanager
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

import pytest

from services.web_ui import (
    JobAPIRequestError,
    JobAPIBackedJobManager,
    ProcessJobManager,
    ProcessJobSnapshot,
    _resolve_authoritative_review_project_dir,
    _save_speaker_review_submission,
    _save_translation_review_submission,
    build_provider_key_options,
    build_translation_model_options,
    build_web_ui_snapshot,
    create_web_ui_server,
    render_web_ui_html,
    save_web_ui_settings,
    set_translation_primary_model,
)


def _write_test_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "gemini": {
                    "api_key": "gemini-key",
                    "api_key_env_var": "GEMINI_API_KEY",
                    "model_name": "gemini-2.5-pro",
                    "temperature": 0.3,
                    "max_output_tokens": 8192,
                },
                "deepseek": {
                    "api_key": "deepseek-key",
                    "api_key_env_var": "DEEPSEEK_API_KEY",
                    "model_name": "deepseek-chat",
                },
                "openai": {
                    "api_key": "openai-key",
                    "api_key_env_var": "OPENAI_API_KEY",
                    "model_name": "gpt-4.1",
                },
                "anthropic": {
                    "api_key": "",
                    "api_key_env_var": "ANTHROPIC_API_KEY",
                    "model_name": "claude-sonnet-4-6",
                },
                "llm_models": {
                    "deepseek_chat": {
                        "provider": "deepseek",
                        "model_name": "deepseek-chat",
                    },
                    "gemini_3_1_flash_lite_preview": {
                        "provider": "gemini",
                        "model_name": "gemini-3.1-flash-lite-preview",
                    },
                    "gpt_41": {
                        "provider": "openai",
                        "model_name": "gpt-4.1",
                    },
                },
                "llm_fallbacks": {
                    "s2_infer": ["default_llm", "gpt_41"],
                    "s2_review": ["default_llm", "gpt_41"],
                    "s3_translate": [
                        "gemini_3_1_flash_lite_preview",
                        "default_llm",
                        "deepseek_chat",
                        "gpt_41",
                    ],
                    "s5_rewrite": [
                        "gemini_3_1_flash_lite_preview",
                        "default_llm",
                        "deepseek_chat",
                        "gpt_41",
                    ],
                },
                "prompts": {
                    "s2_infer": None,
                    "s3_translate": None,
                    "s5_rewrite": None,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@contextmanager
def _running_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request_server_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    try:
        with urlopen(url) as response:
            return response.status, response.read(), dict(response.headers.items())
    except HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _write_test_process_project(
    project_root: Path,
    *,
    project_name: str,
    youtube_url: str,
    needs_review_count: int = 2,
) -> Path:
    project_dir = project_root / "projects" / project_name
    (project_dir / "output").mkdir(parents=True, exist_ok=True)
    (project_dir / "output" / "segments").mkdir(parents=True, exist_ok=True)
    (project_dir / "transcript").mkdir(parents=True, exist_ok=True)
    (project_dir / "translation").mkdir(parents=True, exist_ok=True)
    (project_dir / "publish").mkdir(parents=True, exist_ok=True)
    (project_dir / "tts").mkdir(parents=True, exist_ok=True)
    (project_dir / "download_metadata.json").write_text(
        json.dumps(
            {
                "url": youtube_url,
                "video_title": project_name,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for filename in (
        "dubbed_audio_complete.wav",
        "ambient_audio.wav",
        "subtitles.srt",
        "alignment_report.md",
        "background_sounds.txt",
    ):
        (project_dir / "output" / filename).write_text(filename, encoding="utf-8")
    (project_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (project_dir / "publish" / "dubbed_video.mp4").write_bytes(b"video")
    tts_1 = project_dir / "tts" / "segment_001_speaker_a.wav"
    tts_2 = project_dir / "tts" / "segment_002_speaker_b.wav"
    aligned_1 = project_dir / "output" / "segments" / "segment_001_aligned.wav"
    aligned_2 = project_dir / "output" / "segments" / "segment_002_aligned.wav"
    tts_1.write_bytes(b"wav-1")
    tts_2.write_bytes(b"wav-2")
    aligned_1.write_bytes(b"aligned-1")
    aligned_2.write_bytes(b"aligned-2")
    segments_payload = {
        "segments": [
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "display_name": "Speaker A",
                "source_text": "Hello there",
                "cn_text": "你好",
                "tts_cn_text": "你好呀",
                "alignment_method": "force_dsp",
                "rewrite_count": 1,
                "needs_review": True,
                "start_ms": 0,
                "end_ms": 1200,
                "target_duration_ms": 1200,
                "actual_duration_ms": 1400,
                "tts_audio_path": str(tts_1.resolve(strict=False)),
                "aligned_audio_path": str(aligned_1.resolve(strict=False)),
            },
            {
                "segment_id": 2,
                "speaker_id": "speaker_b",
                "display_name": "Speaker B",
                "source_text": "Thanks for joining",
                "cn_text": "欢迎加入",
                "tts_cn_text": "欢迎你加入",
                "alignment_method": "dsp",
                "rewrite_count": 0,
                "needs_review": needs_review_count > 1,
                "start_ms": 1300,
                "end_ms": 2600,
                "target_duration_ms": 1300,
                "actual_duration_ms": 1250,
                "tts_audio_path": str(tts_2.resolve(strict=False)),
                "aligned_audio_path": str(aligned_2.resolve(strict=False)),
            },
            {
                "segment_id": 3,
                "speaker_id": "speaker_a",
                "display_name": "Speaker A",
                "source_text": "This one is fine",
                "cn_text": "这一段没问题",
                "tts_cn_text": "这一段没问题",
                "alignment_method": "direct",
                "rewrite_count": 0,
                "needs_review": False,
                "start_ms": 2700,
                "end_ms": 3600,
                "target_duration_ms": 900,
                "actual_duration_ms": 900,
                "tts_audio_path": None,
                "aligned_audio_path": None,
            },
        ]
    }
    (project_dir / "translation" / "segments.json").write_text(
        json.dumps(segments_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (project_dir / "transcript" / "transcript.json").write_text(
        json.dumps(
            {
                "lines": [
                    {
                        "index": 1,
                        "speaker_id": "speaker_a",
                        "speaker_name": "Speaker A",
                        "source_text": "Hello there",
                        "start_ms": 0,
                        "end_ms": 1200,
                    },
                    {
                        "index": 2,
                        "speaker_id": "speaker_b",
                        "speaker_name": "Speaker B",
                        "source_text": "Thanks for joining",
                        "start_ms": 1300,
                        "end_ms": 2600,
                    },
                    {
                        "index": 3,
                        "speaker_id": "speaker_a",
                        "speaker_name": "Speaker A",
                        "source_text": "This one is fine",
                        "start_ms": 2700,
                        "end_ms": 3600,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (project_dir / "project_state.json").write_text(
        json.dumps(
            {
                "project_id": project_name,
                "stages": {
                    "ingestion": {
                        "status": "done",
                        "started_at": "2026-03-18T00:00:00+00:00",
                        "finished_at": "2026-03-18T00:00:05+00:00",
                        "updated_at": "2026-03-18T00:00:05+00:00",
                        "error_message": None,
                        "payload": {
                            "execution_mode": "youtube_download",
                            "artifacts": {
                                "file_count": 3,
                            },
                        },
                    },
                    "audio_preparation": {
                        "status": "done",
                        "started_at": "2026-03-18T00:00:05+00:00",
                        "finished_at": "2026-03-18T00:00:12+00:00",
                        "updated_at": "2026-03-18T00:00:12+00:00",
                        "error_message": None,
                        "payload": {
                            "execution_mode": "fresh_prepare",
                            "artifacts": {
                                "file_count": 3,
                            },
                        },
                    },
                    "media_understanding": {
                        "status": "done",
                        "started_at": "2026-03-18T00:00:12+00:00",
                        "finished_at": "2026-03-18T00:00:20+00:00",
                        "updated_at": "2026-03-18T00:00:20+00:00",
                        "error_message": None,
                        "payload": {
                            "execution_mode": "assemblyai_transcribe",
                            "line_count": 3,
                            "speaker_count": 2,
                            "artifacts": {
                                "file_count": 2,
                            },
                        },
                    },
                    "translation": {
                        "status": "done",
                        "started_at": "2026-03-18T00:00:20+00:00",
                        "finished_at": "2026-03-18T00:00:35+00:00",
                        "updated_at": "2026-03-18T00:00:35+00:00",
                        "error_message": None,
                        "payload": {
                            "execution_mode": "llm_translate",
                            "segment_count": 3,
                            "artifacts": {
                                "file_count": 1,
                            },
                        },
                    },
                    "alignment": {
                        "status": "done",
                        "started_at": "2026-03-18T00:00:35+00:00",
                        "finished_at": "2026-03-18T00:00:45+00:00",
                        "updated_at": "2026-03-18T00:00:45+00:00",
                        "error_message": None,
                        "payload": {
                            "execution_mode": "legacy_process",
                            "block_count": 3,
                            "needs_review_count": needs_review_count,
                            "artifacts": {
                                "file_count": 2,
                            },
                        },
                    },
                    "legacy_process_output": {
                        "status": "done",
                        "started_at": "2026-03-18T00:00:45+00:00",
                        "finished_at": "2026-03-18T00:00:50+00:00",
                        "updated_at": "2026-03-18T00:00:50+00:00",
                        "error_message": None,
                        "payload": {
                            "execution_mode": "legacy_process_output_dispatch",
                            "segment_count": 3,
                            "needs_review_count": needs_review_count,
                            "manifest_path": str((project_dir / "manifest.json").resolve(strict=False)),
                            "artifacts": {
                                "file_count": 7,
                            },
                        },
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return project_dir


def _write_test_voice_registry(project_root: Path) -> Path:
    registry_path = project_root / "voice_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "speakers": {
                    "speaker_a": {
                        "speaker_name": "Speaker A",
                        "default_voice_id": "clone_a_001",
                        "default_voice_type": "cloned",
                        "voices": [
                            {
                                "voice_id": "clone_a_001",
                                "voice_type": "cloned",
                                "provider": "minimax",
                                "tts_provider": "minimax_tts",
                                "platform": "minimax_domestic",
                                "label": "Speaker A Clone",
                                "created_at": "2026-03-17T09:00:00Z",
                                "source_audio_path": "D:/voices/a.wav",
                                "notes": "primary clone",
                                "verification_status": "verified",
                                "last_verified_at": "2026-03-17T09:10:00Z",
                                "last_verification_success": True,
                                "last_verification_audio_path": "D:/voices/a_verify.wav",
                                "last_verification_error": None,
                            },
                            {
                                "voice_id": "builtin_a_001",
                                "voice_type": "builtin",
                                "provider": "minimax",
                                "tts_provider": "minimax_tts",
                                "platform": "minimax_domestic",
                                "label": "Speaker A Builtin",
                                "created_at": "2026-03-17T09:05:00Z",
                                "source_audio_path": None,
                                "notes": None,
                                "verification_status": "unverified",
                                "last_verified_at": None,
                                "last_verification_success": None,
                                "last_verification_audio_path": None,
                                "last_verification_error": None,
                            },
                        ],
                    },
                    "speaker_b": {
                        "speaker_name": "Speaker B",
                        "default_voice_id": "builtin_b_001",
                        "default_voice_type": "builtin",
                        "voices": [
                            {
                                "voice_id": "builtin_b_001",
                                "voice_type": "builtin",
                                "provider": "minimax",
                                "tts_provider": "minimax_tts",
                                "platform": "minimax_domestic",
                                "label": "Speaker B Builtin",
                                "created_at": "2026-03-17T09:06:00Z",
                                "source_audio_path": None,
                                "notes": "backup builtin",
                                "verification_status": "failed",
                                "last_verified_at": "2026-03-17T09:20:00Z",
                                "last_verification_success": False,
                                "last_verification_audio_path": None,
                                "last_verification_error": "timeout",
                            }
                        ],
                    },
                },
                "project_defaults": {
                    "default_builtin_voice": {
                        "voice_id": "builtin_b_001",
                        "voice_type": "builtin",
                        "provider": "minimax",
                        "tts_provider": "minimax_tts",
                        "platform": "minimax_domestic",
                        "label": "Speaker B Builtin",
                        "created_at": "2026-03-17T09:06:00Z",
                        "notes": "project fallback",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return registry_path


def test_build_translation_model_options_includes_default_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    options = build_translation_model_options(config_path=config_path)

    assert options[0]["alias"] == "default_llm"
    assert options[0]["model_name"] == "gemini-2.5-pro"
    assert any(option["alias"] == "deepseek_chat" for option in options)
    assert any(option["alias"] == "gemini_3_1_flash_lite_preview" for option in options)


def test_set_translation_primary_model_syncs_s2_and_s5_routes_to_s3(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    updated_route = set_translation_primary_model("deepseek_chat", config_path=config_path)
    saved_payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert updated_route == [
        "deepseek_chat",
        "gemini_3_1_flash_lite_preview",
        "default_llm",
        "gpt_41",
    ]
    assert saved_payload["llm_fallbacks"]["s3_translate"] == updated_route
    assert saved_payload["llm_fallbacks"]["s2_infer"] == updated_route
    assert saved_payload["llm_fallbacks"]["s2_review"] == updated_route
    assert saved_payload["llm_fallbacks"]["s5_rewrite"] == updated_route


def test_build_provider_key_options_returns_only_safe_metadata(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    options = build_provider_key_options(config_path=config_path)
    option_map = {option["provider"]: option for option in options}

    assert "api_key" not in option_map["gemini"]
    assert option_map["gemini"]["is_configured"] is True
    assert option_map["gemini"]["configured_source"] == "config"
    assert option_map["deepseek"]["is_configured"] is True
    assert option_map["deepseek"]["configured_source"] == "config"
    assert option_map["openai"]["is_configured"] is True
    assert option_map["openai"]["configured_source"] == "config"
    assert option_map["anthropic"]["is_configured"] is False
    assert option_map["anthropic"]["configured_source"] == ""
    assert "deepseek_chat" in option_map["deepseek"]["model_aliases"]


def test_build_provider_key_options_reports_env_backed_keys_without_exposing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["anthropic"]["api_key"] = None
    config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    options = build_provider_key_options(config_path=config_path)
    option_map = {option["provider"]: option for option in options}

    assert option_map["anthropic"]["is_configured"] is True
    assert option_map["anthropic"]["configured_source"] == "env"
    assert "api_key" not in option_map["anthropic"]


def test_save_web_ui_settings_updates_model_and_provider_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    updated_route = save_web_ui_settings(
        translation_model_alias="deepseek_chat",
        translation_prompt_template=None,
        provider_api_keys={
            "gemini": "new-gemini-key",
            "deepseek": "new-deepseek-key",
            "openai": "",
            "anthropic": "new-anthropic-key",
        },
        config_path=config_path,
    )
    saved_payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert updated_route == [
        "deepseek_chat",
        "gemini_3_1_flash_lite_preview",
        "default_llm",
        "gpt_41",
    ]
    assert saved_payload["gemini"]["api_key"] == "new-gemini-key"
    assert saved_payload["deepseek"]["api_key"] == "new-deepseek-key"
    assert saved_payload["openai"]["api_key"] is None
    assert saved_payload["anthropic"]["api_key"] == "new-anthropic-key"


def test_build_web_ui_snapshot_reflects_saved_primary_model(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    save_web_ui_settings(
        translation_model_alias="deepseek_chat",
        translation_prompt_template=None,
        provider_api_keys={},
        config_path=config_path,
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["settings"]["selected_translation_model"] == "deepseek_chat"
    assert snapshot["settings"]["s3_translate_route"][0]["alias"] == "deepseek_chat"


def test_create_web_ui_server_uses_job_api_manager_by_default() -> None:
    server = create_web_ui_server(port=0)

    try:
        assert isinstance(server.job_manager, JobAPIBackedJobManager)  # type: ignore[attr-defined]
        assert server.config_path == str(server.job_manager.config_path)  # type: ignore[attr-defined]
    finally:
        server.server_close()


def test_job_api_backed_manager_submits_and_polls_via_job_api(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    youtube_url = "https://www.youtube.com/watch?v=job-api-submit"
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        calls.append((method, path, payload))
        if method == "POST" and path == "/jobs":
            return {
                "job_id": "job-from-api",
                "status": "queued",
                "speakers": "2",
                "voice_a": "job-api-voice-a",
                "voice_b": "job-api-voice-b",
            }
        if method == "GET" and path == "/jobs/job-from-api":
            return {
                "job_id": "job-from-api",
                "source_ref": youtube_url,
                "speakers": "2",
                "voice_a": "job-api-voice-a",
                "voice_b": "job-api-voice-b",
                "status": "running",
                "current_stage": "ingestion",
                "progress_message": "Downloading: 12.5% of 100MiB",
                "started_at": "2026-03-18T00:00:00Z",
                "completed_at": None,
                "project_dir": None,
                "review_gate": None,
            }
        if method == "GET" and path == "/jobs/job-from-api/logs":
            return {
                "job_id": "job-from-api",
                "lines": ["[S0] Downloading source..."],
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    manager = JobAPIBackedJobManager(
        project_root=tmp_path,
        config_path=config_path,
        request_json=fake_request_json,
    )

    snapshot = manager.start_job(
        youtube_url=youtube_url,
        speakers="2",
        voice_a="job-api-voice-a",
        voice_b="job-api-voice-b",
        translation_model_alias="deepseek_chat",
    )

    assert calls == [
        (
            "POST",
            "/jobs",
            {
                "job_type": "localize_video",
                "source": {
                    "type": "youtube_url",
                    "value": youtube_url,
                },
                "output_target": "editor",
                "speakers": "2",
                "voice_a": "job-api-voice-a",
                "voice_b": "job-api-voice-b",
            },
        ),
        ("GET", "/jobs/job-from-api", None),
        ("GET", "/jobs/job-from-api/logs", None),
    ]
    assert snapshot["job_id"] == "job-from-api"
    assert snapshot["status"] == "running"
    assert snapshot["current_stage"] == "ingestion"
    assert snapshot["current_message"] == "下载中：12.5% of 100MiB"
    assert snapshot["logs"] == ["[S0] Downloading source..."]
    assert snapshot["speakers"] == "2"
    assert snapshot["voice_a"] == "job-api-voice-a"
    assert snapshot["voice_b"] == "job-api-voice-b"
    assert snapshot["control_mode"] == "job_api"


def test_job_api_backed_manager_surfaces_job_api_http_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    def fake_urlopen(request, timeout=5):
        del timeout
        raise HTTPError(
            request.full_url,
            HTTPStatus.CONFLICT,
            "Conflict",
            hdrs=None,
            fp=BytesIO(
                json.dumps(
                    {"error": "已有任务正在运行，请等待当前任务完成。"},
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
        )

    monkeypatch.setattr("services.web_ui.urlopen", fake_urlopen)

    manager = JobAPIBackedJobManager(
        project_root=tmp_path,
        config_path=config_path,
        job_api_base_url="http://127.0.0.1:8877",
    )

    with pytest.raises(ValueError, match="已有任务正在运行，请等待当前任务完成。"):
        manager.start_job(
            youtube_url="https://www.youtube.com/watch?v=test",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            translation_model_alias="default_llm",
        )


def test_job_api_backed_manager_refreshes_snapshot_after_submit_conflict(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    active_job_id = "job-real-active"

    def fake_request_json(
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        calls.append((method, path, payload))
        if method == "POST" and path == "/jobs":
            raise JobAPIRequestError(
                HTTPStatus.CONFLICT,
                "job job_ee81143d30a6469192d42332b7152441 is still active with status running",
            )
        if method == "GET" and path == "/jobs":
            return {
                "jobs": [
                    {
                        "job_id": active_job_id,
                        "status": "running",
                    }
                ]
            }
        if method == "GET" and path == f"/jobs/{active_job_id}":
            return {
                "job_id": active_job_id,
                "source_ref": "https://www.youtube.com/watch?v=blocking-job",
                "speakers": "1",
                "voice_a": None,
                "voice_b": None,
                "status": "running",
                "current_stage": "media_understanding",
                "progress_message": "Processing active job...",
                "started_at": "2026-03-19T03:23:20Z",
                "completed_at": None,
                "project_dir": None,
                "review_gate": None,
            }
        if method == "GET" and path == f"/jobs/{active_job_id}/logs":
            return {
                "job_id": active_job_id,
                "lines": ["[S1] Processing active job..."],
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    manager = JobAPIBackedJobManager(
        project_root=tmp_path,
        config_path=config_path,
        request_json=fake_request_json,
    )
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-completed-before-conflict",
        status="succeeded",
        youtube_url="https://www.youtube.com/watch?v=completed-before-conflict",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        translation_model_alias="default_llm",
        project_dir=None,
        current_stage="completed",
        current_message="Job completed successfully.",
        started_at="2026-03-19T03:00:00Z",
        completed_at="2026-03-19T03:05:00Z",
        returncode=None,
        logs=["[S6] Done"],
        review_gate=None,
        control_mode="job_api",
    )

    with pytest.raises(ValueError, match="当前已有任务正在运行，请等待完成后再提交新任务。"):
        manager.start_job(
            youtube_url="https://www.youtube.com/watch?v=test",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            translation_model_alias="default_llm",
        )

    snapshot = manager.snapshot()

    assert calls[:4] == [
        (
            "POST",
            "/jobs",
            {
                "job_type": "localize_video",
                "source": {
                    "type": "youtube_url",
                    "value": "https://www.youtube.com/watch?v=test",
                },
                "output_target": "editor",
                "speakers": "auto",
                "voice_a": None,
                "voice_b": None,
            },
        ),
        ("GET", "/jobs", None),
        ("GET", f"/jobs/{active_job_id}", None),
        ("GET", f"/jobs/{active_job_id}/logs", None),
    ]
    assert snapshot["job_id"] == active_job_id
    assert snapshot["status"] == "running"
    assert snapshot["current_stage"] == "media_understanding"
    assert snapshot["current_message"] == "Processing active job..."
    assert snapshot["logs"] == ["[S1] Processing active job..."]


def test_job_api_backed_manager_continue_uses_job_api(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = tmp_path / "projects" / "job_api_continue"
    project_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-api-continue"
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    continued = False

    def fake_request_json(
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        nonlocal continued
        calls.append((method, path, payload))
        if method == "GET" and path == f"/jobs/{job_id}":
            return {
                "job_id": job_id,
                "source_ref": "https://www.youtube.com/watch?v=job-api-continue",
                "status": "running" if continued else "waiting_for_review",
                "current_stage": "draft" if continued else "voice_review",
                "progress_message": "Resuming..." if continued else "voice review required before continue",
                "started_at": "2026-03-18T00:00:00Z",
                "completed_at": None,
                "project_dir": str(project_dir.resolve(strict=False)),
                "review_gate": None
                if continued
                else {
                    "stage": "voice_review",
                    "message": "voice review required before continue",
                },
            }
        if method == "GET" and path == f"/jobs/{job_id}/logs":
            return {
                "job_id": job_id,
                "lines": ["[S3] Resuming..."] if continued else ["[WEB_REVIEW] voice review required"],
            }
        if method == "POST" and path == f"/jobs/{job_id}/continue":
            continued = True
            return {
                "job_id": job_id,
                "status": "running",
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    manager = JobAPIBackedJobManager(
        project_root=tmp_path,
        config_path=config_path,
        request_json=fake_request_json,
    )
    manager._snapshot = ProcessJobSnapshot(
        job_id=job_id,
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=job-api-continue",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        translation_model_alias="deepseek_chat",
        project_dir=str(project_dir.resolve(strict=False)),
        current_stage="voice_review",
        current_message="voice review required before continue",
        started_at="2026-03-18T00:00:00Z",
        completed_at=None,
        returncode=None,
        logs=[],
        review_gate={
            "stage": "voice_review",
            "message": "voice review required before continue",
        },
        control_mode="job_api",
    )

    snapshot = manager.continue_after_review(expected_stage="voice_review")

    assert ("POST", f"/jobs/{job_id}/continue", {}) in calls
    assert snapshot["status"] == "running"
    assert snapshot["current_stage"] == "draft"
    assert snapshot["current_message"] == "Resuming..."
    assert snapshot["logs"] == ["[S3] Resuming..."]
    assert snapshot["control_mode"] == "job_api"


def test_resolve_authoritative_review_project_dir_rejects_mismatched_payload_path(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    current_project_dir = _write_test_process_project(
        tmp_path,
        project_name="authoritative_review_project",
        youtube_url="https://www.youtube.com/watch?v=review-authoritative",
    )
    other_project_dir = _write_test_process_project(
        tmp_path,
        project_name="mismatched_review_project",
        youtube_url="https://www.youtube.com/watch?v=review-mismatch",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-review-authoritative",
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=review-authoritative",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="deepseek_chat",
        project_dir=str(current_project_dir.resolve(strict=False)),
        current_stage="speaker_review",
        current_message="speaker review required",
        started_at="2026-03-19T00:00:00Z",
        completed_at=None,
        returncode=None,
        logs=[],
        review_gate={"stage": "speaker_review", "message": "speaker review required"},
    )

    with pytest.raises(ValueError, match="project_dir"):
        _resolve_authoritative_review_project_dir(
            manager=manager,
            requested_project_dir=str(other_project_dir.resolve(strict=False)),
            expected_stage="speaker_review",
            require_waiting_review=True,
        )


def test_resolve_authoritative_review_project_dir_requires_matching_waiting_stage(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    current_project_dir = _write_test_process_project(
        tmp_path,
        project_name="review_stage_guard_project",
        youtube_url="https://www.youtube.com/watch?v=review-stage-guard",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-review-stage-guard",
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=review-stage-guard",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="deepseek_chat",
        project_dir=str(current_project_dir.resolve(strict=False)),
        current_stage="voice_review",
        current_message="voice review required",
        started_at="2026-03-19T00:00:00Z",
        completed_at=None,
        returncode=None,
        logs=[],
        review_gate={"stage": "voice_review", "message": "voice review required"},
    )

    with pytest.raises(ValueError, match="review 阶段"):
        _resolve_authoritative_review_project_dir(
            manager=manager,
            requested_project_dir=str(current_project_dir.resolve(strict=False)),
            expected_stage="translation_review",
            require_waiting_review=True,
        )


def _legacy_test_render_web_ui_html_preserves_unsaved_settings_during_polling() -> None:
    html = render_web_ui_html()

    assert "function readSettingsDraft()" in html
    assert "const preserveDraft = options.preserveDraft !== false;" in html
    assert 'await refreshState({ preserveDraft: false });' in html
    assert "await refreshState();" in html
    assert 'id="download-progress-wrap"' in html
    assert 'currentMessage.match(/^下载中：([0-9]+(?:[.][0-9]+)?)%(.*)$/);' in html
    assert 'id="speaker-infer-prompt"' in html
    assert 'id="translation-prompt"' in html
    assert 'id="rewrite-prompt"' in html
    assert "__CONTEXT_EXCERPT__" in html
    assert "__GROUPS_JSON__" in html
    assert "__TTS_CN_TEXT__" in html


def test_render_web_ui_html_includes_tabs_results_and_polling_safe_drafts() -> None:
    html = render_web_ui_html()

    assert "function readSettingsDraft()" in html
    assert "function readResultsDraft()" in html
    assert "function readReviewDraft()" in html
    assert "function readTranslationDraft()" in html
    assert "function readAudioAlignmentDraft()" in html
    assert "function getFilteredTranscriptReviewItems(results, filters = null, confirmations = null)" in html
    assert "function getFilteredTranslationReviewItems(results, filters = null, state = null)" in html
    assert "function applyBulkTranscriptReviewAction(action)" in html
    assert "function applyBulkTranslationReviewAction(action)" in html
    assert "function initializeTabs()" in html
    assert "function renderResults(results, options = {})" in html
    assert "function renderTranscriptReview(results, options = {})" in html
    assert "function renderTranslationReview(results, options = {})" in html
    assert "function renderVoiceLibrary(results, options = {})" in html
    assert "function renderAudioAlignment(results, options = {})" in html
    assert "function buildSaveIndicatorState(projectKey = \"\")" in html
    assert "function renderSaveIndicator(scope, results)" in html
    assert "function isTranslationReviewEditingActive()" in html
    assert "function isAudioAlignmentPlaybackActive()" in html
    assert "function syncAudioAlignmentPlaybackIndicators()" in html
    assert "function initializeReviewControls()" in html
    assert "function initializeTranslationControls()" in html
    assert "function initializeVoiceLibraryControls()" in html
    assert "function initializeAudioAlignmentControls()" in html
    assert "function renderProjectStateList(projectState)" in html
    assert "const preserveDraft = options.preserveDraft !== false;" in html
    assert 'await refreshState({ preserveDraft: false });' in html
    assert "await refreshState();" in html
    assert 'id="tab-button-run"' in html
    assert 'id="tab-button-results"' in html
    assert 'id="tab-button-translation"' in html
    assert 'id="tab-button-voice-library"' in html
    assert 'id="tab-button-audio-alignment"' in html
    assert 'data-tab-panel="results"' in html
    assert 'data-tab-panel="review"' in html
    assert 'data-tab-panel="translation"' in html
    assert 'data-tab-panel="voice-library"' in html
    assert 'data-tab-panel="audio-alignment"' in html
    assert 'id="download-progress-wrap"' in html
    assert 'currentMessage.match(/^下载中：([0-9]+(?:[.][0-9]+)?)%(.*)$/)' in html
    assert 'id="speaker-infer-prompt"' in html
    assert 'id="translation-prompt"' in html
    assert 'id="rewrite-prompt"' in html
    assert 'id="needs-review-filter-segment-id"' in html
    assert 'id="results-project-state-status"' in html
    assert 'id="results-project-state-latest-stage"' in html
    assert 'id="results-project-state-done-count"' in html
    assert 'id="results-project-state-failed-count"' in html
    assert 'id="results-project-state-path"' in html
    assert 'id="results-source-kind"' in html
    assert 'id="results-source-locator"' in html
    assert 'id="results-project-state-list"' in html
    assert 'id="needs-review-jump"' in html
    assert 'id="needs-review-filter-speaker"' in html
    assert 'id="needs-review-page-size"' in html
    assert 'id="needs-review-list"' in html
    assert 'id="review-filter-segment-id"' in html
    assert 'id="review-jump"' in html
    assert 'id="review-filter-status"' in html
    assert 'id="review-list"' in html
    assert 'id="review-save-status"' in html
    assert 'id="review-bulk-confirm-speaker"' in html
    assert 'id="review-bulk-confirm-transcript"' in html
    assert 'id="review-bulk-reset"' in html
    assert 'id="translation-filter-segment-id"' in html
    assert 'id="translation-jump"' in html
    assert 'id="translation-filter-status"' in html
    assert 'id="translation-save"' in html
    assert 'id="translation-save-status"' in html
    assert 'id="translation-approve"' in html
    assert 'id="translation-bulk-confirm"' in html
    assert 'id="translation-bulk-mark-rewrite"' in html
    assert 'id="translation-bulk-reset"' in html
    assert 'id="translation-gate-message"' in html
    assert 'id="translation-stage-callout" class="panel review-attention-callout"' in html
    assert 'id="translation-list"' in html
    assert ".translation-review-textarea" in html
    assert 'class="translation-review-textarea"' in html
    assert 'id="voice-library-filter-speaker"' in html
    assert 'id="voice-library-project-default-select"' in html
    assert 'id="voice-library-binding-list"' in html
    assert 'id="voice-library-registry-list"' in html
    assert 'class="panel review-attention-callout" id="voice-review-wrap"' in html
    assert 'id="voice-review-approve"' in html
    assert 'id="voice-review-cancel"' in html
    assert 'id="review-stage-callout" class="panel review-attention-callout"' in html
    assert 'id="pending-review-wrap" class="panel review-attention-callout"' in html
    assert 'id="audio-alignment-filter-segment-id"' in html
    assert 'id="audio-alignment-jump"' in html
    assert 'id="audio-alignment-filter-status"' in html
    assert 'id="audio-alignment-stage-callout" class="panel review-attention-callout"' in html
    assert 'id="audio-alignment-gate-message"' in html
    assert 'id="audio-alignment-list"' in html
    assert 'data-audio-alignment-role="playing-indicator"' in html
    assert 'id="job-highlight-title"' in html
    assert 'id="job-highlight-badge"' in html
    assert 'id="job-highlight-copy"' in html
    assert 'id="job-log-preview"' in html
    assert 'id="job-log-details"' in html
    assert 'id="job-log-details-meta"' in html
    assert 'class="job-summary-card"' in html
    assert 'data-translation-cn-text="true"' in html
    assert 'data-translation-tts-text="true"' in html
    assert "/api/review/translation/save" in html
    assert "/api/review/translation/approve" in html
    assert "/api/voice-library/register-manual" in html
    assert "/api/review/voice/approve" in html
    assert "/api/review/voice/cancel" in html
    assert "/api/project-file?path=" in html
    assert "/api/result-download?project_dir=" in html
    assert "__CONTEXT_EXCERPT__" in html
    assert "__GROUPS_JSON__" in html
    assert "__TTS_CN_TEXT__" in html
    assert 'id="tab-button-settings" class="tab-button workbench-admin-only"' in html
    assert 'class="tab-panel workbench-admin-only" data-tab-panel="settings"' in html
    assert 'id="save-settings-button" class="workbench-admin-only"' in html
    assert 'class="field workbench-admin-only"' in html
    assert html.count('class="panel workbench-admin-only"') >= 2


def test_render_web_ui_html_disables_run_for_waiting_review_and_stop_for_job_api_mode() -> None:
    html = render_web_ui_html()

    assert 'const isQueued = job.status === "queued";' in html
    assert 'const isWaitingForReview = job.status === "waiting_for_review";' in html
    assert 'const isJobApiMode = (job.control_mode || "legacy_process") === "job_api";' in html
    assert 'const canStop = !isJobApiMode && isRunning;' in html
    assert 'id="stop-button" class="warning" type="button" style="display: none;"' in html
    assert 'id="stop-unavailable-hint"' in html
    assert "当前远程工作台模式暂不支持停止任务；如流程进入待确认，请先完成确认后继续。" in html
    assert 'stopButton.style.display = isJobApiMode ? "none" : "";' in html
    assert 'stopUnavailableHint.style.display = isJobApiMode && isActive ? "block" : "none";' in html
    assert '(job.current_stage === "S0" || job.current_stage === "ingestion")' in html
    assert "function clearReviewAttention()" in html
    assert "function applyReviewAttention(stage, options = {})" in html
    assert 'const shouldPulseAttention = job.status === "waiting_for_review" && webUiState.lastAutoReviewStage !== stage;' in html
    assert 'applyReviewAttention(stage, { pulse: shouldPulseAttention, tab: nextTab });' in html
    assert "review-attention-pulse" in html
    assert 'summaryTitle = "任务已完成";' in html
    assert 'logPreviewNode.innerHTML = latestLogLines.map((line, index) => `' in html
    assert 'if (logDetailsNode.open) {' in html


def test_render_web_ui_html_does_not_hydrate_provider_keys_from_state_snapshot() -> None:
    html = render_web_ui_html()

    assert '(provider.api_key || "")' not in html
    assert "provider.is_configured" in html
    assert "provider.configured_source" in html


def test_render_web_ui_html_preserves_review_edits_and_uses_clean_translation_copy() -> None:
    html = render_web_ui_html()

    assert "function isReviewEditingActive()" in html
    assert "&& isReviewEditingActive()" in html
    assert 'document.getElementById("review-speaker-editor").addEventListener("change"' in html
    assert '无法连接到 Web UI 服务，请确认 "python main.py web-ui" 仍在运行，然后刷新页面重试。' in html
    assert "段落确认会同步到项目审校草稿；显示名称修改仍需点击保存发言人草稿。" in html
    assert "翻译确认和需重写标记会同步到项目审校草稿；文本修改仍需点击保存翻译草稿。" in html
    assert "有未保存修改" in html
    assert "正在保存..." in html
    assert "保存失败：" in html
    assert "已保存：" in html
    assert "当前正在等待你确认翻译。" in html
    assert "当前没有待处理的 S3 确认。" in html
    assert "确认状态：" in html
    assert "需要复核" in html
    assert "中文直译" in html
    assert "TTS 文本" in html
    assert "褰撳墠" not in html


def test_render_web_ui_html_includes_bulk_review_actions() -> None:
    html = render_web_ui_html()

    assert "批量确认说话人" in html
    assert "批量确认翻译" in html
    assert 'id="review-bulk-confirm-speaker"' in html
    assert 'id="review-bulk-confirm-transcript"' in html
    assert 'id="translation-bulk-confirm"' in html
    assert 'id="translation-bulk-mark-rewrite"' in html
    assert 'document.getElementById("review-bulk-confirm-speaker").addEventListener("click"' in html
    assert 'document.getElementById("translation-bulk-mark-rewrite").addEventListener("click"' in html
    assert 'buttonNode.textContent = `${label}（${count}）`;' in html


def test_render_web_ui_html_includes_review_navigation_actions() -> None:
    html = render_web_ui_html()

    assert "function openNeedsReviewSegment(tabName, segmentId)" in html
    assert "function navigateTranscriptReview(action)" in html
    assert "function navigateTranslationReview(action)" in html
    assert "function navigateToAudioAlignmentSegment(segmentId, options = {})" in html
    assert 'id="review-nav-prev"' in html
    assert 'id="review-nav-next"' in html
    assert 'id="review-nav-next-pending"' in html
    assert 'id="review-nav-next-needs-review"' in html
    assert 'id="translation-nav-prev"' in html
    assert 'id="translation-nav-next"' in html
    assert 'id="translation-nav-next-pending"' in html
    assert 'id="translation-nav-next-needs-review"' in html
    assert 'data-results-action="open-review"' in html
    assert 'data-results-action="open-translation"' in html
    assert 'data-results-action="open-audio"' in html
    assert 'document.getElementById("review-nav-next").addEventListener("click"' in html
    assert 'document.getElementById("translation-nav-next-needs-review").addEventListener("click"' in html
    assert 'document.getElementById("needs-review-list").addEventListener("click"' in html


class _FakeProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.stdout = iter(
            [
                "[S0] start\n",
                "[S3] progress 1/2\n",
                "[S6] done D:\\test\\output\n",
            ]
        )
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def terminate(self) -> None:
        self._returncode = -15


class _BlockingFakeProcess(_FakeProcess):
    def __init__(self, command: list[str]) -> None:
        super().__init__(command)
        self.stdout = iter(())

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is None:
            time.sleep(0.05)
        return self._returncode or 0


def test_process_job_manager_starts_process_and_tracks_logs(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    popen_calls: list[dict[str, object]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        popen_calls.append({"command": command, "kwargs": kwargs})
        return _FakeProcess(command)

    manager = ProcessJobManager(
        project_root=tmp_path,
        config_path=config_path,
        python_executable="python",
        popen_factory=fake_popen,
    )

    manager.start_job(
        youtube_url="https://www.youtube.com/watch?v=test",
        speakers="2",
        voice_a="voice-a-id",
        voice_b="",
        translation_model_alias="gemini_3_1_flash_lite_preview",
    )

    for _ in range(50):
        snapshot = manager.snapshot()
        if snapshot["status"] != "running":
            break
        time.sleep(0.02)

    snapshot = manager.snapshot()

    assert popen_calls
    assert popen_calls[0]["command"] == [
        "python",
        "-u",
        str(Path(__file__).resolve().parents[1] / "main.py"),
        "process",
        "https://www.youtube.com/watch?v=test",
        "--speakers",
        "2",
        "--wait-for-review",
        "--voice-a",
        "voice-a-id",
    ]
    assert popen_calls[0]["kwargs"]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert popen_calls[0]["kwargs"]["env"]["PYTHONUTF8"] == "1"
    assert popen_calls[0]["kwargs"]["env"]["PYTHONUNBUFFERED"] == "1"
    assert snapshot["status"] == "succeeded"
    assert snapshot["current_stage"] == "S6"
    assert snapshot["translation_model_alias"] == "gemini_3_1_flash_lite_preview"
    assert snapshot["voice_a"] == "voice-a-id"
    assert snapshot["voice_b"] is None
    assert any("[S3]" in line for line in snapshot["logs"])


def test_process_job_manager_stop_job_marks_snapshot_as_stopping(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    def fake_popen(command: list[str], **kwargs: object) -> _BlockingFakeProcess:
        return _BlockingFakeProcess(command)

    manager = ProcessJobManager(
        project_root=tmp_path,
        config_path=config_path,
        python_executable="python",
        popen_factory=fake_popen,
    )

    manager.start_job(
        youtube_url="https://www.youtube.com/watch?v=test",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
    )
    snapshot = manager.stop_job()

    assert snapshot["status"] == "stopping"
    assert snapshot["current_message"] == "正在停止任务..."
    assert any("[WEB]" in line for line in snapshot["logs"])


def test_process_job_manager_can_cancel_waiting_review(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = (tmp_path / "projects" / "voice_review_project").resolve(strict=False)
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-voice-review",
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=voice-review",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        project_dir=str(project_dir),
        current_stage="voice_review",
        current_message="waiting for voice review",
        started_at="2026-03-18T00:00:00Z",
        completed_at=None,
        returncode=0,
        logs=[],
        review_gate={
            "stage": "voice_review",
            "tab": "voice-library",
            "project_dir": str(project_dir),
            "message": "waiting for voice review",
        },
    )

    snapshot = manager.cancel_waiting_review(expected_stage="voice_review")

    assert snapshot["status"] == "cancelled"
    assert snapshot["current_message"] == "任务已取消。"
    assert snapshot["review_gate"] == {}
    assert any("已取消等待人工确认的任务" in line for line in snapshot["logs"])


def test_process_job_manager_updates_status_from_download_progress_and_local_steps(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-download-progress",
        status="running",
        youtube_url="https://www.youtube.com/watch?v=test",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S0",
        current_message="下载视频...",
        started_at="2026-03-17T08:40:00Z",
        completed_at=None,
        returncode=None,
        logs=[],
    )

    manager._record_line(
        "job-download-progress",
        "[download] 46.8% of 446.45MiB at 1.03MiB/s ETA 03:52",
    )
    manager._record_line(
        "job-download-progress",
        "[download] 47.2% of 446.45MiB at 1.10MiB/s ETA 03:40",
    )
    snapshot = manager.snapshot()
    assert snapshot["current_stage"] == "S0"
    assert snapshot["current_message"] == "下载中：47.2% of 446.45MiB at 1.10MiB/s ETA 03:40"
    download_logs = [line for line in snapshot["logs"] if line.startswith("[download]")]
    assert download_logs == ["[download] 47.2% of 446.45MiB at 1.10MiB/s ETA 03:40"]

    manager._record_line("job-download-progress", "[S0] 正在提取音频...")
    snapshot = manager.snapshot()
    assert snapshot["current_stage"] == "S0"
    assert snapshot["current_message"] == "正在提取音频..."
    assert any("[download]" in line for line in snapshot["logs"])


def test_process_job_manager_updates_latest_message_from_non_stage_logs(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-generic-logs",
        status="running",
        youtube_url="https://www.youtube.com/watch?v=test",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S0",
        current_message="下载中：99.9%",
        started_at="2026-03-17T08:40:00Z",
        completed_at=None,
        returncode=None,
        logs=[],
    )

    manager._record_line("job-generic-logs", "AssemblyAI upload started...")
    snapshot = manager.snapshot()

    assert snapshot["current_stage"] == "S0"
    assert snapshot["current_message"] == "AssemblyAI upload started..."


def test_build_web_ui_snapshot_reports_selected_translation_model(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["settings"]["selected_translation_model"] == "gemini_3_1_flash_lite_preview"
    assert snapshot["settings"]["s3_translate_route"][0]["alias"] == "gemini_3_1_flash_lite_preview"
    assert any(
        option["provider"] == "gemini" for option in snapshot["settings"]["provider_key_options"]
    )
    assert all("api_key" not in option for option in snapshot["settings"]["provider_key_options"])
    assert "__CONTEXT_EXCERPT__" in snapshot["settings"]["speaker_infer_prompt_template"]
    assert snapshot["settings"]["speaker_infer_prompt_source"] == "default"
    assert "__GROUPS_JSON__" in snapshot["settings"]["translation_prompt_template"]
    assert snapshot["settings"]["translation_prompt_source"] == "default"
    assert "__TTS_CN_TEXT__" in snapshot["settings"]["rewrite_prompt_template"]
    assert snapshot["settings"]["rewrite_prompt_source"] == "default"
    assert snapshot["job"]["status"] == "idle"


def test_build_web_ui_snapshot_does_not_autoload_latest_project_when_idle(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    _write_test_process_project(
        tmp_path,
        project_name="existing_project",
        youtube_url="https://www.youtube.com/watch?v=existing123",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["job"]["status"] == "idle"
    assert snapshot["results"]["available"] is False
    assert snapshot["results"]["source"] == "no_project_match"
    assert snapshot["results"]["project_dir"] is None
    assert snapshot["results"]["project_state"]["available"] is False
    assert snapshot["results"]["project_state"]["stages"] == []
    assert snapshot["results"]["transcript_review"]["items"] == []
    assert snapshot["results"]["translation_review"]["items"] == []


def test_build_web_ui_snapshot_includes_recent_results_and_needs_review_items(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="demo_project",
        youtube_url="https://www.youtube.com/watch?v=demo123",
    )
    _write_test_voice_registry(tmp_path)
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-demo",
        status="succeeded",
        youtube_url="https://www.youtube.com/watch?v=demo123",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S6",
        current_message="done",
        started_at="2026-03-17T00:00:00Z",
        completed_at="2026-03-17T00:01:00Z",
        returncode=0,
        logs=[],
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["available"] is True
    assert snapshot["results"]["source"] == "matched_youtube_url"
    assert snapshot["results"]["source_label"] == "按当前任务 URL 匹配到项目"
    assert snapshot["results"]["project_dir"] == str(project_dir.resolve(strict=False))
    assert snapshot["results"]["manifest_path"] == str((project_dir / "manifest.json").resolve(strict=False))
    assert snapshot["results"]["project_state"]["available"] is True
    assert snapshot["results"]["project_state"]["project_id"] == "demo_project"
    assert snapshot["results"]["project_state"]["overall_status"] == "done"
    assert snapshot["results"]["project_state"]["stage_count"] == 6
    assert snapshot["results"]["project_state"]["completed_stage_count"] == 6
    assert snapshot["results"]["project_state"]["latest_stage_name"] == "legacy_process_output"
    assert snapshot["results"]["project_state"]["latest_stage_status"] == "done"
    assert snapshot["results"]["project_state"]["path"] == str((project_dir / "project_state.json").resolve(strict=False))
    assert snapshot["results"]["project_state"]["stages"][0]["name"] == "ingestion"
    assert snapshot["results"]["project_state"]["stages"][2]["summary"] == "assemblyai_transcribe | 3 lines | 2 speakers | 2 artifacts"
    assert snapshot["results"]["project_state"]["stages"][-1]["name"] == "legacy_process_output"
    assert snapshot["results"]["project_state"]["stages"][-1]["summary"] == "legacy_process_output_dispatch | 3 segments | 2 needs review | 7 artifacts"
    assert snapshot["results"]["needs_review"]["total_items"] == 2
    assert snapshot["results"]["needs_review"]["default_page_size"] == 20
    assert snapshot["results"]["needs_review"]["page_size_options"] == [20, 50, 100]
    assert snapshot["results"]["needs_review"]["speaker_options"] == [
        {"value": "speaker_a", "label": "Speaker A"},
        {"value": "speaker_b", "label": "Speaker B"},
    ]
    assert snapshot["results"]["needs_review"]["items"][0]["segment_id"] == 1
    assert snapshot["results"]["needs_review"]["items"][0]["tts_cn_text"] == "你好呀"
    assert snapshot["results"]["transcript_review"]["total_items"] == 3
    assert snapshot["results"]["transcript_review"]["default_page_size"] == 20
    assert snapshot["results"]["transcript_review"]["page_size_options"] == [20, 50, 100]
    assert snapshot["results"]["transcript_review"]["speaker_options"] == [
        {"value": "speaker_a", "label": "Speaker A"},
        {"value": "speaker_b", "label": "Speaker B"},
    ]
    assert snapshot["results"]["transcript_review"]["items"][0]["needs_review"] is True
    assert snapshot["results"]["transcript_review"]["items"][2]["segment_id"] == 3
    assert snapshot["results"]["translation_review"]["total_items"] == 3
    assert snapshot["results"]["translation_review"]["default_page_size"] == 20
    assert snapshot["results"]["translation_review"]["page_size_options"] == [20, 50, 100]
    assert snapshot["results"]["translation_review"]["speaker_options"] == [
        {"value": "speaker_a", "label": "Speaker A"},
        {"value": "speaker_b", "label": "Speaker B"},
    ]
    assert snapshot["results"]["translation_review"]["items"][0]["cn_text"] == "你好"
    assert snapshot["results"]["translation_review"]["items"][1]["tts_cn_text"] == "欢迎你加入"
    assert snapshot["results"]["audio_alignment"]["total_items"] == 3
    assert snapshot["results"]["audio_alignment"]["default_page_size"] == 20
    assert snapshot["results"]["audio_alignment"]["page_size_options"] == [20, 50, 100]
    assert snapshot["results"]["audio_alignment"]["speaker_options"] == [
        {"value": "speaker_a", "label": "Speaker A"},
        {"value": "speaker_b", "label": "Speaker B"},
    ]
    assert snapshot["results"]["audio_alignment"]["items"][0]["tts_audio_path"].endswith(
        "tts\\segment_001_speaker_a.wav"
    )
    assert snapshot["results"]["audio_alignment"]["items"][0]["aligned_audio_path"].endswith(
        "output\\segments\\segment_001_aligned.wav"
    )
    assert snapshot["results"]["audio_alignment"]["items"][0]["has_audio_preview"] is True
    assert snapshot["results"]["audio_alignment"]["items"][2]["tts_audio_path"] is None
    assert snapshot["results"]["audio_alignment"]["items"][2]["has_audio_preview"] is False
    assert snapshot["results"]["voice_library"]["speaker_count"] == 2
    assert snapshot["results"]["voice_library"]["voice_count"] == 3
    assert snapshot["results"]["voice_library"]["builtin_voice_count"] == 2
    assert snapshot["results"]["voice_library"]["project_default_builtin_voice"]["voice_id"] == "builtin_b_001"
    assert snapshot["results"]["voice_library"]["current_project_speakers"][0]["resolved_voice_id"] == "clone_a_001"
    assert snapshot["results"]["voice_library"]["current_project_speakers"][1]["default_voice_id"] == "builtin_b_001"
    assert snapshot["results"]["voice_library"]["speakers"][0]["voices"][0]["voice_id"] == "clone_a_001"
    assert snapshot["results"]["editor_outputs"][2]["path"].endswith("dubbed_audio_complete.wav")
    assert snapshot["results"]["publish_outputs"][2]["path"].endswith("dubbed_video.mp4")


def test_build_web_ui_snapshot_includes_active_voice_review_details(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    _write_test_voice_registry(tmp_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="voice_review_snapshot",
        youtube_url="https://www.youtube.com/watch?v=voice-review-snapshot",
    )
    (project_dir / "review_state.json").write_text(
        json.dumps(
            {
                "active_stage": "voice_review",
                "stages": {
                    "voice_review": {
                        "stage": "voice_review",
                        "tab": "voice-library",
                        "status": "pending",
                        "updated_at": "2026-03-18T03:50:49.223596+00:00",
                        "approved_at": None,
                        "payload": {
                            "reason": "sample_too_short",
                            "message": "Speaker A 样本不足，等待在 Web UI 选择音色、输入 Voice ID，或取消任务。",
                            "speakers": [
                                {
                                    "speaker_id": "speaker_a",
                                    "speaker_label": "Speaker A",
                                    "speaker_name": "Speaker A",
                                    "voice_arg_name": "voice-a",
                                    "sample_path": str((project_dir / "voice_samples" / "speaker_a_sample.wav").resolve(strict=False)),
                                    "sample_duration_s": 6.9,
                                    "silence_ratio": 0.38,
                                }
                            ],
                        },
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-voice-review-snapshot",
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=voice-review-snapshot",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        project_dir=str(project_dir.resolve(strict=False)),
        current_stage="S2",
        current_message="waiting for voice review",
        started_at="2026-03-18T00:00:00Z",
        completed_at=None,
        returncode=0,
        logs=[],
        review_gate={
            "stage": "voice_review",
            "tab": "voice-library",
            "project_dir": str(project_dir.resolve(strict=False)),
            "message": "waiting for voice review",
        },
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["voice_library"]["active_review"]["stage"] == "voice_review"
    assert snapshot["results"]["voice_library"]["active_review"]["reason"] == "sample_too_short"
    assert snapshot["results"]["voice_library"]["active_review"]["speakers"][0]["speaker_id"] == "speaker_a"
    assert snapshot["results"]["voice_library"]["active_review"]["speakers"][0]["sample_duration_s"] == 6.9
    assert snapshot["results"]["voice_library"]["active_review"]["speakers"][0]["resolved_voice_id"] == "clone_a_001"


def test_build_web_ui_snapshot_prefers_manifest_artifact_paths_for_results_outputs(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="manifest_driven_project",
        youtube_url="https://www.youtube.com/watch?v=manifest123",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-manifest",
        status="succeeded",
        youtube_url="https://www.youtube.com/watch?v=manifest123",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S6",
        current_message="done",
        started_at="2026-03-17T00:00:00Z",
        completed_at="2026-03-17T00:01:00Z",
        returncode=0,
        logs=[],
    )

    manifest_editor_dir = project_dir / "canonical_editor"
    manifest_publish_dir = project_dir / "canonical_publish"
    manifest_state_dir = project_dir / "canonical_state"
    manifest_editor_dir.mkdir(parents=True, exist_ok=True)
    manifest_publish_dir.mkdir(parents=True, exist_ok=True)
    manifest_state_dir.mkdir(parents=True, exist_ok=True)
    manifest_audio_path = (manifest_editor_dir / "final_mix.wav").resolve(strict=False)
    manifest_video_path = (manifest_publish_dir / "final_video.mp4").resolve(strict=False)
    manifest_state_path = (manifest_state_dir / "project_state.json").resolve(strict=False)
    manifest_audio_path.write_bytes(b"manifest-audio")
    manifest_video_path.write_bytes(b"manifest-video")
    manifest_state_path.write_text(
        (project_dir / "project_state.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    (project_dir / "output" / "dubbed_audio_complete.wav").unlink()
    (project_dir / "publish" / "dubbed_video.mp4").unlink()
    (project_dir / "project_state.json").unlink()
    (project_dir / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_index": {
                    "editor.dubbed_audio_complete": str(manifest_audio_path),
                    "publish.dubbed_video": str(manifest_video_path),
                    "state.project": str(manifest_state_path),
                    "translation.segments": str((project_dir / "translation" / "segments.json").resolve(strict=False)),
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["editor_outputs"][2]["path"] == str(manifest_audio_path)
    assert snapshot["results"]["publish_outputs"][2]["path"] == str(manifest_video_path)
    assert snapshot["results"]["project_state"]["path"] == str(manifest_state_path)
    assert snapshot["results"]["project_state"]["project_id"] == "manifest_driven_project"


def test_build_web_ui_snapshot_exposes_only_whitelisted_result_download_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="result_download_keys_project",
        youtube_url="https://www.youtube.com/watch?v=result-keys",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-result-keys",
        status="succeeded",
        youtube_url="https://www.youtube.com/watch?v=result-keys",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S6",
        current_message="done",
        started_at="2026-03-17T00:00:00Z",
        completed_at="2026-03-17T00:01:00Z",
        returncode=0,
        logs=[],
    )
    (project_dir / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_index": {
                    "editor.dubbed_audio_complete": "output/dubbed_audio_complete.wav",
                    "editor.subtitles": "output/subtitles.srt",
                    "translation.segments": "translation/segments.json",
                    "publish.dubbed_video": "publish/dubbed_video.mp4",
                    "state.project": "project_state.json",
                    "state.review": "review_state.json",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_web_ui_snapshot(manager=manager)
    editor_outputs = {item["label"]: item for item in snapshot["results"]["editor_outputs"]}
    publish_outputs = {item["label"]: item for item in snapshot["results"]["publish_outputs"]}

    assert editor_outputs["完整配音"]["download_key"] == "editor.dubbed_audio_complete"
    assert editor_outputs["字幕文件"]["download_key"] == "editor.subtitles"
    assert editor_outputs["翻译分段"]["download_key"] == "translation.segments"
    assert publish_outputs["Manifest"]["download_key"] == "manifest.file"
    assert publish_outputs["成品视频"]["download_key"] == "publish.dubbed_video"
    assert editor_outputs["项目目录"]["download_key"] is None
    assert editor_outputs["输出目录"]["download_key"] is None


def test_web_ui_result_download_endpoint_allows_only_public_whitelist(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="result_download_endpoint_project",
        youtube_url="https://www.youtube.com/watch?v=result-download",
    )
    (project_dir / "review_state.json").write_text(
        json.dumps({"stages": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (project_dir / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_index": {
                    "editor.dubbed_audio_complete": "output/dubbed_audio_complete.wav",
                    "editor.subtitles": "output/subtitles.srt",
                    "translation.segments": "translation/segments.json",
                    "publish.dubbed_video": "publish/dubbed_video.mp4",
                    "state.project": "project_state.json",
                    "state.review": "review_state.json",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    server = create_web_ui_server(job_manager=manager, port=0)

    with _running_server(server) as base_url:
        manifest_status, manifest_body, manifest_headers = _request_server_bytes(
            f"{base_url}/api/result-download?project_dir={quote(str(project_dir.resolve(strict=False)))}&key=manifest.file"
        )
        blocked_status, blocked_body, _ = _request_server_bytes(
            f"{base_url}/api/result-download?project_dir={quote(str(project_dir.resolve(strict=False)))}&key=state.review"
        )

    assert manifest_status == HTTPStatus.OK
    assert json.loads(manifest_body.decode("utf-8"))["artifact_index"]["editor.subtitles"] == "output/subtitles.srt"
    assert "attachment;" in manifest_headers["Content-Disposition"]
    assert blocked_status == HTTPStatus.FORBIDDEN
    assert "not allowed" in blocked_body.decode("utf-8")


def test_project_file_endpoint_is_restricted_to_audio_preview_files(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="audio_preview_only_project",
        youtube_url="https://www.youtube.com/watch?v=audio-preview-only",
    )
    review_state_path = project_dir / "review_state.json"
    review_state_path.write_text(json.dumps({"active_stage": None}, ensure_ascii=False), encoding="utf-8")
    audio_path = (project_dir / "tts" / "segment_001_speaker_a.wav").resolve(strict=False)
    other_project_dir = _write_test_process_project(
        tmp_path,
        project_name="audio_preview_other_project",
        youtube_url="https://www.youtube.com/watch?v=audio-preview-other",
    )
    other_audio_path = (other_project_dir / "tts" / "segment_001_speaker_a.wav").resolve(strict=False)
    rogue_audio_path = (tmp_path / "voice_bank" / "rogue_preview.wav").resolve(strict=False)
    rogue_audio_path.parent.mkdir(parents=True, exist_ok=True)
    rogue_audio_path.write_bytes(b"rogue-audio")
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-audio-preview",
        status="succeeded",
        youtube_url="https://www.youtube.com/watch?v=audio-preview-only",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="deepseek_chat",
        project_dir=str(project_dir.resolve(strict=False)),
        current_stage="completed",
        current_message="done",
        started_at="2026-03-19T00:00:00Z",
        completed_at="2026-03-19T00:05:00Z",
        returncode=0,
        logs=[],
        review_gate=None,
    )
    server = create_web_ui_server(job_manager=manager, port=0)

    with _running_server(server) as base_url:
        audio_status, audio_body, audio_headers = _request_server_bytes(
            f"{base_url}/api/project-file?path={quote(str(audio_path))}"
        )
        blocked_status, blocked_body, _ = _request_server_bytes(
            f"{base_url}/api/project-file?path={quote(str(review_state_path.resolve(strict=False)))}"
        )
        cross_project_status, cross_project_body, _ = _request_server_bytes(
            f"{base_url}/api/project-file?path={quote(str(other_audio_path))}"
        )
        rogue_status, rogue_body, _ = _request_server_bytes(
            f"{base_url}/api/project-file?path={quote(str(rogue_audio_path))}"
        )

    assert audio_status == HTTPStatus.OK
    assert audio_body == b"wav-1"
    assert audio_headers["Content-Type"].startswith("audio/")
    assert blocked_status == HTTPStatus.FORBIDDEN
    assert "audio preview" in blocked_body.decode("utf-8")
    assert cross_project_status == HTTPStatus.FORBIDDEN
    assert "current project directory" in cross_project_body.decode("utf-8")
    assert rogue_status == HTTPStatus.FORBIDDEN
    assert "current project directory" in rogue_body.decode("utf-8")


def test_build_web_ui_snapshot_prefers_manifest_artifact_paths_for_review_inputs(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="manifest_review_project",
        youtube_url="https://www.youtube.com/watch?v=manifest-review",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-manifest-review",
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=manifest-review",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S2",
        current_message="waiting for speaker review",
        started_at="2026-03-18T00:00:00Z",
        completed_at=None,
        returncode=0,
        logs=[],
    )

    _save_speaker_review_submission(
        project_dir=project_dir,
        speaker_names_payload={
            "speaker_a": "Manifest Andy",
            "speaker_b": "Manifest Warren",
        },
        segment_speakers_payload={
            "1": "speaker_a",
            "2": "speaker_b",
            "3": "speaker_a",
        },
        review_confirmations_payload={
            "1": {
                "speaker_confirmed": True,
                "transcript_confirmed": True,
            }
        },
        status="pending",
    )

    manifest_review_dir = project_dir / "canonical_review"
    manifest_transcript_dir = project_dir / "canonical_transcript"
    manifest_translation_dir = project_dir / "canonical_translation"
    manifest_review_dir.mkdir(parents=True, exist_ok=True)
    manifest_transcript_dir.mkdir(parents=True, exist_ok=True)
    manifest_translation_dir.mkdir(parents=True, exist_ok=True)
    manifest_review_path = (manifest_review_dir / "review_state.json").resolve(strict=False)
    manifest_transcript_path = (manifest_transcript_dir / "transcript.json").resolve(strict=False)
    manifest_translation_path = (manifest_translation_dir / "segments.json").resolve(strict=False)

    manifest_review_path.write_text(
        (project_dir / "review_state.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    transcript_payload = json.loads((project_dir / "transcript" / "transcript.json").read_text(encoding="utf-8"))
    transcript_payload["lines"][0]["source_text"] = "Manifest transcript line"
    manifest_transcript_path.write_text(
        json.dumps(transcript_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    translation_payload = json.loads((project_dir / "translation" / "segments.json").read_text(encoding="utf-8"))
    translation_payload["segments"][0]["source_text"] = "Manifest transcript line"
    translation_payload["segments"][0]["cn_text"] = "Manifest 翻译"
    manifest_translation_path.write_text(
        json.dumps(translation_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    (project_dir / "review_state.json").unlink()
    (project_dir / "transcript" / "transcript.json").unlink()
    (project_dir / "translation" / "segments.json").unlink()
    (project_dir / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_index": {
                    "state.review": str(manifest_review_path),
                    "media.transcript_structured": str(manifest_transcript_path),
                    "translation.segments": str(manifest_translation_path),
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["review_flow"]["path"] == str(manifest_review_path)
    assert snapshot["results"]["review_flow"]["active_stage"] == "speaker_review"
    assert snapshot["results"]["transcript_review"]["items"][0]["source_text"] == "Manifest transcript line"
    assert snapshot["results"]["transcript_review"]["items"][0]["display_name"] == "Manifest Andy"
    assert snapshot["results"]["translation_review"]["items"][0]["cn_text"] == "Manifest 翻译"


def test_build_web_ui_snapshot_matches_project_by_manifest_source_info_when_metadata_missing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="manifest_source_project",
        youtube_url="https://www.youtube.com/watch?v=manifest-source",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-manifest-source",
        status="succeeded",
        youtube_url="https://www.youtube.com/watch?v=manifest-source",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S6",
        current_message="done",
        started_at="2026-03-17T00:00:00Z",
        completed_at="2026-03-17T00:01:00Z",
        returncode=0,
        logs=[],
    )

    (project_dir / "download_metadata.json").unlink()
    (project_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_info": {
                    "source_kind": "youtube_url",
                    "locator": "https://www.youtube.com/watch?v=manifest-source",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["available"] is True
    assert snapshot["results"]["source"] == "matched_youtube_url"
    assert snapshot["results"]["project_dir"] == str(project_dir.resolve(strict=False))
    assert snapshot["results"]["source_context"]["locator"] == "https://www.youtube.com/watch?v=manifest-source"


def test_build_web_ui_snapshot_prefers_manifest_video_title_for_project_name(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="manifest_title_project",
        youtube_url="https://www.youtube.com/watch?v=manifest-title",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-manifest-title",
        status="succeeded",
        youtube_url="https://www.youtube.com/watch?v=manifest-title",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S6",
        current_message="done",
        started_at="2026-03-17T00:00:00Z",
        completed_at="2026-03-17T00:01:00Z",
        returncode=0,
        logs=[],
    )

    (project_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_info": {
                    "source_kind": "youtube_url",
                    "locator": "https://www.youtube.com/watch?v=manifest-title",
                    "metadata": {
                        "video_title": "Manifest Canonical Title",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["project_name"] == "Manifest Canonical Title"
    assert snapshot["results"]["source_context"]["source_kind"] == "youtube_url"
    assert snapshot["results"]["source_context"]["locator"] == "https://www.youtube.com/watch?v=manifest-title"
    assert snapshot["results"]["source_context"]["video_title"] == "Manifest Canonical Title"


def test_save_translation_review_submission_persists_review_state(tmp_path: Path) -> None:
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="translation_review_pending",
        youtube_url="https://www.youtube.com/watch?v=pending123",
    )
    original_segments = json.loads(
        (project_dir / "translation" / "segments.json").read_text(encoding="utf-8")
    )["segments"]

    normalized_payload = _save_translation_review_submission(
        project_dir=project_dir,
        translation_segments_payload={
            "1": {
                "cn_text": "手工改过的翻译",
                "tts_cn_text": "手工改过的配音文本",
                "translation_confirmed": True,
            },
            "2": {
                "rewrite_requested": True,
            },
        },
        status="pending",
    )

    review_state = json.loads((project_dir / "review_state.json").read_text(encoding="utf-8"))
    translation_stage = review_state["stages"]["translation_review"]

    assert review_state["active_stage"] == "translation_review"
    assert translation_stage["status"] == "pending"
    assert normalized_payload["segments"]["1"]["cn_text"] == "手工改过的翻译"
    assert normalized_payload["segments"]["1"]["tts_cn_text"] == "手工改过的配音文本"
    assert normalized_payload["segments"]["1"]["translation_confirmed"] is True
    assert translation_stage["payload"]["segments"]["2"]["rewrite_requested"] is True
    assert translation_stage["payload"]["segments"]["2"]["cn_text"] == original_segments[1]["cn_text"]
    assert translation_stage["payload"]["segments"]["2"]["tts_cn_text"] == original_segments[1]["tts_cn_text"]


def test_approve_translation_review_submission_writes_segments_json(tmp_path: Path) -> None:
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="translation_review_approved",
        youtube_url="https://www.youtube.com/watch?v=approved123",
    )

    _save_translation_review_submission(
        project_dir=project_dir,
        translation_segments_payload={
            "1": {
                "cn_text": "批准后的翻译",
                "tts_cn_text": "批准后的配音文本",
                "translation_confirmed": True,
                "rewrite_requested": False,
            }
        },
        status="approved",
    )

    review_state = json.loads((project_dir / "review_state.json").read_text(encoding="utf-8"))
    saved_segments = json.loads(
        (project_dir / "translation" / "segments.json").read_text(encoding="utf-8")
    )["segments"]
    first_segment = saved_segments[0]

    assert review_state["active_stage"] is None
    assert review_state["stages"]["translation_review"]["status"] == "approved"
    assert first_segment["cn_text"] == "批准后的翻译"
    assert first_segment["tts_cn_text"] == "批准后的配音文本"
    assert "translation_confirmed" not in first_segment
    assert "rewrite_requested" not in first_segment


def test_save_speaker_review_submission_persists_confirmation_state(tmp_path: Path) -> None:
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="speaker_review_pending",
        youtube_url="https://www.youtube.com/watch?v=speaker-pending",
    )

    normalized_payload = _save_speaker_review_submission(
        project_dir=project_dir,
        speaker_names_payload={
            "speaker_a": "Andy Serwer",
            "speaker_b": "沃伦·巴菲特",
        },
        segment_speakers_payload={
            "1": "speaker_a",
            "2": "speaker_b",
            "3": "speaker_a",
        },
        review_confirmations_payload={
            "1": {
                "speaker_confirmed": True,
                "transcript_confirmed": True,
                "updated_at": "2026-03-18T01:23:45Z",
            },
            "2": {
                "speaker_confirmed": True,
                "transcript_confirmed": False,
            },
        },
        status="pending",
    )

    review_state = json.loads((project_dir / "review_state.json").read_text(encoding="utf-8"))
    speaker_stage = review_state["stages"]["speaker_review"]

    assert review_state["active_stage"] == "speaker_review"
    assert speaker_stage["status"] == "pending"
    assert normalized_payload["confirmations"]["1"]["speaker_confirmed"] is True
    assert normalized_payload["confirmations"]["1"]["transcript_confirmed"] is True
    assert speaker_stage["payload"]["confirmations"]["2"]["speaker_confirmed"] is True
    assert speaker_stage["payload"]["confirmations"]["2"]["transcript_confirmed"] is False


def test_build_web_ui_snapshot_applies_pending_translation_review_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="translation_review_snapshot",
        youtube_url="https://www.youtube.com/watch?v=snapshot123",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-translation-review",
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=snapshot123",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        project_dir=str(project_dir.resolve(strict=False)),
        current_stage="S3",
        current_message="waiting for translation review",
        started_at="2026-03-18T00:00:00Z",
        completed_at=None,
        returncode=0,
        logs=[],
        review_gate={
            "stage": "translation_review",
            "tab": "translation",
            "project_dir": str(project_dir.resolve(strict=False)),
            "message": "waiting for translation review",
        },
    )

    _save_translation_review_submission(
        project_dir=project_dir,
        translation_segments_payload={
            "1": {
                "cn_text": "快照里的翻译覆盖",
                "tts_cn_text": "快照里的配音覆盖",
                "translation_confirmed": True,
            }
        },
        status="pending",
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["translation_review"]["items"][0]["cn_text"] == "快照里的翻译覆盖"
    assert snapshot["results"]["translation_review"]["items"][0]["tts_cn_text"] == "快照里的配音覆盖"
    assert snapshot["results"]["translation_review"]["items"][0]["translation_confirmed"] is True
    assert snapshot["results"]["translation_review"]["confirmed_count"] == 1
    assert snapshot["results"]["review_flow"]["active_stage"] == "translation_review"


def test_build_web_ui_snapshot_applies_saved_speaker_display_names(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="speaker_review_snapshot",
        youtube_url="https://www.youtube.com/watch?v=speaker123",
    )
    manager = ProcessJobManager(project_root=tmp_path, config_path=config_path)
    manager._snapshot = ProcessJobSnapshot(
        job_id="job-speaker-review",
        status="waiting_for_review",
        youtube_url="https://www.youtube.com/watch?v=speaker123",
        speakers="2",
        voice_a=None,
        voice_b=None,
        translation_model_alias="gemini_3_1_flash_lite_preview",
        current_stage="S2",
        current_message="waiting",
        started_at="2026-03-18T00:00:00Z",
        completed_at=None,
        returncode=None,
        logs=[],
        review_gate={
            "stage": "speaker_review",
            "status": "pending",
            "message": "Please review speakers",
            "project_dir": str(project_dir.resolve(strict=False)),
        },
    )

    _save_speaker_review_submission(
        project_dir=project_dir,
        speaker_names_payload={
            "speaker_a": "Andy Serwer",
            "speaker_b": "沃伦·巴菲特",
        },
        segment_speakers_payload={
            "1": "speaker_a",
            "2": "speaker_b",
            "3": "speaker_a",
        },
        review_confirmations_payload={
            "1": {
                "speaker_confirmed": True,
                "transcript_confirmed": True,
                "updated_at": "2026-03-18T01:23:45Z",
            },
            "2": {
                "speaker_confirmed": True,
                "transcript_confirmed": False,
                "updated_at": "2026-03-18T01:24:45Z",
            },
        },
        status="pending",
    )

    snapshot = build_web_ui_snapshot(manager=manager)

    assert snapshot["results"]["transcript_review"]["speaker_options"] == [
        {"value": "speaker_a", "label": "Andy Serwer"},
        {"value": "speaker_b", "label": "沃伦·巴菲特"},
    ]
    assert snapshot["results"]["transcript_review"]["items"][0]["display_name"] == "Andy Serwer"
    assert snapshot["results"]["transcript_review"]["items"][1]["display_name"] == "沃伦·巴菲特"
    assert snapshot["results"]["transcript_review"]["items"][0]["speaker_confirmed"] is True
    assert snapshot["results"]["transcript_review"]["items"][0]["transcript_confirmed"] is True
    assert snapshot["results"]["transcript_review"]["items"][1]["speaker_confirmed"] is True
    assert snapshot["results"]["transcript_review"]["items"][1]["transcript_confirmed"] is False
    assert snapshot["results"]["transcript_review"]["confirmed_count"] == 1
    assert snapshot["results"]["transcript_review"]["speaker_count"] == 2
    assert snapshot["results"]["translation_review"]["items"][0]["display_name"] == "Andy Serwer"
    assert snapshot["results"]["translation_review"]["items"][1]["display_name"] == "沃伦·巴菲特"


def test_approve_speaker_review_submission_writes_speaker_names_to_transcript(tmp_path: Path) -> None:
    project_dir = _write_test_process_project(
        tmp_path,
        project_name="speaker_review_approved",
        youtube_url="https://www.youtube.com/watch?v=speaker-approved",
    )

    _save_speaker_review_submission(
        project_dir=project_dir,
        speaker_names_payload={
            "speaker_a": "Andy Serwer",
            "speaker_b": "沃伦·巴菲特",
        },
        segment_speakers_payload={
            "1": "speaker_a",
            "2": "speaker_b",
            "3": "speaker_a",
        },
        review_confirmations_payload={},
        status="approved",
    )

    transcript_payload = json.loads((project_dir / "transcript" / "transcript.json").read_text(encoding="utf-8"))
    lines = transcript_payload["lines"]

    assert lines[0]["speaker_id"] == "speaker_a"
    assert lines[0]["speaker_name"] == "Andy Serwer"
    assert lines[1]["speaker_id"] == "speaker_b"
    assert lines[1]["speaker_name"] == "沃伦·巴菲特"


def test_save_web_ui_settings_updates_prompt_templates(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    save_web_ui_settings(
        translation_model_alias="deepseek_chat",
        speaker_infer_prompt_template="识别说话人\\n__CONTEXT_EXCERPT__",
        translation_prompt_template="自定义提示词\\n__GROUPS_JSON__",
        rewrite_prompt_template="改写\\n__DIRECTION_DESC__\\n__DIRECTION_INSTRUCTION__\\n__TTS_CN_TEXT__\\n__TARGET_CHARS__",
        provider_api_keys={},
        config_path=config_path,
    )
    saved_payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert saved_payload["prompts"]["s2_infer"] == "识别说话人\\n__CONTEXT_EXCERPT__"
    assert saved_payload["prompts"]["s3_translate"] == "自定义提示词\\n__GROUPS_JSON__"
    assert (
        saved_payload["prompts"]["s5_rewrite"]
        == "改写\\n__DIRECTION_DESC__\\n__DIRECTION_INSTRUCTION__\\n__TTS_CN_TEXT__\\n__TARGET_CHARS__"
    )


def test_save_web_ui_settings_rejects_prompt_without_groups_token(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    with pytest.raises(ValueError, match="__GROUPS_JSON__"):
        save_web_ui_settings(
            translation_model_alias="deepseek_chat",
            translation_prompt_template="缺少输入占位符",
            provider_api_keys={},
            config_path=config_path,
        )


def test_save_web_ui_settings_rejects_s2_prompt_without_context_token(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    with pytest.raises(ValueError, match="__CONTEXT_EXCERPT__"):
        save_web_ui_settings(
            translation_model_alias="deepseek_chat",
            speaker_infer_prompt_template="缺少上下文占位符",
            provider_api_keys={},
            config_path=config_path,
        )


def test_save_web_ui_settings_rejects_s5_prompt_without_required_tokens(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    _write_test_config(config_path)

    with pytest.raises(ValueError, match="__TTS_CN_TEXT__"):
        save_web_ui_settings(
            translation_model_alias="deepseek_chat",
            rewrite_prompt_template="缺少改写占位符",
            provider_api_keys={},
            config_path=config_path,
        )

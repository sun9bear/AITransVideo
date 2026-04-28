from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmark.translation_eval_samples import (
    build_translation_eval_samples,
    validate_sample_payload,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_build_translation_eval_samples_uses_admin_prompt_overrides(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    job_dir = data_root / "projects" / "user_one" / "job_alpha"
    _write_json(
        job_dir / "translation" / "segments.json",
        {
            "segments": [
                {
                    "segment_id": 1,
                    "speaker_id": "speaker_a",
                    "display_name": "Speaker A",
                    "start_ms": 0,
                    "end_ms": 10000,
                    "target_duration_ms": 10000,
                    "source_text": (
                        "Warren Buffett said Berkshire Hathaway had over $381 billion "
                        "in cash, and Greg Abel would become CEO on January 1."
                    ),
                    "cn_text": "沃伦·巴菲特说，伯克希尔·哈撒韦有超过3810亿美元现金。",
                    "tts_cn_text": "沃伦·巴菲特说，伯克希尔·哈撒韦有超过3810亿美元现金。",
                    "actual_duration_ms": 13500,
                    "rewrite_count": 1,
                    "needs_review": False,
                    "alignment_method": "rewrite",
                },
                {
                    "segment_id": 2,
                    "speaker_id": "speaker_b",
                    "display_name": "Speaker B",
                    "start_ms": 10000,
                    "end_ms": 12000,
                    "target_duration_ms": 2000,
                    "source_text": "Yeah, sure.",
                    "cn_text": "嗯，当然。",
                    "tts_cn_text": "嗯，当然。",
                    "actual_duration_ms": 0,
                    "rewrite_count": 0,
                    "needs_review": False,
                    "alignment_method": "",
                },
            ]
        },
    )
    _write_json(
        job_dir / "translation" / "glossary.json",
        {
            "Warren Buffett": "沃伦·巴菲特",
            "Berkshire Hathaway": "伯克希尔·哈撒韦",
            "Greg Abel": "格雷格·艾贝尔",
            "CEO": "首席执行官",
        },
    )
    _write_json(
        job_dir / "project_state.json",
        {
            "stages": {
                "ingestion": {
                    "payload": {
                        "title": "Warren Buffett steps down",
                        "locator": "https://www.youtube.com/watch?v=test123",
                    }
                }
            }
        },
    )
    _write_json(
        data_root / "jobs" / "job_alpha.json",
        {"service_mode": "studio", "tts_provider": "minimax", "tts_model": "speech"},
    )
    _write_json(
        job_dir / "audio" / "probe_calibration.json",
        {
            "global_chars_per_second": 4.0,
            "chars_per_second_by_speaker": {"speaker_a": 3.6, "speaker_b": 4.2},
        },
    )
    admin_settings = tmp_path / "admin_settings.json"
    _write_json(
        admin_settings,
        {
            "review_prompts": {
                "translate": (
                    "CUSTOM_TRANSLATE\n"
                    "标题：__VIDEO_TITLE__\n"
                    "来源：__YOUTUBE_URL__\n"
                    "__GLOSSARY_SECTION__\n"
                    "输入：__GROUPS_JSON__"
                ),
                "rewrite": (
                    "CUSTOM_REWRITE __DIRECTION_DESC__\n"
                    "__DIRECTION_INSTRUCTION__\n"
                    "__TTS_CN_TEXT__\n"
                    "__SOURCE_TEXT__\n"
                    "__TARGET_CHARS__\n"
                    "__CURRENT_CHARS__"
                ),
            }
        },
    )

    output = tmp_path / "samples.json"
    payload = build_translation_eval_samples(
        data_root=data_root,
        output=output,
        max_samples=2,
        window_size=2,
        admin_settings_path=admin_settings,
        force=True,
    )

    validation = validate_sample_payload(payload)
    assert validation["status"] == "ok"
    sample = payload["samples"][0]
    assert "CUSTOM_TRANSLATE" in sample["translation_prompt"]
    assert "Warren Buffett steps down" in sample["translation_prompt"]
    assert "https://www.youtube.com/watch?v=test123" in sample["translation_prompt"]
    assert "Warren Buffett → 沃伦·巴菲特" in sample["translation_prompt"]
    assert "min_chars" in sample["translation_prompt"]
    assert "target_chars" in sample["translation_prompt"]
    assert "review_prompts.translate" in sample["translation_prompt_source"]
    assert any("CUSTOM_REWRITE" in case["rewrite_prompt"] for case in sample["rewrite_cases"])


def test_build_translation_eval_samples_can_redact_source_url(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    job_dir = data_root / "projects" / "user_one" / "job_beta"
    _write_json(
        job_dir / "translation" / "segments.json",
        {
            "segments": [
                {
                    "segment_id": 1,
                    "speaker_id": "speaker_a",
                    "start_ms": 0,
                    "end_ms": 6000,
                    "target_duration_ms": 6000,
                    "source_text": "This platform uses an AI model and an API pipeline.",
                    "cn_text": "这个平台使用人工智能模型和接口流程。",
                    "tts_cn_text": "这个平台使用人工智能模型和接口流程。",
                }
            ]
        },
    )
    _write_json(
        job_dir / "project_state.json",
        {
            "stages": {
                "ingestion": {
                    "payload": {
                        "title": "API demo",
                        "locator": "https://www.youtube.com/watch?v=secret",
                    }
                }
            }
        },
    )

    payload = build_translation_eval_samples(
        data_root=data_root,
        output=tmp_path / "samples.json",
        max_samples=1,
        include_source_url=False,
        force=True,
    )

    sample = payload["samples"][0]
    assert sample["source_url"] == ""
    assert "https://www.youtube.com" not in sample["translation_prompt"]

from __future__ import annotations

import json
from pathlib import Path

from services.translation_quality import (
    build_translation_quality_report,
    evaluate_zh_cn_script_gate,
    write_translation_quality_report,
)


def test_zh_cn_script_gate_allows_chinese_with_latin_terms() -> None:
    result = evaluate_zh_cn_script_gate(
        "我们今天聊 OpenAI 和 Gemini 的模型选择。",
        allowed_latin_terms={"OpenAI", "Gemini"},
    )

    assert result["ok"] is True
    assert result["reason_codes"] == []
    assert result["target_language_full_name"] == "Chinese (Simplified)"


def test_zh_cn_script_gate_flags_latin_only_translation() -> None:
    result = evaluate_zh_cn_script_gate("This is still English output")

    assert result["ok"] is False
    assert "no_cjk_latin_long" in result["reason_codes"]
    assert result["latin_ratio"] == 1.0


def test_translation_quality_report_is_detect_only_and_redacts_text() -> None:
    payload = build_translation_quality_report(
        project_id="job_quality",
        glossary={"OpenAI": "OpenAI"},
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "这是正常中文。",
                "dubbing_mode": "dub",
            },
            {
                "segment_id": 2,
                "speaker_id": "speaker_a",
                "cn_text": "This is wrong script",
                "dubbing_mode": "dub",
            },
            {
                "segment_id": 3,
                "speaker_id": "speaker_b",
                "cn_text": "",
                "dubbing_mode": "keep_original",
            },
        ],
    )

    assert payload["schema_version"] == "translation_quality_report_v1"
    assert payload["advisory_only"] is True
    assert payload["gate_mode"] == "detect_only"
    assert payload["checked_segments"] == 2
    assert payload["skipped_keep_original_segments"] == 1
    assert payload["script_gate_fail_count"] == 1
    issue = payload["issues"][0]
    assert issue["segment_id"] == 2
    assert "text_sha256" in issue
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "This is wrong script" not in serialized


def test_translation_quality_report_writer_is_flagged(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    segments = [{"segment_id": 1, "speaker_id": "speaker_a", "cn_text": "English only"}]

    assert write_translation_quality_report(project_dir, segments=segments) is False
    assert not (project_dir / "reports" / "translation_quality_report.json").exists()

    monkeypatch.setenv("AVT_TRANSLATION_SCRIPT_GATE_SHADOW", "1")
    assert write_translation_quality_report(project_dir, segments=segments) is True

    payload = json.loads(
        (project_dir / "reports" / "translation_quality_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["schema_version"] == "translation_quality_report_v1"
    assert payload["script_gate_fail_count"] == 1

from __future__ import annotations

import json
from pathlib import Path

from services.translation_quality import (
    build_translation_quality_report,
    evaluate_en_script_gate,
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

    payload = json.loads((project_dir / "reports" / "translation_quality_report.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "translation_quality_report_v1"
    assert payload["script_gate_fail_count"] == 1


# ---------------------------------------------------------------------------
# zh->en behavior (2026-07-02 fix): report must stamp the job's real target
# language and dispatch a target-appropriate script gate — the zh-CN gate was
# flagging normal pure-English dubbing text as latin_dominant/no_cjk_*
# (prod repro: job_b07c29cf0652411ca0a7e0461648dc7b).
# ---------------------------------------------------------------------------


def test_en_script_gate_accepts_pure_english() -> None:
    result = evaluate_en_script_gate("Do you like this kind of content? If so, remember to subscribe.")

    assert result["ok"] is True
    assert result["reason_codes"] == []
    assert result["target_language"] == "en"
    assert result["target_language_full_name"] == "English"


def test_en_script_gate_flags_untranslated_chinese() -> None:
    result = evaluate_en_script_gate("这一段话完全没有被翻译成英文。")

    assert result["ok"] is False
    assert "cjk_nontrivial" in result["reason_codes"]
    assert "cjk_dominant" in result["reason_codes"]


def test_en_script_gate_allows_glossary_cjk_terms() -> None:
    result = evaluate_en_script_gate(
        "The founder of 阿里巴巴集团控股 spoke at the conference.",
        allowed_cjk_terms={"阿里巴巴集团控股"},
    )

    assert result["ok"] is True


def test_en_report_glossary_key_side_cjk_is_not_exempt() -> None:
    """@codex round-2 P2: zh->en glossary KEYS are source terms — the glossary
    requires the English VALUE in output, so the zh key appearing verbatim is
    exactly the untranslated-term leak the gate exists to catch."""
    payload = build_translation_quality_report(
        project_id="job_zh_en",
        target_language="en",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "The founder of 阿里巴巴集团控股 spoke at the conference.",
                "dubbing_mode": "dub",
            },
        ],
        glossary={"阿里巴巴集团控股": "Alibaba Group Holding"},
    )

    assert payload["issue_count"] == 1
    assert "cjk_nontrivial" in payload["reason_counts"]


def test_en_report_glossary_value_side_cjk_is_exempt() -> None:
    """Target-side CJK (a term the glossary deliberately keeps in hanzi) is
    legitimate English-output content and must not be flagged."""
    payload = build_translation_quality_report(
        project_id="job_zh_en",
        target_language="en",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "Use the 小红书笔记模板 to format your post today.",
                "dubbing_mode": "dub",
            },
        ],
        glossary={"小红书 note template": "小红书笔记模板"},
    )

    assert payload["issue_count"] == 0


def test_en_target_report_no_false_issues_for_english_dub_script() -> None:
    payload = build_translation_quality_report(
        project_id="job_zh_en",
        target_language="en",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "Do you like this kind of content? If so, subscribe.",
                "dubbing_mode": "dub",
            },
            {
                "segment_id": 2,
                "speaker_id": "speaker_a",
                "cn_text": "This is a perfectly normal English dubbing line.",
                "dubbing_mode": "dub",
            },
        ],
    )

    assert payload["target_language"] == "en"
    assert payload["target_language_full_name"] == "English"
    assert payload["script_gate_supported"] is True
    assert payload["issue_count"] == 0
    assert payload["reason_counts"] == {}
    assert payload["checked_segments"] == 2


def test_en_target_alias_normalized_and_cjk_leak_flagged() -> None:
    payload = build_translation_quality_report(
        project_id="job_zh_en",
        target_language="English",  # alias -> canonical "en"
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "这一段话完全没有被翻译成英文。",
                "dubbing_mode": "dub",
            },
        ],
    )

    assert payload["target_language"] == "en"
    assert payload["issue_count"] == 1
    assert "cjk_nontrivial" in payload["issues"][0]["reason_codes"]


def test_default_target_language_remains_zh_cn() -> None:
    payload = build_translation_quality_report(
        project_id="job_legacy",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "This is still English output",
                "dubbing_mode": "dub",
            },
        ],
    )

    assert payload["target_language"] == "zh-CN"
    assert payload["target_language_full_name"] == "Chinese (Simplified)"
    assert payload["issue_count"] == 1  # zh gate still flags latin-only text


def test_unknown_target_language_runs_no_gate() -> None:
    payload = build_translation_quality_report(
        project_id="job_unknown",
        target_language="fr",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "Ceci est du texte francais.",
                "dubbing_mode": "dub",
            },
        ],
    )

    assert payload["target_language"] == "fr"
    assert payload["script_gate_supported"] is False
    assert payload["issue_count"] == 0
    assert payload["checked_segments"] == 1


def test_writer_passes_target_language_through(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVT_TRANSLATION_SCRIPT_GATE_SHADOW", "1")
    project_dir = tmp_path / "job_zh_en"
    segments = [
        {
            "segment_id": 1,
            "speaker_id": "speaker_a",
            "cn_text": "Pure English dubbing text with many words here.",
            "dubbing_mode": "dub",
        }
    ]

    assert write_translation_quality_report(project_dir, segments=segments, target_language="en") is True

    payload = json.loads((project_dir / "reports" / "translation_quality_report.json").read_text(encoding="utf-8"))
    assert payload["target_language"] == "en"
    assert payload["issue_count"] == 0


def test_translator_report_helper_threads_target_language(tmp_path: Path, monkeypatch) -> None:
    """_maybe_write_translation_quality_report must stamp the pair target."""
    from services.gemini.translator import (
        TranslationResult,
        _maybe_write_translation_quality_report,
    )

    monkeypatch.setenv("AVT_TRANSLATION_SCRIPT_GATE_SHADOW", "1")
    output_root = tmp_path / "job_zh_en" / "translation"
    output_root.mkdir(parents=True)
    result = TranslationResult(
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "cn_text": "Pure English dubbing text with many words here.",
                "dubbing_mode": "dub",
            }
        ],
        total_segments=1,
        output_path=str(output_root / "segments.json"),
    )

    _maybe_write_translation_quality_report(output_root, result, glossary={}, target_language="en")

    payload = json.loads(
        (tmp_path / "job_zh_en" / "reports" / "translation_quality_report.json").read_text(encoding="utf-8")
    )
    assert payload["target_language"] == "en"
    assert payload["issue_count"] == 0

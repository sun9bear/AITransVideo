from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmark.translation_quality_benchmark import (
    BenchmarkConfig,
    evaluate_constraints,
    run_benchmark,
)


class FakeCaller:
    def call(self, model_name: str, prompt: str, *, json_mode: bool) -> str:
        if model_name == "gpt54":
            return json.dumps(
                {
                    "semantic_completeness": 23,
                    "terminology": 15,
                    "length_fit": 12,
                    "oral_naturalness": 13,
                    "context_consistency": 9,
                    "format_compliance": 10,
                    "tts_readiness": 8,
                    "quality_score": 90,
                    "major_issues": [],
                    "brief_reason": "准确自然。",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            [
                {"segment_id": 1, "cn_text": "沃伦巴菲特说伯克希尔哈撒韦现金很多"},
                {"segment_id": 2, "cn_text": "格雷格艾贝尔会接任首席执行官"},
            ],
            ensure_ascii=False,
        )


def _sample_payload() -> dict:
    return {
        "version": "translation_model_eval_samples.v1",
        "prompt_sources": {"translate": {"source": "runtime default"}},
        "coverage": {"samples": 1},
        "samples": [
            {
                "sample_id": "trans_eval_001",
                "video_title": "Buffett sample",
                "selection_reasons": ["glossary_hit", "numeric_financial"],
                "glossary": {
                    "Warren Buffett": "沃伦巴菲特",
                    "Berkshire Hathaway": "伯克希尔哈撒韦",
                    "Greg Abel": "格雷格艾贝尔",
                    "CEO": "首席执行官",
                },
                "groups": [
                    {
                        "segment_id": 1,
                        "speaker_id": "speaker_a",
                        "source_text": "Warren Buffett said Berkshire Hathaway has cash.",
                        "target_duration_seconds": 8.0,
                        "min_chars": 10,
                        "max_chars": 30,
                        "target_chars": 20,
                    },
                    {
                        "segment_id": 2,
                        "speaker_id": "speaker_a",
                        "source_text": "Greg Abel will become CEO.",
                        "target_duration_seconds": 6.0,
                        "min_chars": 8,
                        "max_chars": 24,
                        "target_chars": 16,
                    },
                ],
                "reference_translations": [],
                "translation_prompt": "真实翻译提示词",
            }
        ],
    }


def test_evaluate_constraints_checks_format_lengths_and_glossary() -> None:
    sample = _sample_payload()["samples"][0]
    response = json.dumps(
        [
            {"segment_id": 1, "cn_text": "沃伦巴菲特说伯克希尔哈撒韦现金很多"},
            {"segment_id": 2, "cn_text": "格雷格艾贝尔会接任首席执行官"},
        ],
        ensure_ascii=False,
    )

    result = evaluate_constraints(sample, response)

    assert result["format_ok"] is True
    assert result["char_range_pass_rate"] == 1.0
    assert result["glossary_pass_rate"] == 1.0
    assert result["constraint_score"] == 100.0


def test_run_benchmark_with_fake_caller_writes_summary(tmp_path: Path) -> None:
    samples_path = tmp_path / "samples.json"
    output_path = tmp_path / "run.json"
    samples_path.write_text(json.dumps(_sample_payload(), ensure_ascii=False), encoding="utf-8")

    payload = run_benchmark(
        BenchmarkConfig(
            samples_path=samples_path,
            output_path=output_path,
            models=("deepseek", "mimo_v25"),
            judge_model="gpt54",
            force=True,
        ),
        caller=FakeCaller(),
    )

    assert output_path.exists()
    assert output_path.with_suffix(".md").exists()
    assert payload["status"] == "completed"
    assert len(payload["results"]) == 2
    assert payload["summary"]["models"][0]["avg_quality_score"] == 90.0
    assert payload["summary"]["models"][0]["overall_score"] > 80

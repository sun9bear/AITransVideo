from __future__ import annotations

from scripts.benchmark.speaker_attribution_model_judge import (
    _parse_json_response,
    render_markdown,
)


def test_parse_json_response_handles_fenced_json() -> None:
    payload = _parse_json_response(
        """```json
{"decisions":[{"candidate_id":"cand_1","decision":"s2_speaker"}]}
```"""
    )

    assert payload["decisions"][0]["candidate_id"] == "cand_1"


def test_render_markdown_includes_decision_counts() -> None:
    text = render_markdown(
        {
            "generated_at": "now",
            "audit_batch": "batch.json",
            "review_model": "gemini_pro",
            "summary": {
                "candidates_loaded": 1,
                "decisions": 1,
                "decision_counts": {"s2_speaker": 1},
                "recommended_action_counts": {"keep": 1},
            },
            "decisions": [
                {
                    "candidate_id": "cand_1",
                    "decision": "s2_speaker",
                    "confidence": "high",
                    "recommended_action": "keep",
                    "reason": "audio matches",
                }
            ],
        }
    )

    assert "P2-b Speaker Attribution Model Judgement" in text
    assert "`s2_speaker`" in text

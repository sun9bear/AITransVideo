from __future__ import annotations

from pathlib import Path


def test_translation_form_credit_gate_reads_top_level_error_code() -> None:
    src = (
        Path(__file__).resolve().parents[1]
        / "frontend-next"
        / "src"
        / "components"
        / "workspace"
        / "TranslationForm.tsx"
    ).read_text(encoding="utf-8")
    helper = src.split("function isCreditGateError", 1)[1].split(
        "function validateYoutubeUrl", 1
    )[0]

    assert "topLevelCode" in helper
    assert "detail" in helper
    assert "error_code" in helper
    assert "CREDIT_GATE_ERROR_CODES.has" in helper

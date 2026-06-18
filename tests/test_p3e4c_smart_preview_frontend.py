from __future__ import annotations

import re
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SMART_PREVIEW_TS = (
    _REPO / "frontend-next" / "src" / "lib" / "api" / "smartPreviewClone.ts"
)
_TRANSLATION_FORM_TSX = (
    _REPO / "frontend-next" / "src" / "components" / "workspace" / "TranslationForm.tsx"
)
_CONFIRM_DIALOG_TSX = (
    _REPO
    / "frontend-next"
    / "src"
    / "components"
    / "workspace"
    / "SmartPreviewConfirmDialog.tsx"
)
_VOICE_SELECTION_TS = (
    _REPO / "frontend-next" / "src" / "lib" / "api" / "voiceSelection.ts"
)


def _src() -> str:
    return _SMART_PREVIEW_TS.read_text(encoding="utf-8")


def _exported_function_src(src: str, name: str) -> str:
    match = re.search(
        rf"export async function {name}\([\s\S]*?\n\}}",
        src,
    )
    assert match, f"{name} should be present"
    return match.group(0)


def test_smart_preview_youtube_url_forces_bounded_assemblyai_before_submit():
    """YouTube preview must not submit Gemini URL transcription unbounded."""
    fn = _exported_function_src(_src(), "createSmartPreviewJob")

    assert "input.sourceType ?? 'youtube_url'" in fn
    assert "body.transcription_method = 'assemblyai'" in fn
    assert fn.index("body.transcription_method = 'assemblyai'") < fn.index(
        "apiClient.post"
    )


def test_convert_preview_to_full_keeps_user_transcription_choice():
    """Preview-only URL guard must not rewrite full-length conversion requests."""
    fn = _exported_function_src(_src(), "convertPreviewToFull")

    assert "body.transcription_method = 'assemblyai'" not in fn
    assert "reuse_preview_job_id" in fn


def test_smart_preview_cost_is_not_frontend_hardcoded():
    """Preview clone cost must flow from Gateway pricing, not a TS constant."""
    api_src = _src()
    form_src = _TRANSLATION_FORM_TSX.read_text(encoding="utf-8")
    dialog_src = _CONFIRM_DIALOG_TSX.read_text(encoding="utf-8")
    voice_selection_src = _VOICE_SELECTION_TS.read_text(encoding="utf-8")

    assert "SMART_PREVIEW_CLONE_CREDITS" not in api_src
    assert "SMART_PREVIEW_CLONE_CREDITS" not in form_src
    assert "SMART_PREVIEW_CLONE_CREDITS" not in dialog_src
    assert "smart_preview_clone_cost_credits: number" in voice_selection_src
    assert "pricing.smart_preview_clone_cost_credits" in form_src
    assert "cloneCostCredits={smartPreviewCloneCostCredits}" in form_src
    assert "mapSmartPreviewCreateError(error, { cloneCostCredits })" in dialog_src

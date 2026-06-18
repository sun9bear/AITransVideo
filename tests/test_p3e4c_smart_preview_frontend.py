from __future__ import annotations

import re
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SMART_PREVIEW_TS = (
    _REPO / "frontend-next" / "src" / "lib" / "api" / "smartPreviewClone.ts"
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

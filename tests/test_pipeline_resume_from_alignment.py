"""Tests for ProcessPipeline.run(config) with resume_from='alignment'.

Commit copy_as_new / overwrite triggers this path (plan D28). The goal
is strict: the rebuilt pipeline must skip everything upstream of alignment
— no S0 download, no S1 transcription, no S2 review, no S3 translation,
no voice_selection_review gate, no paid TTS against all segments.

Because a real end-to-end run requires ffmpeg + a full pipeline stack
mocked out, this file uses two test styles:

1. **Runtime dispatch tests** — prove run() actually calls the new
   resume method (and only the new resume method) when resume_from is
   set. Cheap, doesn't need real context.
2. **Static invariant scans** — prove the new method's source cannot
   possibly call the forbidden upstream symbols (YouTubeDownloader,
   review_transcript, the voice_selection_review gate, etc.). These
   catch regressions where someone later "just adds one little review
   check" inside the resume path.

Runtime context-rebuild edge cases get a small handful of tests; the
full integration ("actually run alignment against a fake project_dir")
is covered by the Phase 2 ffmpeg-in-container smoke rather than here.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline.process import ProcessConfig, ProcessPipeline, ProcessResult


REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_SRC_PATH = REPO_ROOT / "src" / "pipeline" / "process.py"


# ===================================================================
# Runtime dispatch — run() branches to _run_alignment_and_publish_only
# ===================================================================


def test_run_with_resume_from_alignment_dispatches_to_resume_method(tmp_path: Path) -> None:
    """run(resume_from='alignment') must delegate to the resume method
    instead of entering the top-down flow. This is the pivot that makes
    copy_as_new actually start from alignment."""
    pipeline = ProcessPipeline()
    expected = ProcessResult(
        project_dir=str(tmp_path),
        dubbed_audio_path="", ambient_audio_path="", subtitles_path="",
        segments_dir="", alignment_report_path="", background_sounds_path="",
        total_segments=0, needs_review_count=0,
    )
    resume_mock = MagicMock(return_value=expected)
    pipeline._run_alignment_and_publish_only = resume_mock  # type: ignore[method-assign]
    # Stub top-down entry points so a missed branch would blow up loudly.
    pipeline._load_stage_config = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("run() entered the top-down flow — "
                                   "resume_from=alignment branch did not fire")
    )

    config = ProcessConfig(
        youtube_url="https://example.com/v",
        source_type="youtube_url",
        source_ref="https://example.com/v",
        project_dir=str(tmp_path),
        resume_from="alignment",
    )
    result = pipeline.run(config)

    assert result is expected
    resume_mock.assert_called_once_with(config)


def test_run_without_resume_from_does_not_dispatch_to_resume_method(tmp_path: Path) -> None:
    """resume_from=None must NOT enter the resume path."""
    pipeline = ProcessPipeline()
    resume_mock = MagicMock(
        side_effect=AssertionError("resume path fired for a non-resume run")
    )
    pipeline._run_alignment_and_publish_only = resume_mock  # type: ignore[method-assign]
    # Stub _load_stage_config to raise a sentinel; reaching it proves
    # run() entered the top-down branch (which is the whole point — we
    # don't actually want to run the pipeline here).
    pipeline._load_stage_config = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("reached top-down flow (expected)")
    )

    config = ProcessConfig(
        youtube_url="https://example.com/v",
        source_type="youtube_url",
        source_ref="https://example.com/v",
        resume_from=None,
    )
    with pytest.raises(RuntimeError, match="reached top-down flow"):
        pipeline.run(config)
    resume_mock.assert_not_called()


# ===================================================================
# Runtime fail-fast — resume method refuses obviously-bad inputs
# ===================================================================


def test_resume_fail_fast_when_project_dir_missing() -> None:
    """Resume with no project_dir at all is a programmer error: the
    commit wiring is supposed to always set it. Surface fast."""
    pipeline = ProcessPipeline()
    config = ProcessConfig(
        youtube_url="https://example.com/v",
        source_type="youtube_url", source_ref="https://example.com/v",
        project_dir=None, resume_from="alignment",
    )
    with pytest.raises(ValueError, match="requires ProcessConfig.project_dir"):
        pipeline._run_alignment_and_publish_only(config)


def test_resume_fail_fast_when_project_dir_does_not_exist(tmp_path: Path) -> None:
    pipeline = ProcessPipeline()
    config = ProcessConfig(
        youtube_url="",
        source_type="youtube_url", source_ref="x",
        project_dir=str(tmp_path / "nope"),
        resume_from="alignment",
    )
    with pytest.raises(ValueError, match="project_dir does not exist"):
        pipeline._run_alignment_and_publish_only(config)


def test_resume_fail_fast_when_translation_segments_missing(tmp_path: Path) -> None:
    """resume_from=alignment explicitly requires translation/segments.json.
    If it's absent, copy_service didn't place it — bail rather than
    falling through to a broken run."""
    (tmp_path / "project").mkdir()
    pipeline = ProcessPipeline()
    config = ProcessConfig(
        youtube_url="",
        source_type="youtube_url", source_ref="x",
        project_dir=str(tmp_path / "project"),
        resume_from="alignment",
    )
    with pytest.raises(ValueError, match="translation/segments.json"):
        pipeline._run_alignment_and_publish_only(config)


def test_resume_fail_fast_when_speech_audio_missing(tmp_path: Path) -> None:
    """alignment needs speech/ambient audio for its DSP. Missing either
    is a copy_service gap — bail."""
    project = tmp_path / "project"
    (project / "translation").mkdir(parents=True)
    (project / "translation" / "segments.json").write_text(
        json.dumps({"segments": [], "total_segments": 0, "output_path": ""}),
        encoding="utf-8",
    )
    pipeline = ProcessPipeline()
    config = ProcessConfig(
        youtube_url="",
        source_type="youtube_url", source_ref="x",
        project_dir=str(project), resume_from="alignment",
    )
    with pytest.raises(ValueError, match="audio/speech.wav missing"):
        pipeline._run_alignment_and_publish_only(config)


# ===================================================================
# Static invariant scans — resume method cannot call upstream symbols
#
# CodeX's 3 asks expressed as AST guards. The argument: if the symbol
# never appears inside _run_alignment_and_publish_only's body, there's
# no runtime path that can call it. This is stronger than a behavioral
# test because it refuses at authoring time, not just at test time.
# ===================================================================


def _extract_resume_method_source() -> str:
    """Return the source of ProcessPipeline._run_alignment_and_publish_only.
    Uses AST instead of regex because the method body is long and
    contains nested try/except / for / if that would trip naive slicing."""
    tree = ast.parse(PIPELINE_SRC_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_run_alignment_and_publish_only"
        ):
            return ast.unparse(node)
    raise AssertionError("_run_alignment_and_publish_only not found in process.py")


@pytest.mark.parametrize("forbidden,why", [
    ("YouTubeDownloader", "would re-download (S0 work + yt-dlp call)"),
    ("SourceAudioPreparationService", "would re-separate human voice (demucs, minutes of work)"),
    (".transcribe(", "transcriber call — re-runs ASR (paid AssemblyAI call)"),
    ("GeminiTranscriber", "Gemini multimodal transcription — paid LLM call"),
    ("review_transcript(", "S2 LLM review — paid Gemini call for speaker/text review"),
    ("VOICE_SELECTION_REVIEW_STAGE", "pausing at voice_selection_review re-opens stage 7"),
    ("_build_voice_selection_review_payload", "rebuilding the voice review payload "
                                               "re-enters the gate"),
    ("approved_translation_review", "translation_review gate — stage 6 regression"),
    (".translate(", "Gemini translation call — the stage 6 Gemini 403 we saw"),
])
def test_resume_method_does_not_reference_upstream_symbol(
    forbidden: str, why: str,
) -> None:
    """Each forbidden token names a pre-alignment pipeline collaborator.
    None of them should appear in the resume method body — resume's
    contract is "only alignment + publish, nothing upstream"."""
    body = _extract_resume_method_source()
    assert forbidden not in body, (
        f"_run_alignment_and_publish_only references {forbidden!r}: {why}. "
        "Resume must not touch pre-alignment stages."
    )


def test_resume_method_does_not_generate_tts_against_all_segments() -> None:
    """CodeX ask #3: if prior TTS wavs exist (copy_service hardlinks
    them), resume must NOT bulk-call TTSGenerator against the full
    segment list. The only allowed TTS call path is
    tts_generator.generate_all(segments_needing_tts, ...) where
    segments_needing_tts is the output of _hydrate_cached_tts_segments
    (empty list when everything is cached).

    Static guard: the resume method's ``generate_all`` call site must
    be against ``segments_needing_tts``, not against
    ``translation_result.segments`` — i.e. never the full set."""
    body = _extract_resume_method_source()
    # Find every generate_all call-site argument. The regex picks up
    # the first positional arg (a Python expression up to the next comma
    # at top-level paren depth — good enough for our formatted source).
    matches = re.findall(r"generate_all\(\s*([^,)]+?)\s*,", body)
    assert matches, (
        "resume method does not reference generate_all — if this is "
        "intentional (e.g. future refactor moves TTS out), delete the "
        "guard. Otherwise alignment cannot have rebuilt audio."
    )
    for arg in matches:
        assert "translation_result.segments" not in arg, (
            f"generate_all called with full segment list ({arg!r}); "
            "resume path must only regen cached-miss segments"
        )


def test_resume_method_does_not_enter_review_gate_stage_names() -> None:
    """Resume must not call set_stage on any upstream stage name —
    it only writes alignment + legacy_process_output. Catches sneak-in
    of e.g. 'voice_selection_review'/'translation_review' stage writes."""
    body = _extract_resume_method_source()
    forbidden_stages = (
        "speaker_review",
        "translation_review",
        "voice_selection_review",
        "voice_selection",
        "translation_config_review",
        "voice_review",
        "media_understanding",
        "audio_preparation",
    )
    # scan for string literals
    for stage_name in forbidden_stages:
        # Allow stage name to appear in comments / docstrings that
        # explain what we skip; only flag if inside a quoted string
        # assigned to current_stage_name or passed to set_stage.
        pattern = rf"(current_stage_name\s*=\s*['\"]{re.escape(stage_name)}['\"]|set_stage\(\s*['\"]{re.escape(stage_name)}['\"])"
        assert not re.search(pattern, body), (
            f"resume method assigns to pre-alignment stage {stage_name!r}; "
            "allowed stages are alignment + legacy_process_output only"
        )

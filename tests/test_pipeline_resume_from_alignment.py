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
    is a copy_service gap — bail. Use canonical demucs filename
    (speech_for_asr.wav), matching services.audio.separator.speech_filename."""
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
    with pytest.raises(ValueError, match="audio/speech_for_asr.wav missing"):
        pipeline._run_alignment_and_publish_only(config)


def test_gamma_fails_fast_when_editor_tts_wav_missing(tmp_path: Path) -> None:
    """γ hard guard: if any segment lacks editor/tts_segments/{sid}.wav,
    raise a specific ValueError with the sid list. The contract says
    commit must have placed every segment's wav on disk — if not, the
    user will see a dubbed video with silence gaps. Better to fail loud."""
    project = tmp_path / "project"
    (project / "translation").mkdir(parents=True)
    (project / "audio").mkdir(parents=True)
    (project / "editor").mkdir(parents=True)
    # Provide translation/segments.json with two segments
    (project / "translation" / "segments.json").write_text(
        json.dumps({
            "segments": [
                {
                    "segment_id": 1, "speaker_id": "speaker_a",
                    "display_name": "A", "voice_id": "", "start_ms": 0,
                    "end_ms": 1000, "target_duration_ms": 1000,
                    "source_text": "hi", "cn_text": "\u4f60\u597d",
                },
                {
                    "segment_id": 2, "speaker_id": "speaker_a",
                    "display_name": "A", "voice_id": "", "start_ms": 1000,
                    "end_ms": 2000, "target_duration_ms": 1000,
                    "source_text": "bye", "cn_text": "\u518d\u89c1",
                },
            ],
            "total_segments": 2,
            "output_path": "",
        }),
        encoding="utf-8",
    )
    # editor/segments.json (authoritative post-commit)
    (project / "editor" / "segments.json").write_text(
        json.dumps([
            {
                "segment_id": "1", "speaker_id": "speaker_a",
                "display_name": "A", "voice_id": "", "start_ms": 0,
                "end_ms": 1000, "target_duration_ms": 1000,
                "source_text": "hi", "cn_text": "\u4f60\u597d",
            },
            {
                "segment_id": "2", "speaker_id": "speaker_a",
                "display_name": "A", "voice_id": "", "start_ms": 1000,
                "end_ms": 2000, "target_duration_ms": 1000,
                "source_text": "bye", "cn_text": "\u518d\u89c1",
            },
        ]),
        encoding="utf-8",
    )
    # Audio precondition files
    (project / "audio" / "speech_for_asr.wav").write_bytes(b"stub")
    (project / "audio" / "ambient.wav").write_bytes(b"stub")
    # Deliberately NO editor/tts_segments/*.wav files

    pipeline = ProcessPipeline()
    config = ProcessConfig(
        youtube_url="",
        source_type="youtube_url", source_ref="x",
        project_dir=str(project), resume_from="alignment",
    )
    with pytest.raises(ValueError, match="editor/tts_segments"):
        pipeline._run_alignment_and_publish_only(config)


def test_gamma_prefers_editor_segments_over_translation_segments(tmp_path: Path) -> None:
    """γ invariant: editor/segments.json is authoritative post-commit
    (applies user text edits + voice_map overrides). translation/ is
    stale (copied verbatim with path rewriting, no edit overlay).

    Pure loader-level assertion: the helper used by γ to read segments
    must return the editor/ content when both files exist, so user edits
    are not silently dropped. Uses a fake project dir — doesn't need to
    actually run publish."""
    project = tmp_path / "project"
    (project / "translation").mkdir(parents=True)
    (project / "editor").mkdir(parents=True)

    # translation/segments.json: OLD text (pre-edit)
    (project / "translation" / "segments.json").write_text(
        json.dumps({
            "segments": [{
                "segment_id": 1, "speaker_id": "speaker_a",
                "display_name": "A", "voice_id": "", "start_ms": 0,
                "end_ms": 1000, "target_duration_ms": 1000,
                "source_text": "hello", "cn_text": "\u4f60\u597d",
            }],
            "total_segments": 1, "output_path": "",
        }),
        encoding="utf-8",
    )
    # editor/segments.json: NEW text (user's edit)
    (project / "editor" / "segments.json").write_text(
        json.dumps([{
            "segment_id": "1", "speaker_id": "speaker_a",
            "display_name": "A", "voice_id": "", "start_ms": 0,
            "end_ms": 1000, "target_duration_ms": 1000,
            "source_text": "hello", "cn_text": "\u4f60\u597d\u5440",
        }]),
        encoding="utf-8",
    )

    pipeline = ProcessPipeline()
    segments = pipeline._load_segments_for_publish_resume(
        editor_segments_path=(project / "editor" / "segments.json"),
        translation_segments_path=(project / "translation" / "segments.json"),
    )
    assert len(segments) == 1
    # User edit MUST be what γ publishes, not the stale translation/ copy.
    assert segments[0].cn_text == "\u4f60\u597d\u5440", (
        "\u03b3 loader must prefer editor/segments.json; got stale translation/ text"
    )
    # segment_id must be coerced to int (DubbingSegment contract), even
    # though editor/segments.json stores it as str.
    assert segments[0].segment_id == 1
    assert isinstance(segments[0].segment_id, int)


def test_gamma_loader_drops_unknown_voice_map_fields(tmp_path: Path) -> None:
    """editing_commit.apply_voice_map adds 'provider' to segment dicts
    (not a DubbingSegment field). γ loader must drop unknown keys, else
    DubbingSegment(**seg) raises TypeError about unexpected kwargs."""
    project = tmp_path / "project"
    (project / "editor").mkdir(parents=True)
    (project / "editor" / "segments.json").write_text(
        json.dumps([{
            "segment_id": "3", "speaker_id": "speaker_a",
            "display_name": "A", "voice_id": "voice123",
            "start_ms": 0, "end_ms": 1000, "target_duration_ms": 1000,
            "source_text": "x", "cn_text": "x",
            # voice_map override adds 'provider' (maps to tts_provider
            # but stored as 'provider' by _apply_voice_map):
            "provider": "cosyvoice",
            # and some other junk — a legacy task or a future schema drift
            "legacy_field_we_dont_care_about": 42,
        }]),
        encoding="utf-8",
    )
    pipeline = ProcessPipeline()
    segments = pipeline._load_segments_for_publish_resume(
        editor_segments_path=(project / "editor" / "segments.json"),
        translation_segments_path=(project / "editor" / "__no_translation__"),
    )
    assert len(segments) == 1
    assert segments[0].voice_id == "voice123"


def test_media_artifact_filenames_match_pipeline_output() -> None:
    """Regression guard for the 2026-04-19 filename drift bug: resume
    checks used ``audio/speech.wav`` while demucs actually produces
    ``audio/speech_for_asr.wav`` (see services.audio.separator.speech_filename).
    The hardlink list + the resume-path pre-check must reference the
    same canonical name, otherwise copy silently skips the file AND
    resume fail-fasts after."""
    from services.audio.separator import AudioStemSeparator
    from services.jobs.copy_service import _MEDIA_HARDLINK_RELS

    speech_name = AudioStemSeparator.speech_filename
    assert f"audio/{speech_name}" in _MEDIA_HARDLINK_RELS, (
        f"copy_service._MEDIA_HARDLINK_RELS does not include "
        f"'audio/{speech_name}' — hardlink will skip the demucs output "
        "and resume-from-alignment will fail-fast at the pre-check"
    )
    # And the resume method's source must reference the same filename.
    body = _extract_resume_method_source()
    assert speech_name in body, (
        f"resume method references an outdated speech filename; must use "
        f"canonical {speech_name!r}"
    )


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


# ===================================================================
# γ (publish-only) guards — 2026-04-19
#
# User contract: commit's resume path runs ONLY publish (audio mux +
# video mux + poster + 3 subtitles). No Gemini rewrite, no TTS
# (re-)synthesis, no alignment DSP. The user reviewed each segment's
# duration in the editing UI and decided whether to re-TTS it before
# commit; editor/tts_segments/{sid}.wav is the authoritative audio.
#
# Plan reference: docs/plans/2026-04-18-studio-post-edit-plan.md (γ).
# ===================================================================


@pytest.mark.parametrize("forbidden,why", [
    ("GeminiRewriter", "γ must not rewrite text via Gemini"),
    ("SegmentAligner", "γ must not run alignment (it orchestrates Gemini rewrite + TTS retry)"),
    ("TTSGenerator", "γ must not (re-)synthesise TTS — user already reviewed per-segment duration"),
    (".align_all(", "γ must not invoke alignment orchestrator"),
    (".generate_all(", "γ must not generate TTS — editor/tts_segments/ wavs are authoritative"),
    ("_pre_rewrite_obvious_overshoot_segments_before_tts", "γ must not pre-rewrite (Gemini call)"),
    ("_presplit_long_overshoot_segments_before_alignment", "γ must not split segments (would invalidate existing wavs)"),
    ("_repair_failed_long_segments", "γ must not repair — that's the user's job in the editing UI"),
    ("_hydrate_cached_tts_segments", "γ reads editor/tts_segments/ directly, not the tts/ cache hydrator"),
    ("_calibrate_tts_duration", "γ takes wav duration as ground truth — no chars/sec recalibration"),
    ("PostTTSBudgetTracker", "γ does no TTS, no post-TTS budget tracking"),
    ("pre_tts_rewriter", "γ does no pre-TTS rewriting"),
])
def test_gamma_resume_method_forbids_upstream_tts_and_alignment(
    forbidden: str, why: str,
) -> None:
    """γ resume path = publish only. Any appearance of a TTS/alignment
    symbol in the method body is a regression: it would mean Gemini or
    TTS gets called despite the user's explicit 'only mux' instruction."""
    body = _extract_resume_method_source()
    assert forbidden not in body, (
        f"_run_alignment_and_publish_only references {forbidden!r}: {why}. "
        "γ contract: publish only — editor/tts_segments/{sid}.wav is authoritative."
    )


def test_gamma_resume_method_consumes_editor_tts_segments() -> None:
    """γ positive assertion: the method must read from
    ``editor/tts_segments/`` and assign to segment.aligned_audio_path
    so publish picks up the user-reviewed wavs.

    If a refactor moves this assignment elsewhere, update the guard —
    but it must still be present in the γ method somewhere."""
    body = _extract_resume_method_source()
    assert "editor" in body and "tts_segments" in body, (
        "γ resume method must reference editor/tts_segments/ directory"
    )
    assert re.search(r"aligned_audio_path\s*=", body), (
        "γ resume method must assign to segment.aligned_audio_path "
        "(otherwise publish falls back to tts_audio_path, which γ may "
        "not populate correctly)"
    )


def test_gamma_resume_method_dispatches_publish_output_bundle() -> None:
    """γ invariant: the method must actually dispatch the publish output
    (editor package + publish muxing). Without this, the 4 artifacts
    the user cares about (dubbed audio, dubbed video, poster, subtitles)
    would not be produced."""
    body = _extract_resume_method_source()
    assert "_dispatch_process_output_bundle" in body, (
        "γ resume method must call _dispatch_process_output_bundle — "
        "otherwise no publish muxing happens and the user sees nothing"
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

"""Phase 1 acceptance tests for Jianying draft delivery (Task J8).

Six scenarios that close out the phase 1 backend PoC:

1. test_phase1_full_e2e_with_pyjianyingdraft
   Full dispatcher run with all gates passing, real pyJianYingDraft, real WAV + SRT.
   Asserts zip + manifest + artifact_index all correct.

2. test_phase1_clean_env_skip_no_engine
   Simulates missing pyJianYingDraft via monkeypatch import hook.
   Dispatcher gracefully degrades to skipped_no_engine; other artifacts still produced.

3. test_phase1_subtitle_consistency_draft_vs_cues_json
   Cues in subtitle_cues.json (phase 1a canonical) match text + timing in the generated
   draft_content.json exactly. Proves phase 1a -> SRT -> pyJianYingDraft -> draft pipeline
   preserves data.

4. test_phase1_pytest_runs_clean_on_no_pyjianyingdraft_install
   Regression guard: production modules do not trigger pyJianYingDraft import on module
   load.

5. test_phase1_express_mode_skipped_silently
   service_mode="express" gate fails closed; no compat report written.

6. test_phase1_failed_quality_report_skips_jianying
   Cue v2 quality gate fails closed when validation_status="failed".

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md (J8 / phase 1 closure)
"""

from __future__ import annotations

import ast
import builtins
import json
import struct
import sys
import wave
import zipfile
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest

from core.artifact_index import ArtifactIndex
from core.enums import OutputTarget
from core.models import SemanticBlock, SubtitleLine
from core.project_model import LocalizedProject
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.output_dispatcher import OutputDispatcher
from modules.output.output_models import OutputRequest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_wav(path: str, duration_s: float = 3.0) -> None:
    """Write a minimal PCM WAV file at *path* using stdlib only (no ffmpeg)."""
    sample_rate = 44100
    n_samples = int(sample_rate * duration_s)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n_samples):
            value = 8000 if (i // 50) % 2 == 0 else -8000
            wf.writeframes(struct.pack("<h", value))


def _make_srt(path: str) -> None:
    """Write a minimal 2-cue SRT file at *path*."""
    content = (
        "1\n"
        "00:00:00,000 --> 00:00:01,500\n"
        "今天我们来看这个。\n"
        "\n"
        "2\n"
        "00:00:01,500 --> 00:00:03,000\n"
        "这是第二段落。\n"
    )
    Path(path).write_text(content, encoding="utf-8")


def _make_fake_editor_result(tmp_path: Path) -> ProjectOutputResult:
    """Minimal ProjectOutputResult — all paths point to dummy files."""
    out = tmp_path / "output"
    return ProjectOutputResult(
        dubbed_audio_path=str(out / "dubbed_audio_complete.wav"),
        ambient_audio_path=str(out / "ambient_audio.wav"),
        segments_dir=str(out / "segments"),
        segment_count=2,
        subtitles_path=str(out / "subtitles.srt"),
        subtitles_en_path=str(out / "subtitles_en.srt"),
        subtitles_bilingual_path=str(out / "subtitles_bilingual.srt"),
        background_sounds_path=str(out / "background_sounds.txt"),
        alignment_report_path=str(out / "alignment_report.md"),
        needs_review_count=0,
    )


class _FakeEditorBackend:
    """Fake editor backend that creates the output directory and returns a fixed result.

    The output dir must exist so OutputDispatcher can write subtitle JSON files there.
    The returned dubbed_audio_complete and subtitles paths are registered by the
    dispatcher into artifact_index; they must survive downstream gate checks.
    """

    def __init__(self, tmp_path: Path, *, pre_create_subtitles: bool = False) -> None:
        self._tmp_path = tmp_path
        self._pre_create_subtitles = pre_create_subtitles

    def write(self, output) -> ProjectOutputResult:
        out = Path(output.output_dir) / "output"
        out.mkdir(parents=True, exist_ok=True)
        result = _make_fake_editor_result(self._tmp_path)
        # Create the real dubbed_audio + subtitles files so downstream gates can read them.
        dubbed = Path(result.dubbed_audio_path)
        dubbed.parent.mkdir(parents=True, exist_ok=True)
        if not dubbed.exists():
            _make_wav(str(dubbed))
        srt_path = Path(result.subtitles_path)
        if not srt_path.exists():
            _make_srt(str(srt_path))
        return result


class _FakeManifestWriter:
    """Stub manifest writer that always succeeds and returns a predictable path."""

    def write(self, *, project_root, localized_project, artifact_index, request, output_bundle):
        manifest_path = str(Path(str(project_root)) / "manifest.json")
        # Write a minimal valid manifest so tests that read it can parse it.
        payload = {
            "manifest_version": "aivideotrans_output_manifest_v1",
            "project_id": localized_project.project_id,
            "primary_outputs": {
                "editor": {
                    "dubbed_audio_path": getattr(output_bundle.editor_result, "dubbed_audio_path", None),
                    "jianying_draft_zip": artifact_index.get("editor.jianying_draft_zip"),
                    "jianying_draft_dir": artifact_index.get("editor.jianying_draft_dir"),
                    "jianying_compatibility_report": artifact_index.get("editor.jianying_compatibility_report"),
                }
                if output_bundle.editor_result is not None
                else None,
                "publish": None,
            },
            "artifact_index": artifact_index.to_dict(),
        }
        Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(manifest_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path


def _build_localized_project(tmp_path: Path, *, project_id: str = "j8_acceptance_test") -> LocalizedProject:
    """Build a minimal LocalizedProject with 2 semantic blocks matching the SRT cues."""
    aligned_audio = tmp_path / "aligned.wav"
    if not aligned_audio.exists():
        _make_wav(str(aligned_audio))

    captions = [
        SubtitleLine(
            index=0,
            start_ms=0,
            end_ms=1500,
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            en_text="Today we look at this.",
            cn_text="今天我们来看这个。",
        ),
        SubtitleLine(
            index=1,
            start_ms=1500,
            end_ms=3000,
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            en_text="This is the second paragraph.",
            cn_text="这是第二段落。",
        ),
    ]
    semantic_blocks = [
        SemanticBlock(
            block_id="block_0001",
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            original_srt_indices=[0],
            first_start_ms=0,
            last_end_ms=1500,
            target_duration_ms=1500,
            merged_cn_text="今天我们来看这个。",
            actual_audio_duration_ms=1500,
            aligned_audio_path=str(aligned_audio),
            status="align_done",
        ),
        SemanticBlock(
            block_id="block_0002",
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            original_srt_indices=[1],
            first_start_ms=1500,
            last_end_ms=3000,
            target_duration_ms=1500,
            merged_cn_text="这是第二段落。",
            actual_audio_duration_ms=1500,
            aligned_audio_path=str(aligned_audio),
            status="align_done",
        ),
    ]
    return LocalizedProject(
        project_id=project_id,
        source_info={
            "source_kind": "local_video",
            "source_path": str(tmp_path / "source.mp4"),
        },
        artifacts=ArtifactIndex(),
        stage_snapshot={},
        semantic_blocks=semantic_blocks,
        aligned_blocks=semantic_blocks,
        captions=captions,
    )


def _pre_populate_subtitle_artifacts(artifact_index: ArtifactIndex, tmp_path: Path) -> None:
    """Register a real dubbed_audio + subtitles SRT so jianying gate checks pass."""
    dubbed = tmp_path / "dubbed_audio_complete.wav"
    if not dubbed.exists():
        _make_wav(str(dubbed))
    artifact_index.register("editor.dubbed_audio_complete", str(dubbed))

    srt = tmp_path / "subtitles.srt"
    if not srt.exists():
        _make_srt(str(srt))
    artifact_index.register("editor.subtitles", str(srt))


# ---------------------------------------------------------------------------
# Scenario 1: Full e2e with real pyJianYingDraft
# ---------------------------------------------------------------------------

# Guard: skip if not installed (must be at function level so other tests still run)
def test_phase1_full_e2e_with_pyjianyingdraft(tmp_path: Path) -> None:
    """Full dispatcher run with all gates passing and real pyJianYingDraft.

    Asserts:
    - OutputBundleResult returned.
    - artifact_index has all 3 jianying keys populated.
    - Zip file exists and is > 1 KB.
    - Zip contains draft_content.json + draft_meta_info.json + materials/dubbed_audio.wav.
    - manifest.json primary_outputs.editor has jianying fields populated.
    """
    pytest.importorskip("pyJianYingDraft")

    project = _build_localized_project(tmp_path)
    artifact_index = ArtifactIndex()

    dispatcher = OutputDispatcher(
        editor_backend=_FakeEditorBackend(tmp_path),
        manifest_writer=_FakeManifestWriter(),
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=True,
        service_mode="studio",
    )

    result = dispatcher.dispatch(project, artifact_index, request)

    # --- OutputBundleResult returned ---
    assert result is not None, "dispatch should return an OutputBundleResult"

    # --- All 3 jianying artifact keys registered ---
    zip_path_str = artifact_index.get("editor.jianying_draft_zip")
    draft_dir_str = artifact_index.get("editor.jianying_draft_dir")
    compat_report_str = artifact_index.get("editor.jianying_compatibility_report")

    assert zip_path_str, "editor.jianying_draft_zip should be registered"
    assert draft_dir_str, "editor.jianying_draft_dir should be registered"
    assert compat_report_str, "editor.jianying_compatibility_report should be registered"

    # --- Zip file exists and is > 1 KB ---
    zip_path = Path(zip_path_str)
    assert zip_path.exists(), f"jianying zip not found: {zip_path}"
    assert zip_path.stat().st_size > 1024, (
        f"jianying zip too small: {zip_path.stat().st_size} bytes"
    )

    # --- Draft dir exists ---
    draft_dir = Path(draft_dir_str)
    assert draft_dir.is_dir(), f"jianying draft_dir not found: {draft_dir}"

    # --- Compatibility report exists ---
    compat_report = Path(compat_report_str)
    assert compat_report.exists(), f"jianying compatibility report not found: {compat_report}"

    # --- Zip contains required files ---
    with zipfile.ZipFile(str(zip_path)) as zf:
        names = set(zf.namelist())
    assert "draft_content.json" in names, (
        f"draft_content.json missing from zip; found: {sorted(names)}"
    )
    assert "draft_meta_info.json" in names, (
        f"draft_meta_info.json missing from zip; found: {sorted(names)}"
    )
    assert "materials/dubbed_audio.wav" in names, (
        f"materials/dubbed_audio.wav missing from zip; found: {sorted(names)}"
    )

    # --- Manifest has jianying fields populated ---
    manifest_path_str = artifact_index.get("manifest.root")
    assert manifest_path_str, "manifest.root should be registered"
    manifest = json.loads(Path(manifest_path_str).read_text(encoding="utf-8"))
    editor_out = manifest.get("primary_outputs", {}).get("editor", {}) or {}
    assert editor_out.get("jianying_draft_zip") == zip_path_str, (
        "manifest primary_outputs.editor.jianying_draft_zip mismatch"
    )
    assert editor_out.get("jianying_draft_dir") == draft_dir_str, (
        "manifest primary_outputs.editor.jianying_draft_dir mismatch"
    )
    assert editor_out.get("jianying_compatibility_report") == compat_report_str, (
        "manifest primary_outputs.editor.jianying_compatibility_report mismatch"
    )


# ---------------------------------------------------------------------------
# Scenario 2: Clean env — simulated missing pyJianYingDraft via monkeypatch
# ---------------------------------------------------------------------------


def test_phase1_clean_env_skip_no_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher with monkeypatched missing pyJianYingDraft gracefully degrades.

    Asserts:
    - dispatch() completes without raising.
    - editor.jianying_compatibility_report registered (backend still writes skip report).
    - editor.jianying_draft_zip is empty/None (no zip produced).
    - compatibility_report JSON validation_status == "skipped_no_engine".
    - Editor artifacts still produced (no regression).
    """
    # Monkey-patch builtins.__import__ to simulate absent pyJianYingDraft.
    # Reload the writer module so the lazy-import path is exercised fresh.
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "pyJianYingDraft" or name.startswith("pyJianYingDraft."):
            raise ImportError("simulated clean env — pyJianYingDraft absent")
        return real_import(name, *args, **kwargs)

    # Remove cached writer module so the lazy import is triggered inside dispatch
    writer_mod_name = "modules.output.jianying.jianying_draft_writer"
    saved_writer = sys.modules.pop(writer_mod_name, None)

    try:
        monkeypatch.setattr(builtins, "__import__", _fake_import)

        project = _build_localized_project(tmp_path)
        artifact_index = ArtifactIndex()

        dispatcher = OutputDispatcher(
            editor_backend=_FakeEditorBackend(tmp_path),
            manifest_writer=_FakeManifestWriter(),
        )
        request = OutputRequest(
            targets=[OutputTarget.EDITOR],
            output_dir=str(tmp_path),
            include_jianying_draft=True,
            service_mode="studio",
        )

        # Should not raise
        result = dispatcher.dispatch(project, artifact_index, request)

    finally:
        # Restore writer module to avoid leaking state into subsequent tests
        if saved_writer is not None:
            sys.modules[writer_mod_name] = saved_writer
        elif writer_mod_name in sys.modules:
            del sys.modules[writer_mod_name]

    assert result is not None, "dispatch should return a result even on skip path"

    # compatibility_report still registered (backend writes skip report regardless)
    compat_path_str = artifact_index.get("editor.jianying_compatibility_report")
    assert compat_path_str, (
        "editor.jianying_compatibility_report should be registered even on skip path"
    )

    # No zip produced
    zip_path_str = artifact_index.get("editor.jianying_draft_zip")
    assert not zip_path_str, (
        f"editor.jianying_draft_zip should not be registered on skip path; got: {zip_path_str!r}"
    )

    # Compatibility report contains skipped_no_engine
    compat_report = json.loads(Path(compat_path_str).read_text(encoding="utf-8"))
    assert compat_report.get("validation_status") == "skipped_no_engine", (
        f"expected validation_status='skipped_no_engine', got: {compat_report.get('validation_status')!r}"
    )

    # Editor artifacts still produced (no regression)
    assert artifact_index.get("editor.dubbed_audio_complete"), (
        "editor.dubbed_audio_complete should still be registered"
    )
    assert artifact_index.get("editor.subtitles"), (
        "editor.subtitles should still be registered"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Subtitle consistency — draft_content.json matches subtitle_cues.json
# ---------------------------------------------------------------------------


def test_phase1_subtitle_consistency_draft_vs_cues_json(tmp_path: Path) -> None:
    """Cues in subtitle_cues.json (phase 1a canonical) match text + timing in draft_content.json.

    Proves: phase 1a cue v2 -> SRT -> pyJianYingDraft -> draft pipeline preserves data.

    Checks for each cue in subtitle_cues.json:
    - cue.text matches the corresponding text segment in draft_content.json (after normalize).
    - cue.start_ms * 1000 == segment.target_timerange.start (microseconds).
    - (cue.start_ms + duration) * 1000 == segment.target_timerange.start + duration_us.
    """
    pytest.importorskip("pyJianYingDraft")

    project = _build_localized_project(tmp_path)
    artifact_index = ArtifactIndex()

    dispatcher = OutputDispatcher(
        editor_backend=_FakeEditorBackend(tmp_path),
        manifest_writer=_FakeManifestWriter(),
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=True,
        service_mode="studio",
    )
    dispatcher.dispatch(project, artifact_index, request)

    # --- Read subtitle_cues.json (phase 1a canonical output) ---
    cues_path_str = artifact_index.get("editor.subtitle_cues")
    assert cues_path_str, "editor.subtitle_cues should be registered after dispatch"
    cues_data = json.loads(Path(cues_path_str).read_text(encoding="utf-8"))
    cues = cues_data["cues"]
    assert len(cues) >= 1, "subtitle_cues.json should have at least 1 cue"

    # --- Read draft_content.json from the zip ---
    zip_path_str = artifact_index.get("editor.jianying_draft_zip")
    assert zip_path_str, "jianying_draft_zip should be registered"
    zip_path = Path(zip_path_str)
    assert zip_path.exists()

    with zipfile.ZipFile(str(zip_path)) as zf:
        draft_content_raw = zf.read("draft_content.json").decode("utf-8")
    draft_content = json.loads(draft_content_raw)

    # --- Extract text materials: build id -> text lookup ---
    text_materials = draft_content.get("materials", {}).get("texts", [])
    text_by_id: dict[str, str] = {}
    for mat in text_materials:
        mat_id = mat.get("id", "")
        content_json_str = mat.get("content", "{}")
        try:
            content_obj = json.loads(content_json_str)
            text_by_id[mat_id] = content_obj.get("text", "")
        except (json.JSONDecodeError, ValueError):
            pass

    # --- Extract text track segments (timing + material_id) ---
    text_segments: list[dict] = []
    for track in draft_content.get("tracks", []):
        if track.get("type") == "text":
            text_segments.extend(track.get("segments", []))

    assert len(text_segments) >= len(cues), (
        f"draft has {len(text_segments)} text segments but cues.json has {len(cues)} cues; "
        "expected at least as many segments as cues"
    )

    # Sort both by start_ms / start_us for alignment
    cues_sorted = sorted(cues, key=lambda c: c["start_ms"])
    segs_sorted = sorted(text_segments, key=lambda s: s.get("target_timerange", {}).get("start", 0))

    # --- Check each cue against the corresponding segment ---
    from modules.subtitles.cue_models import normalize  # phase 1a normalize function

    for idx, cue in enumerate(cues_sorted):
        seg = segs_sorted[idx]
        tr = seg.get("target_timerange", {})
        seg_start_us = tr.get("start", -1)
        seg_duration_us = tr.get("duration", -1)

        # Timing check: start_ms * 1000 must equal seg_start_us
        expected_start_us = cue["start_ms"] * 1000
        assert seg_start_us == expected_start_us, (
            f"Cue {cue['cue_id']} start mismatch: "
            f"expected {expected_start_us} us (start_ms={cue['start_ms']}), "
            f"got segment start {seg_start_us} us"
        )

        # End timing: (end_ms - start_ms) * 1000 == segment duration_us
        expected_duration_us = (cue["end_ms"] - cue["start_ms"]) * 1000
        assert seg_duration_us == expected_duration_us, (
            f"Cue {cue['cue_id']} duration mismatch: "
            f"expected {expected_duration_us} us (duration={(cue['end_ms'] - cue['start_ms'])}ms), "
            f"got segment duration {seg_duration_us} us"
        )

        # Text check: normalize(cue.text) == normalize(draft_text)
        mat_id = seg.get("material_id", "")
        draft_text = text_by_id.get(mat_id, "")
        assert normalize(cue["text"]) == normalize(draft_text), (
            f"Cue {cue['cue_id']} text mismatch: "
            f"cue.text={cue['text']!r}, draft_text={draft_text!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 4: Module-load guard — no pyJianYingDraft import on module load
# ---------------------------------------------------------------------------


def test_phase1_pytest_runs_clean_on_no_pyjianyingdraft_install() -> None:
    """Production modules must not trigger pyJianYingDraft import on module load.

    Verifies by AST-scanning all jianying-related production modules for
    top-level (non-guarded) 'import pyJianYingDraft' statements.

    A top-level import would break clean-env tests by failing at collection time.
    """
    modules_to_check = [
        "src/modules/output/jianying/jianying_draft_writer.py",
        "src/modules/output/jianying/jianying_draft_backend.py",
        "src/modules/output/jianying/jianying_draft_validator.py",
        "src/modules/output/jianying/jianying_draft_models.py",
        "src/modules/output/output_dispatcher.py",
    ]

    # Walk AST looking for module-level 'import pyJianYingDraft' or
    # 'from pyJianYingDraft import ...' that are NOT inside a function/class.
    def _has_top_level_pyjianying_import(source: str) -> bool:
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            # Only check top-level statements (direct children of Module)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "pyJianYingDraft" or alias.name.startswith("pyJianYingDraft."):
                            return True
                elif isinstance(node, ast.ImportFrom):
                    if node.module and (
                        node.module == "pyJianYingDraft"
                        or node.module.startswith("pyJianYingDraft.")
                    ):
                        return True
        return False

    repo_root = Path(__file__).resolve().parents[1]
    for rel_path in modules_to_check:
        abs_path = repo_root / rel_path
        assert abs_path.exists(), f"Expected module not found: {abs_path}"
        source = abs_path.read_text(encoding="utf-8")
        has_bad_import = _has_top_level_pyjianying_import(source)
        assert not has_bad_import, (
            f"{rel_path} has a top-level 'import pyJianYingDraft' — "
            "this will break clean-env test collection. Use lazy import inside functions."
        )

    # Also verify: importing the writer module with pyJianYingDraft absent does not raise.
    real_import = builtins.__import__

    def _no_pjy(name, *args, **kwargs):
        if name == "pyJianYingDraft" or name.startswith("pyJianYingDraft."):
            raise ImportError("simulated absent pyJianYingDraft")
        return real_import(name, *args, **kwargs)

    writer_mod = "modules.output.jianying.jianying_draft_writer"
    saved = sys.modules.pop(writer_mod, None)
    original_import = builtins.__import__
    try:
        builtins.__import__ = _no_pjy
        import importlib
        mod = importlib.import_module(writer_mod)
        assert hasattr(mod, "JianyingDraftWriter"), "JianyingDraftWriter missing from writer module"
        assert hasattr(mod, "JianyingEngineUnavailable"), "JianyingEngineUnavailable missing"
    finally:
        builtins.__import__ = original_import
        if saved is not None:
            sys.modules[writer_mod] = saved
        elif writer_mod in sys.modules:
            del sys.modules[writer_mod]


# ---------------------------------------------------------------------------
# Scenario 5: express mode gate fails closed, no compat report
# ---------------------------------------------------------------------------


def test_phase1_express_mode_skipped_silently(tmp_path: Path) -> None:
    """service_mode='express' gate fails closed — no jianying artifacts registered.

    The Gate 1 (service_mode check) fails before any backend call, so even
    the compatibility_report is NOT written (no backend invocation at all).
    """
    fake_jianying_backend = MagicMock()
    fake_jianying_backend.write.side_effect = AssertionError("backend must not be called")

    project = _build_localized_project(tmp_path)
    artifact_index = ArtifactIndex()

    dispatcher = OutputDispatcher(
        editor_backend=_FakeEditorBackend(tmp_path),
        manifest_writer=_FakeManifestWriter(),
        jianying_backend=fake_jianying_backend,
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=True,
        service_mode="express",
    )
    dispatcher.dispatch(project, artifact_index, request)  # must not raise

    fake_jianying_backend.write.assert_not_called()

    assert artifact_index.get("editor.jianying_draft_zip") is None, (
        "express mode: jianying_draft_zip should not be registered"
    )
    assert artifact_index.get("editor.jianying_draft_dir") is None, (
        "express mode: jianying_draft_dir should not be registered"
    )
    assert artifact_index.get("editor.jianying_compatibility_report") is None, (
        "express mode: jianying_compatibility_report should not be registered "
        "(gate closes before backend is called)"
    )

    # Other editor artifacts still produced (no regression)
    assert artifact_index.get("editor.dubbed_audio_complete"), (
        "editor.dubbed_audio_complete should still be registered in express mode"
    )


# ---------------------------------------------------------------------------
# Scenario 6: Failed quality report closes the jianying gate
# ---------------------------------------------------------------------------


def test_phase1_failed_quality_report_skips_jianying(tmp_path: Path) -> None:
    """Cue v2 quality gate fails closed when validation_status='failed'.

    Pre-populates subtitle_cues.json + subtitle_quality_report.json with
    validation_status='failed'. Uses a project with no semantic_blocks so the
    subtitle v2 pipeline does NOT overwrite the pre-registered report.

    Asserts:
    - No jianying artifacts registered.
    - Editor artifacts still produced (no regression).
    """
    from modules.output.jianying.jianying_draft_models import JianyingDraftResult  # noqa: PLC0415

    # Pre-write a 'failed' quality report
    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    cues_path = out_dir / "subtitle_cues.json"
    cues_path.write_text(
        json.dumps({
            "schema_version": "subtitle_cues_v2",
            "project_id": "j8_failed_gate_test",
            "cues": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    report_path = out_dir / "subtitle_quality_report.json"
    report_path.write_text(
        json.dumps({
            "schema_version": "subtitle_quality_report_v2",
            "project_id": "j8_failed_gate_test",
            "validation_status": "failed",
            "issues": [
                {
                    "block_id": "block_0001",
                    "cue_id": None,
                    "code": "text_mismatch",
                    "severity": "error",
                    "message": "Text mismatch detected.",
                }
            ],
            "block_summaries": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    # Build artifact_index with pre-registered subtitle artifacts
    artifact_index = ArtifactIndex()
    artifact_index.register("editor.subtitle_cues", str(cues_path))
    artifact_index.register("editor.subtitle_quality_report", str(report_path))

    # Use a project with no semantic_blocks so cue pipeline won't overwrite
    captions: list[SubtitleLine] = []
    project = LocalizedProject(
        project_id="j8_failed_gate_test",
        source_info={
            "source_kind": "local_video",
            "source_path": str(tmp_path / "source.mp4"),
        },
        artifacts=ArtifactIndex(),
        stage_snapshot={},
        semantic_blocks=[],
        aligned_blocks=[],
        captions=captions,
    )

    # Jianying backend should not be called at all
    fake_jianying_backend = MagicMock()
    fake_jianying_backend.write.side_effect = AssertionError(
        "jianying backend must not be called when quality gate fails"
    )

    class _NoOpEditorBackend:
        def write(self, output) -> ProjectOutputResult:
            Path(output.output_dir, "output").mkdir(parents=True, exist_ok=True)
            return _make_fake_editor_result(tmp_path)

    dispatcher = OutputDispatcher(
        editor_backend=_NoOpEditorBackend(),
        manifest_writer=_FakeManifestWriter(),
        jianying_backend=fake_jianying_backend,
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=True,
        service_mode="studio",
    )

    dispatcher.dispatch(project, artifact_index, request)  # must not raise

    fake_jianying_backend.write.assert_not_called()

    assert artifact_index.get("editor.jianying_draft_zip") is None, (
        "failed quality gate: jianying_draft_zip should not be registered"
    )
    assert artifact_index.get("editor.jianying_draft_dir") is None, (
        "failed quality gate: jianying_draft_dir should not be registered"
    )
    assert artifact_index.get("editor.jianying_compatibility_report") is None, (
        "failed quality gate: jianying_compatibility_report should not be registered "
        "(Gate 2 closes before backend is called)"
    )

    # Editor artifacts still registered (no regression)
    assert artifact_index.get("editor.dubbed_audio_complete"), (
        "editor.dubbed_audio_complete should still be registered even when jianying gate fails"
    )

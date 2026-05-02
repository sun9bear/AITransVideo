"""Phase 1 acceptance tests for Jianying draft backend modules (K1 rollback, Task J8 remnants).

Three scenarios that verify the backend layer is intact after the K1 rollback:

1. test_phase1_pytest_runs_clean_on_no_pyjianyingdraft_install
   Regression guard: production modules do not trigger pyJianYingDraft import on module
   load.

2. test_phase1_clean_env_skip_no_engine
   JianyingDraftBackend.write() with simulated missing pyJianYingDraft gracefully
   returns a skipped_no_engine result without raising.

3. test_phase1_subtitle_consistency_draft_vs_cues_json
   Cues in subtitle_cues.json (phase 1a canonical) match text + timing in the generated
   draft_content.json exactly. Proves phase 1a -> SRT -> pyJianYingDraft -> draft pipeline
   preserves data.  Calls backend directly (not via dispatcher).

Removed scenarios (dispatcher-dependent, moved to K4-K5):
- test_phase1_full_e2e_with_pyjianyingdraft (dispatcher e2e)
- test_phase1_express_mode_skipped_silently (dispatcher service_mode gate)
- test_phase1_failed_quality_report_skips_jianying (dispatcher cue quality gate)

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md
(K1 rollback; J8 / phase 1 closure for backend-layer scenarios)
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

import pytest

from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend
from modules.output.jianying.jianying_draft_models import JianyingDraftRequest


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


def _build_jianying_request(tmp_path: Path) -> JianyingDraftRequest:
    """Build a minimal JianyingDraftRequest pointing to real files in tmp_path."""
    dubbed = tmp_path / "dubbed_audio_complete.wav"
    if not dubbed.exists():
        _make_wav(str(dubbed))
    srt = tmp_path / "subtitles.srt"
    if not srt.exists():
        _make_srt(str(srt))
    return JianyingDraftRequest(
        project_id="k1_acceptance_test",
        project_title="K1 Acceptance Test",
        source_video_path=str(tmp_path / "source.mp4"),
        dubbed_audio_path=str(dubbed),
        subtitle_path=str(srt),
        output_dir=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Scenario 1: Module-load guard — no pyJianYingDraft import on module load
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
# Scenario 2: Clean env — simulated missing pyJianYingDraft via monkeypatch
# ---------------------------------------------------------------------------


def test_phase1_clean_env_skip_no_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JianyingDraftBackend.write() with monkeypatched missing pyJianYingDraft gracefully degrades.

    Calls backend directly (no dispatcher).

    Asserts:
    - write() completes without raising.
    - result.validation_status == "skipped_no_engine".
    - result.compatibility_report_path is set and contains valid JSON.
    - No draft zip produced (result.draft_zip_path is None or empty).
    """
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "pyJianYingDraft" or name.startswith("pyJianYingDraft."):
            raise ImportError("simulated clean env — pyJianYingDraft absent")
        return real_import(name, *args, **kwargs)

    # Remove cached writer module so the lazy import is triggered fresh
    writer_mod_name = "modules.output.jianying.jianying_draft_writer"
    saved_writer = sys.modules.pop(writer_mod_name, None)

    try:
        monkeypatch.setattr(builtins, "__import__", _fake_import)

        request = _build_jianying_request(tmp_path)
        backend = JianyingDraftBackend()
        result = backend.write(request)

    finally:
        if saved_writer is not None:
            sys.modules[writer_mod_name] = saved_writer
        elif writer_mod_name in sys.modules:
            del sys.modules[writer_mod_name]

    assert result is not None, "write() should return a result even on skip path"
    assert result.validation_status == "skipped_no_engine", (
        f"expected validation_status='skipped_no_engine', got: {result.validation_status!r}"
    )

    # No zip produced
    assert not result.draft_zip_path, (
        f"draft_zip_path should not be set on skip path; got: {result.draft_zip_path!r}"
    )

    # Compatibility report written and valid JSON
    assert result.compatibility_report_path, (
        "compatibility_report_path should be set even on skip path"
    )
    compat_report = json.loads(Path(result.compatibility_report_path).read_text(encoding="utf-8"))
    assert compat_report.get("validation_status") == "skipped_no_engine", (
        f"compatibility_report validation_status mismatch: {compat_report.get('validation_status')!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Subtitle consistency — draft_content.json matches SRT cues
# ---------------------------------------------------------------------------


def test_phase1_subtitle_consistency_draft_vs_cues_json(tmp_path: Path) -> None:
    """Cues in subtitle_cues.json (phase 1a canonical) match text + timing in draft_content.json.

    Calls backend directly (not via dispatcher).
    Proves: phase 1a cue v2 -> SRT -> pyJianYingDraft -> draft pipeline preserves data.

    Checks for each cue in subtitle_cues.json:
    - cue.text matches the corresponding text segment in draft_content.json (after normalize).
    - cue.start_ms * 1000 == segment.target_timerange.start (microseconds).
    - (cue.end_ms - cue.start_ms) * 1000 == segment.target_timerange.duration (microseconds).
    """
    pytest.importorskip("pyJianYingDraft")

    from core.artifact_index import ArtifactIndex
    from core.models import SemanticBlock, SubtitleLine
    from core.project_model import LocalizedProject
    from modules.output.output_dispatcher import OutputDispatcher
    from modules.output.output_models import OutputRequest
    from core.enums import OutputTarget

    # Build a minimal LocalizedProject with 2 blocks matching the SRT cues
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
    project = LocalizedProject(
        project_id="k1_consistency_test",
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

    # Use the dispatcher to get subtitle_cues.json written — we need it to verify
    # consistency.  The dispatcher is still the correct path for subtitle cue generation
    # (OutputDispatcher._generate_subtitle_cues).  After K1 it no longer calls the
    # jianying backend, so we call the backend separately below.
    class _FakeEditorBackend:
        def write(self, output):
            from modules.output.editor.editor_package_models import ProjectOutputResult
            out = Path(output.output_dir) / "output"
            out.mkdir(parents=True, exist_ok=True)
            dubbed = out / "dubbed_audio_complete.wav"
            if not dubbed.exists():
                _make_wav(str(dubbed))
            srt_path = out / "subtitles.srt"
            if not srt_path.exists():
                _make_srt(str(srt_path))
            return ProjectOutputResult(
                dubbed_audio_path=str(dubbed),
                ambient_audio_path=str(out / "ambient_audio.wav"),
                segments_dir=str(out / "segments"),
                segment_count=2,
                subtitles_path=str(srt_path),
                subtitles_en_path=str(out / "subtitles_en.srt"),
                subtitles_bilingual_path=str(out / "subtitles_bilingual.srt"),
                background_sounds_path=str(out / "background_sounds.txt"),
                alignment_report_path=str(out / "alignment_report.md"),
                needs_review_count=0,
            )

    class _FakeManifestWriter:
        def write(self, *, project_root, localized_project, artifact_index, request, output_bundle):
            manifest_path = str(Path(str(project_root)) / "manifest.json")
            Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
            Path(manifest_path).write_text(
                json.dumps({"project_id": localized_project.project_id}, ensure_ascii=False),
                encoding="utf-8",
            )
            return manifest_path

    artifact_index = ArtifactIndex()
    dispatcher = OutputDispatcher(
        editor_backend=_FakeEditorBackend(),
        manifest_writer=_FakeManifestWriter(),
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
    )
    dispatcher.dispatch(project, artifact_index, request)

    # --- Read subtitle_cues.json (phase 1a canonical output) ---
    cues_path_str = artifact_index.get("editor.subtitle_cues")
    assert cues_path_str, "editor.subtitle_cues should be registered after dispatch"
    cues_data = json.loads(Path(cues_path_str).read_text(encoding="utf-8"))
    cues = cues_data["cues"]
    assert len(cues) >= 1, "subtitle_cues.json should have at least 1 cue"

    # --- Now call JianyingDraftBackend directly with the SRT from editor output ---
    dubbed_path_str = artifact_index.get("editor.dubbed_audio_complete")
    srt_path_str = artifact_index.get("editor.subtitles")
    assert dubbed_path_str, "editor.dubbed_audio_complete must be registered"
    assert srt_path_str, "editor.subtitles must be registered"

    jianying_request = JianyingDraftRequest(
        project_id="k1_consistency_test",
        project_title="K1 Consistency Test",
        source_video_path=str(tmp_path / "source.mp4"),
        dubbed_audio_path=dubbed_path_str,
        subtitle_path=srt_path_str,
        output_dir=str(tmp_path),
    )
    backend = JianyingDraftBackend()
    result = backend.write(jianying_request)

    assert result.validation_status == "ok", (
        f"expected backend result 'ok', got {result.validation_status!r}; "
        f"report: {result.compatibility_report_path}"
    )
    assert result.draft_zip_path, "draft_zip_path should be set on success"

    zip_path = Path(result.draft_zip_path)
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

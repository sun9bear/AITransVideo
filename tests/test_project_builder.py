import pytest

from core.artifact_index import ArtifactIndex
from core.models import SemanticBlock, SubtitleLine
from modules.workflow.project_builder import ProjectBuilder
from modules.workflow.workflow_result import WorkflowBuildResult


def _build_caption() -> SubtitleLine:
    return SubtitleLine(
        index=1,
        start_ms=0,
        end_ms=1000,
        speaker_id="speaker_a",
        speaker_name="Speaker A",
        en_text="Hello",
        cn_text="你好",
    )


def _build_block(block_id: str = "block-1") -> SemanticBlock:
    return SemanticBlock(
        block_id=block_id,
        speaker_id="speaker_a",
        speaker_name="Speaker A",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1000,
        target_duration_ms=1000,
        merged_cn_text="你好",
    )


def test_project_builder_builds_localized_project_from_current_stage_names() -> None:
    builder = ProjectBuilder()
    artifacts = ArtifactIndex()
    caption = _build_caption()
    block = _build_block()
    aligned_block = _build_block("block-1-aligned")

    project = builder.build(
        project_id="workflow-demo",
        source_info={"source_kind": "local_audio"},
        artifact_index=artifacts,
        stage_snapshot={"alignment": {"status": "done"}},
        stage_outputs={
            "translated_lines": [caption],
            "blocks": [block],
            "aligned_blocks": [aligned_block],
        },
    )

    assert project.project_id == "workflow-demo"
    assert project.captions[0].speaker_id == "speaker_a"
    assert project.semantic_blocks[0].block_id == "block-1"
    assert project.aligned_blocks[0].block_id == "block-1-aligned"


def test_project_builder_prefers_canonical_keys_when_present() -> None:
    builder = ProjectBuilder()
    artifacts = ArtifactIndex()
    canonical_caption = _build_caption()
    fallback_caption = SubtitleLine(
        index=2,
        start_ms=1000,
        end_ms=2000,
        speaker_id="speaker_b",
        speaker_name="Speaker B",
        en_text="Bye",
        cn_text="再见",
    )
    canonical_block = _build_block("canonical-block")
    fallback_block = _build_block("fallback-block")

    project = builder.build(
        project_id="workflow-demo",
        source_info={"source_kind": "local_audio"},
        artifact_index=artifacts,
        stage_snapshot={},
        stage_outputs={
            "captions": [canonical_caption],
            "translated_lines": [fallback_caption],
            "semantic_blocks": [canonical_block],
            "blocks": [fallback_block],
        },
    )

    assert project.captions[0].index == 1
    assert project.semantic_blocks[0].block_id == "canonical-block"


def test_project_builder_treats_subtitle_lines_as_compatibility_fallback_only() -> None:
    builder = ProjectBuilder()
    artifacts = ArtifactIndex()
    canonical_caption = _build_caption()
    fallback_caption = SubtitleLine(
        index=2,
        start_ms=1000,
        end_ms=2000,
        speaker_id="speaker_b",
        speaker_name="Speaker B",
        en_text="Fallback",
        cn_text="鍏煎",
    )

    project = builder.build(
        project_id="workflow-demo",
        source_info={"source_kind": "local_audio"},
        artifact_index=artifacts,
        stage_snapshot={},
        stage_outputs={
            "captions": [canonical_caption],
            "subtitle_lines": [fallback_caption],
        },
    )

    assert project.captions[0].index == canonical_caption.index


def test_project_builder_rejects_invalid_stage_output_shape() -> None:
    builder = ProjectBuilder()

    with pytest.raises(TypeError, match="stage_outputs\\['captions'\\] must be a list or tuple"):
        builder.build(
            project_id="workflow-demo",
            source_info={"source_kind": "local_audio"},
            artifact_index=ArtifactIndex(),
            stage_snapshot={},
            stage_outputs={"captions": {"bad": "shape"}},
        )


def test_project_builder_build_artifact_index_skips_empty_values_and_supports_overrides() -> None:
    builder = ProjectBuilder()

    artifact_index = builder.build_artifact_index(
        [
            ("source.original_audio", ""),
            ("source.original_audio", "input/original.wav"),
            ("working.ambient_audio", None),
            ("editor.draft_dir", "output/draft"),
        ]
    )

    assert artifact_index.require("source.original_audio") == "input/original.wav"
    assert artifact_index.require("editor.draft_dir") == "output/draft"
    assert artifact_index.get("working.ambient_audio") is None


def test_project_builder_build_result_returns_workflow_build_result() -> None:
    builder = ProjectBuilder()
    artifacts = builder.build_artifact_index({"source.original_audio": "input/original.wav"})
    caption = _build_caption()

    result = builder.build_result(
        project_id="workflow-demo",
        source_info={"source_kind": "local_audio"},
        artifact_index=artifacts,
        stage_snapshot={"translation": {"status": "done"}},
        stage_outputs={"captions": [caption]},
    )

    assert isinstance(result, WorkflowBuildResult)
    assert result.project_id == "workflow-demo"
    assert result.localized_project.captions[0].index == caption.index
    assert result.artifact_index.require("source.original_audio") == "input/original.wav"

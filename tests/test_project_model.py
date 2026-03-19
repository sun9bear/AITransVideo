import pytest

from core.artifact_index import ArtifactIndex
from core.models import SemanticBlock, SubtitleLine
from core.project_model import LocalizedProject


def test_localized_project_accepts_minimum_fields_and_serializes() -> None:
    artifacts = ArtifactIndex()
    artifacts.register("source.original_audio", "input/original.wav")

    project = LocalizedProject(
        project_id="demo-project",
        source_info={"source_kind": "local_audio"},
        artifacts=artifacts,
        stage_snapshot={"translation": {"status": "done"}},
    )

    assert project.project_id == "demo-project"
    assert project.source_info == {"source_kind": "local_audio"}
    assert project.artifacts.require("source.original_audio") == "input/original.wav"
    assert project.stage_snapshot == {"translation": {"status": "done"}}
    assert project.to_dict() == {
        "project_id": "demo-project",
        "source_info": {"source_kind": "local_audio"},
        "artifacts": {"source.original_audio": "input/original.wav"},
        "stage_snapshot": {"translation": {"status": "done"}},
        "semantic_blocks": [],
        "aligned_blocks": [],
        "captions": [],
    }


def test_localized_project_preserves_optional_canonical_lists() -> None:
    artifacts = ArtifactIndex()
    caption = SubtitleLine(
        index=1,
        start_ms=0,
        end_ms=1000,
        speaker_id="speaker_a",
        speaker_name="Speaker A",
        en_text="Hello",
        cn_text="你好",
    )
    block = SemanticBlock(
        block_id="block-1",
        speaker_id="speaker_a",
        speaker_name="Speaker A",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1000,
        target_duration_ms=1000,
        merged_cn_text="你好",
    )

    project = LocalizedProject(
        project_id="demo-project",
        source_info={"source_kind": "local_audio"},
        artifacts=artifacts,
        stage_snapshot={},
        semantic_blocks=[block],
        aligned_blocks=[block],
        captions=[caption],
    )

    serialized = project.to_dict()

    assert len(project.semantic_blocks) == 1
    assert len(project.aligned_blocks) == 1
    assert len(project.captions) == 1
    assert serialized["semantic_blocks"][0]["block_id"] == "block-1"
    assert serialized["aligned_blocks"][0]["block_id"] == "block-1"
    assert serialized["captions"][0]["speaker_id"] == "speaker_a"


def test_localized_project_rejects_invalid_minimum_fields() -> None:
    with pytest.raises(ValueError, match="project_id is required"):
        LocalizedProject(
            project_id="   ",
            source_info={},
            artifacts=ArtifactIndex(),
            stage_snapshot={},
        )

    with pytest.raises(TypeError, match="artifacts must be an ArtifactIndex"):
        LocalizedProject(
            project_id="demo-project",
            source_info={},
            artifacts={},  # type: ignore[arg-type]
            stage_snapshot={},
        )

import pytest

from core.artifact_index import ArtifactIndex
from core.project_model import LocalizedProject
from modules.workflow.workflow_result import WorkflowBuildResult


def _build_localized_project(project_id: str = "workflow-demo") -> LocalizedProject:
    artifacts = ArtifactIndex()
    artifacts.register("source.original_audio", "input/original.wav")
    return LocalizedProject(
        project_id=project_id,
        source_info={"source_kind": "local_audio"},
        artifacts=artifacts,
        stage_snapshot={"translation": {"status": "done"}},
    )


def test_workflow_build_result_accepts_minimum_fields_and_serializes() -> None:
    project = _build_localized_project()

    result = WorkflowBuildResult(
        project_id="workflow-demo",
        localized_project=project,
        artifact_index=project.artifacts,
        stage_snapshot={"translation": {"status": "done"}},
    )

    assert result.project_id == "workflow-demo"
    assert result.localized_project.project_id == "workflow-demo"
    assert result.artifact_index.require("source.original_audio") == "input/original.wav"
    assert result.to_dict()["localized_project"]["source_info"] == {"source_kind": "local_audio"}


def test_workflow_build_result_rejects_invalid_types_and_mismatched_project_id() -> None:
    project = _build_localized_project("workflow-demo")

    with pytest.raises(ValueError, match="project_id is required"):
        WorkflowBuildResult(
            project_id="   ",
            localized_project=project,
            artifact_index=project.artifacts,
            stage_snapshot={},
        )

    with pytest.raises(TypeError, match="localized_project must be a LocalizedProject"):
        WorkflowBuildResult(
            project_id="workflow-demo",
            localized_project={},  # type: ignore[arg-type]
            artifact_index=project.artifacts,
            stage_snapshot={},
        )

    with pytest.raises(TypeError, match="artifact_index must be an ArtifactIndex"):
        WorkflowBuildResult(
            project_id="workflow-demo",
            localized_project=project,
            artifact_index={},  # type: ignore[arg-type]
            stage_snapshot={},
        )

    with pytest.raises(ValueError, match="project_id must match localized_project.project_id"):
        WorkflowBuildResult(
            project_id="other-project",
            localized_project=project,
            artifact_index=project.artifacts,
            stage_snapshot={},
        )

from dataclasses import dataclass, field
from typing import Any, Mapping

from core.artifact_index import ArtifactIndex
from core.project_model import LocalizedProject


@dataclass(slots=True)
class WorkflowBuildResult:
    """Canonical workflow build output before output dispatch."""

    project_id: str
    localized_project: LocalizedProject
    artifact_index: ArtifactIndex
    stage_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_id = self.project_id.strip()
        if not self.project_id:
            raise ValueError("project_id is required")
        if not isinstance(self.localized_project, LocalizedProject):
            raise TypeError("localized_project must be a LocalizedProject")
        if not isinstance(self.artifact_index, ArtifactIndex):
            raise TypeError("artifact_index must be an ArtifactIndex")
        if self.localized_project.project_id != self.project_id:
            raise ValueError("project_id must match localized_project.project_id")
        self.stage_snapshot = dict(self.stage_snapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "localized_project": self.localized_project.to_dict(),
            "artifact_index": self.artifact_index.to_dict(),
            "stage_snapshot": dict(self.stage_snapshot),
        }

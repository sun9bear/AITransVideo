from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Mapping

from core.artifact_index import ArtifactIndex
from core.models import SemanticBlock, SubtitleLine


@dataclass(slots=True)
class LocalizedProject:
    """Canonical project model shared across output backends."""

    project_id: str
    source_info: Mapping[str, Any]
    artifacts: ArtifactIndex
    stage_snapshot: Mapping[str, Any]
    semantic_blocks: list[SemanticBlock] = field(default_factory=list)
    aligned_blocks: list[SemanticBlock] = field(default_factory=list)
    captions: list[SubtitleLine] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project_id = self.project_id.strip()
        if not self.project_id:
            raise ValueError("project_id is required")
        if not isinstance(self.artifacts, ArtifactIndex):
            raise TypeError("artifacts must be an ArtifactIndex")

        self.source_info = dict(self.source_info)
        self.stage_snapshot = dict(self.stage_snapshot)
        self.semantic_blocks = list(self.semantic_blocks)
        self.aligned_blocks = list(self.aligned_blocks)
        self.captions = list(self.captions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_info": dict(self.source_info),
            "artifacts": self.artifacts.to_dict(),
            "stage_snapshot": dict(self.stage_snapshot),
            "semantic_blocks": [self._serialize_value(block) for block in self.semantic_blocks],
            "aligned_blocks": [self._serialize_value(block) for block in self.aligned_blocks],
            "captions": [self._serialize_value(line) for line in self.captions],
        }

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return value.to_dict()
        if is_dataclass(value):
            return asdict(value)
        return value

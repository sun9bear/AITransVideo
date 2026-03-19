from dataclasses import dataclass
from os import PathLike
from typing import Any, Iterable, Mapping

from core.artifact_index import ArtifactIndex
from core.project_model import LocalizedProject
from modules.workflow.workflow_result import WorkflowBuildResult


@dataclass(slots=True)
class ProjectBuilder:
    """Build the canonical project model from workflow stage outputs."""

    def build_artifact_index(
        self,
        artifact_entries: Mapping[str, object] | Iterable[tuple[str, object]],
    ) -> ArtifactIndex:
        artifact_index = ArtifactIndex()
        for key, value in self._iter_artifact_entries(artifact_entries):
            normalized_value = self._normalize_artifact_value(value)
            if normalized_value is None:
                continue
            artifact_index.register(key, normalized_value)
        return artifact_index

    def build(
        self,
        *,
        project_id: str,
        source_info: Mapping[str, Any],
        artifact_index: ArtifactIndex,
        stage_snapshot: Mapping[str, Any],
        stage_outputs: Mapping[str, object] | None = None,
    ) -> LocalizedProject:
        normalized_stage_outputs = dict(stage_outputs or {})
        semantic_blocks = self._read_stage_output_sequence(
            normalized_stage_outputs,
            "semantic_blocks",
            "blocks",
        )
        aligned_blocks = self._read_stage_output_sequence(
            normalized_stage_outputs,
            "aligned_blocks",
        )
        # Compatibility only: new workflow code should publish canonical captions,
        # while legacy callers may still surface transitional subtitle_lines keys.
        captions = self._read_stage_output_sequence(
            normalized_stage_outputs,
            "captions",
            "translated_lines",
            "subtitle_lines",
        )
        return LocalizedProject(
            project_id=project_id,
            source_info=source_info,
            artifacts=artifact_index,
            stage_snapshot=stage_snapshot,
            semantic_blocks=semantic_blocks,
            aligned_blocks=aligned_blocks,
            captions=captions,
        )

    def build_result(
        self,
        *,
        project_id: str,
        source_info: Mapping[str, Any],
        artifact_index: ArtifactIndex,
        stage_snapshot: Mapping[str, Any],
        stage_outputs: Mapping[str, object] | None = None,
    ) -> WorkflowBuildResult:
        localized_project = self.build(
            project_id=project_id,
            source_info=source_info,
            artifact_index=artifact_index,
            stage_snapshot=stage_snapshot,
            stage_outputs=stage_outputs,
        )
        return WorkflowBuildResult(
            project_id=project_id,
            localized_project=localized_project,
            artifact_index=artifact_index,
            stage_snapshot=stage_snapshot,
        )

    @staticmethod
    def _read_stage_output_sequence(
        stage_outputs: Mapping[str, object],
        *keys: str,
    ) -> list[object]:
        for key in keys:
            if key not in stage_outputs:
                continue
            value = stage_outputs[key]
            if value is None:
                return []
            if not isinstance(value, (list, tuple)):
                raise TypeError(f"stage_outputs[{key!r}] must be a list or tuple")
            return list(value)
        return []

    @staticmethod
    def _iter_artifact_entries(
        artifact_entries: Mapping[str, object] | Iterable[tuple[str, object]],
    ) -> Iterable[tuple[str, object]]:
        if isinstance(artifact_entries, Mapping):
            return artifact_entries.items()
        return artifact_entries

    @staticmethod
    def _normalize_artifact_value(value: object) -> str | PathLike[str] | None:
        if isinstance(value, str):
            normalized_value = value.strip()
            return normalized_value or None
        if isinstance(value, PathLike):
            return value
        return None

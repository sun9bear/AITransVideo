from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from core.artifact_index import ArtifactIndex
from core.project_model import LocalizedProject
from modules.output.output_models import OutputBundleResult, OutputRequest
from services.state_manager import utc_now_iso


class ManifestWriter:
    """Write the minimal shared output manifest for a workflow build."""

    def __init__(self, filename: str = "manifest.json") -> None:
        self.filename = filename

    def write(
        self,
        *,
        project_root: str | Path,
        localized_project: LocalizedProject,
        artifact_index: ArtifactIndex,
        request: OutputRequest,
        output_bundle: OutputBundleResult,
    ) -> str:
        project_root_path = Path(project_root).resolve(strict=False)
        project_root_path.mkdir(parents=True, exist_ok=True)
        output_path = project_root_path / self.filename
        payload = self.build_payload(
            localized_project=localized_project,
            artifact_index=artifact_index,
            request=request,
            output_bundle=output_bundle,
        )
        self._write_json_atomic(output_path, payload)
        return str(output_path)

    def build_payload(
        self,
        *,
        localized_project: LocalizedProject,
        artifact_index: ArtifactIndex,
        request: OutputRequest,
        output_bundle: OutputBundleResult,
    ) -> dict[str, object]:
        artifact_map = artifact_index.to_dict()
        return {
            "manifest_version": "aivideotrans_output_manifest_v1",
            "generated_at": utc_now_iso(),
            "project_id": localized_project.project_id,
            "requested_targets": [target.value for target in request.expanded_targets()],
            "source_info": dict(localized_project.source_info),
            "key_audio_assets": self._build_key_audio_assets(artifact_map),
            "primary_outputs": self._build_primary_outputs(output_bundle),
            "fallback_summary": self._build_fallback_summary(localized_project.stage_snapshot),
            "artifact_index": artifact_map,
        }

    @staticmethod
    def _build_key_audio_assets(artifact_map: Mapping[str, str]) -> dict[str, str]:
        priority_keys = (
            "source.original_audio",
            "working.speech_for_asr",
            "working.ambient_audio",
            "editor.dubbed_audio_complete",
            "editor.ambient_audio",
        )
        key_audio_assets: dict[str, str] = {}
        for key in priority_keys:
            value = artifact_map.get(key)
            if isinstance(value, str) and value.strip():
                key_audio_assets[key] = value
        return key_audio_assets

    @staticmethod
    def _build_primary_outputs(output_bundle: OutputBundleResult) -> dict[str, object]:
        editor_result = output_bundle.editor_result
        publish_result = output_bundle.publish_result
        return {
            "editor": (
                {
                    "dubbed_audio_path": editor_result.dubbed_audio_path,
                    "ambient_audio_path": editor_result.ambient_audio_path,
                    "segments_dir": editor_result.segments_dir,
                    "subtitles_path": editor_result.subtitles_path,
                    "alignment_report_path": editor_result.alignment_report_path,
                }
                if editor_result is not None
                else None
            ),
            "publish": (
                {
                    "dubbed_video_path": publish_result.dubbed_video_path,
                    "original_video_path": publish_result.original_video_path,
                    "dubbed_audio_path": publish_result.dubbed_audio_path,
                }
                if publish_result is not None
                else None
            ),
        }

    @staticmethod
    def _build_fallback_summary(stage_snapshot: Mapping[str, Any]) -> dict[str, dict[str, object]]:
        fallback_summary: dict[str, dict[str, object]] = {}
        for stage_name, stage_data in stage_snapshot.items():
            if not isinstance(stage_data, dict):
                continue
            payload = stage_data.get("payload", {})
            if not isinstance(payload, dict):
                continue
            summary = {
                "status": stage_data.get("status"),
                "execution_mode": payload.get("execution_mode"),
                "fallback_applied": payload.get("fallback_applied"),
                "fallback_reason": payload.get("fallback_reason"),
                "fallback_trigger": payload.get("fallback_trigger"),
                "restore_reason": payload.get("restore_reason"),
                "rerun_reason": payload.get("rerun_reason"),
                "skipped": payload.get("skipped"),
                "error_type": payload.get("error_type"),
            }
            if not any(
                (
                    summary["fallback_applied"],
                    summary["fallback_reason"],
                    summary["fallback_trigger"],
                    summary["restore_reason"],
                    summary["skipped"],
                    summary["error_type"],
                )
            ):
                continue
            fallback_summary[stage_name] = summary
        return fallback_summary

    @staticmethod
    def _write_json_atomic(output_path: Path, payload: dict[str, object]) -> None:
        temp_path: Path | None = None
        try:
            serialized_payload = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f"{output_path.stem}_",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(serialized_payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, output_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

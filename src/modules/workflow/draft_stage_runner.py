from dataclasses import dataclass

from core.enums import StageStatus
from core.exceptions import WorkflowError
from core.models import SemanticBlock, SubtitleLine
from modules.draft.caption_retiming import CaptionRetimer
from modules.draft.draft_writer import DraftWriteResult, DraftWriter
from modules.output.editor.draft_backend import DraftBackend
from modules.workflow.restore_policy import evaluate_draft_restore
from modules.workflow.stage_helpers import build_artifacts_payload, get_stage_payload_value
from services.state_manager import StateManager


@dataclass(slots=True)
class DraftStageRunnerConfig:
    project_id: str


class DraftStageRunner:
    """Execute draft writing and lightweight draft artifact reuse."""

    def __init__(
        self,
        caption_retimer: CaptionRetimer,
        draft_writer: DraftWriter,
        state_manager: StateManager,
        config: DraftStageRunnerConfig,
        draft_backend: DraftBackend | None = None,
    ) -> None:
        self.draft_writer = draft_writer
        self.state_manager = state_manager
        self.config = config
        self.draft_backend = draft_backend or DraftBackend(
            caption_retimer=caption_retimer,
            draft_writer=draft_writer,
        )

    def run(
        self,
        translated_lines: list[SubtitleLine],
        aligned_blocks: list[SemanticBlock],
    ) -> DraftWriteResult:
        stage_name = "draft"
        restore_decision = evaluate_draft_restore(
            self.state_manager,
            lambda: self.draft_backend.load_existing_result(self.config.project_id),
        )
        if restore_decision.reuse_allowed and restore_decision.restored_result is not None:
            restored_result = restore_decision.restored_result
            self.state_manager.set_stage(
                stage_name,
                StageStatus.DONE,
                self._build_draft_stage_payload(
                    restored_result,
                    execution_mode="reuse_existing_artifacts",
                    skipped=True,
                    restore_reason=restore_decision.restore_reason,
                    rerun_reason=restore_decision.rerun_reason,
                    artifact_paths=restore_decision.artifact_paths,
                    reused_artifacts=restore_decision.reused_artifacts,
                ),
            )
            return restored_result

        self.state_manager.set_stage(
            stage_name,
            StageStatus.RUNNING,
            {
                "execution_mode": "fresh_write",
                "source_input_hash": restore_decision.source_input_hash,
                "artifact_paths": [],
                "reused_artifacts": [],
                "restore_reason": None,
                "rerun_reason": restore_decision.rerun_reason,
            },
        )
        try:
            draft_result = self.draft_backend.write(
                project_id=self.config.project_id,
                translated_lines=translated_lines,
                aligned_blocks=aligned_blocks,
                stage_snapshot=self.state_manager.load().get("stages", {}),
            )
            self.state_manager.set_stage(
                stage_name,
                StageStatus.DONE,
                self._build_draft_stage_payload(
                    draft_result,
                    execution_mode="fresh_write",
                    skipped=False,
                    restore_reason=None,
                    rerun_reason=restore_decision.rerun_reason,
                    artifact_paths=None,
                    reused_artifacts=[],
                ),
            )
            return draft_result
        except Exception as exc:
            self.state_manager.set_stage(
                stage_name,
                StageStatus.FAILED,
                payload={
                    "execution_mode": "fresh_write",
                    "source_input_hash": restore_decision.source_input_hash,
                    "artifact_paths": [],
                    "reused_artifacts": [],
                    "restore_reason": None,
                    "rerun_reason": restore_decision.rerun_reason,
                },
                error_message=str(exc),
            )
            raise WorkflowError("Draft stage failed.") from exc

    def _build_draft_stage_payload(
        self,
        draft_result: DraftWriteResult,
        execution_mode: str,
        skipped: bool,
        restore_reason: str | None,
        rerun_reason: str | None,
        artifact_paths: list[str] | None,
        reused_artifacts: list[str],
    ) -> dict[str, object]:
        export_project = draft_result.export_project
        normalized_artifact_paths = artifact_paths or [
            draft_result.draft_content_path,
            draft_result.draft_meta_info_path,
            draft_result.export_path,
        ]
        export_summary = {
            "audio_track_count": len(export_project.timeline.audio_tracks) if export_project is not None else None,
            "caption_track_count": len(export_project.timeline.caption_tracks) if export_project is not None else None,
            "audio_material_count": len(export_project.audio_materials) if export_project is not None else None,
        }
        return {
            "draft_dir": draft_result.draft_dir,
            "draft_content_path": draft_result.draft_content_path,
            "draft_meta_info_path": draft_result.draft_meta_info_path,
            "material_dir": draft_result.material_dir,
            "export_path": draft_result.export_path,
            "block_count": draft_result.block_count,
            "caption_count": draft_result.caption_count,
            "material_count": draft_result.material_count,
            "source_input_hash": get_stage_payload_value(self.state_manager, "ingestion", "input_hash"),
            "execution_mode": execution_mode,
            "skipped": skipped,
            "artifact_paths": [path for path in normalized_artifact_paths if isinstance(path, str) and path],
            "reused_artifacts": [path for path in reused_artifacts if isinstance(path, str) and path],
            "restore_reason": restore_reason,
            "rerun_reason": rerun_reason,
            "artifacts": build_artifacts_payload(
                kind="draft_export_bundle",
                file_paths=normalized_artifact_paths,
                extra={
                    "draft_dir": draft_result.draft_dir,
                    "material_dir": draft_result.material_dir,
                },
            ),
            "export_summary": export_summary,
            "export_validation_report": draft_result.export_validation_report,
        }

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile

from core.exceptions import DraftError
from core.models import SemanticBlock
from modules.draft.caption_retiming import RetimedCaption
from modules.draft.export_validator import JianyingExportValidator
from modules.draft.jianying_adapter import JianyingExportAdapter
from modules.draft.material_mapper import MaterialMapper
from modules.draft.export_schema import JianyingLikeExport
from modules.draft.schema import (
    DraftAudioTrack,
    DraftAudioTrackItem,
    DraftCaptionItem,
    DraftCaptionTrack,
    DraftMaterial,
    DraftProject,
    DraftTimeline,
)
from modules.draft.schema_validator import DraftSchemaValidator
from services.state_manager import utc_now_iso


@dataclass(slots=True)
class DraftWriterConfig:
    draft_folder_name: str = "draft"
    content_filename: str = "draft_content.json"
    meta_filename: str = "draft_meta_info.json"
    export_filename: str = "jianying_like_export.json"


@dataclass(slots=True)
class DraftWriteResult:
    draft_dir: str
    draft_content_path: str
    draft_meta_info_path: str
    material_dir: str
    block_count: int
    caption_count: int
    material_count: int
    draft_project: DraftProject | None = None
    export_path: str | None = None
    export_project: JianyingLikeExport | None = None
    export_validation_report: dict[str, object] | None = None


class DraftWriter:
    """Write an internal Jianying-like draft scaffold to disk."""

    def __init__(
        self,
        output_root_dir: str,
        material_mapper: MaterialMapper | None = None,
        config: DraftWriterConfig | None = None,
    ) -> None:
        self.output_root_dir = Path(output_root_dir)
        self.material_mapper = material_mapper or MaterialMapper()
        self.config = config or DraftWriterConfig()
        self.validator = DraftSchemaValidator()
        self.export_adapter = JianyingExportAdapter()
        self.export_validator = JianyingExportValidator()

    def resolve_draft_dir(self, project_id: str) -> Path:
        return self.output_root_dir / project_id / self.config.draft_folder_name

    def load_existing_result(self, project_id: str) -> DraftWriteResult | None:
        draft_dir = self.resolve_draft_dir(project_id)
        draft_content_path = draft_dir / self.config.content_filename
        draft_meta_info_path = draft_dir / self.config.meta_filename
        export_path = draft_dir / self.config.export_filename
        materials_dir = draft_dir / "materials"

        required_paths = (draft_dir, draft_content_path, draft_meta_info_path, export_path, materials_dir)
        if not all(path.exists() for path in required_paths):
            return None

        try:
            draft_content = json.loads(draft_content_path.read_text(encoding="utf-8"))
            draft_meta = json.loads(draft_meta_info_path.read_text(encoding="utf-8"))
            export_json = json.loads(export_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if draft_content.get("project_id") != project_id or export_json.get("project_id") != project_id:
            return None

        summary = draft_meta.get("summary", {})
        block_count = self._read_int(summary.get("block_count"), fallback=0)
        caption_count = self._read_int(summary.get("caption_count"), fallback=0)
        material_count = self._read_int(summary.get("material_count"), fallback=0)
        if caption_count == 0:
            caption_count = self._count_caption_items(draft_content)
        if material_count == 0:
            material_count = len(draft_content.get("materials", []))

        return DraftWriteResult(
            draft_dir=str(draft_dir),
            draft_content_path=str(draft_content_path),
            draft_meta_info_path=str(draft_meta_info_path),
            material_dir=str(materials_dir),
            block_count=block_count,
            caption_count=caption_count,
            material_count=material_count,
            export_path=str(export_path),
        )

    def write(
        self,
        project_id: str,
        blocks: list[SemanticBlock],
        captions: list[RetimedCaption],
        stage_snapshot: dict[str, object] | None = None,
    ) -> DraftWriteResult:
        if not blocks:
            raise DraftError("Cannot write draft scaffold without blocks.")

        draft_dir = self.resolve_draft_dir(project_id)
        materials_dir = draft_dir / "materials"
        draft_dir.mkdir(parents=True, exist_ok=True)
        materials_dir.mkdir(parents=True, exist_ok=True)

        materials = self.material_mapper.map_audio_materials(blocks)
        copied_materials = self._copy_materials(materials, materials_dir)
        draft_project = self.build_project(
            project_id=project_id,
            blocks=blocks,
            materials=copied_materials,
            captions=captions,
            stage_snapshot=stage_snapshot,
        )
        self.validator.validate(draft_project)
        export_project = self.export_adapter.adapt(draft_project)
        export_validation_report = self.export_validator.validate(export_project)

        draft_content_path = draft_dir / self.config.content_filename
        draft_meta_info_path = draft_dir / self.config.meta_filename
        export_path = draft_dir / self.config.export_filename
        self._write_json_atomic(draft_content_path, draft_project.to_content_dict())
        self._write_json_atomic(draft_meta_info_path, draft_project.to_meta_info_dict())
        self._write_json_atomic(export_path, export_project.to_dict())

        return DraftWriteResult(
            draft_dir=str(draft_dir),
            draft_content_path=str(draft_content_path),
            draft_meta_info_path=str(draft_meta_info_path),
            material_dir=str(materials_dir),
            block_count=draft_project.block_count,
            caption_count=draft_project.caption_count,
            material_count=draft_project.material_count,
            draft_project=draft_project,
            export_path=str(export_path),
            export_project=export_project,
            export_validation_report=export_validation_report,
        )

    def build_project(
        self,
        project_id: str,
        blocks: list[SemanticBlock],
        materials: list[DraftMaterial],
        captions: list[RetimedCaption],
        stage_snapshot: dict[str, object] | None = None,
    ) -> DraftProject:
        if not materials:
            raise DraftError("Cannot build draft project without materials.")

        audio_track = DraftAudioTrack(
            track_id="audio_track_0001",
            items=[
                DraftAudioTrackItem(
                    item_id=f"audio_item_{index:04d}",
                    material_id=material.material_id,
                    block_id=material.block_id,
                    start_ms=material.start_ms,
                    duration_ms=material.duration_ms,
                )
                for index, material in enumerate(materials, start=1)
            ],
        )
        caption_track = DraftCaptionTrack(
            track_id="caption_track_0001",
            items=[
                DraftCaptionItem(
                    item_id=f"caption_item_{index:04d}",
                    caption_id=caption.caption_id,
                    block_id=caption.block_id,
                    source_srt_index=caption.source_srt_index,
                    speaker_id=caption.speaker_id,
                    speaker_name=caption.speaker_name,
                    text=caption.text,
                    start_ms=caption.start_ms,
                    end_ms=caption.end_ms,
                )
                for index, caption in enumerate(captions, start=1)
            ],
        )
        timeline_duration_ms = max(
            [item.end_ms for item in audio_track.items] + [item.end_ms for item in caption_track.items],
            default=0,
        )
        return DraftProject(
            schema_version="jianying_draft_scaffold_v1",
            project_id=project_id,
            generated_at=utc_now_iso(),
            block_count=len(blocks),
            caption_count=len(caption_track.items),
            material_count=len(materials),
            timeline=DraftTimeline(
                duration_ms=timeline_duration_ms,
                audio_tracks=[audio_track],
                caption_tracks=[caption_track],
            ),
            materials=materials,
            notes=[
                "Sprint 4A internal draft scaffold.",
                "This is not a full Jianying private project export.",
            ],
            stage_snapshot=stage_snapshot or {},
        )

    def _copy_materials(
        self,
        materials: list[DraftMaterial],
        materials_dir: Path,
    ) -> list[DraftMaterial]:
        copied_materials: list[DraftMaterial] = []
        for material in materials:
            target_path = materials_dir / Path(material.relative_material_path).name
            if not Path(material.source_audio_path).exists():
                raise DraftError(f"Material source file not found: {material.source_audio_path}")
            shutil.copy2(material.source_audio_path, target_path)
            copied_materials.append(
                DraftMaterial(
                    material_id=material.material_id,
                    block_id=material.block_id,
                    source_audio_path=material.source_audio_path,
                    relative_material_path=f"materials/{target_path.name}",
                    start_ms=material.start_ms,
                    duration_ms=material.duration_ms,
                    speaker_id=material.speaker_id,
                    speaker_name=material.speaker_name,
                )
            )
        return copied_materials

    def _write_json_atomic(self, output_path: Path, payload: dict[str, object]) -> None:
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
        except OSError as exc:
            raise DraftError(f"Failed to write draft JSON: {output_path}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _count_caption_items(self, draft_content: dict[str, object]) -> int:
        timeline = draft_content.get("timeline", {})
        if not isinstance(timeline, dict):
            return 0
        caption_tracks = timeline.get("caption_tracks", [])
        if not isinstance(caption_tracks, list):
            return 0

        caption_count = 0
        for track in caption_tracks:
            if not isinstance(track, dict):
                continue
            items = track.get("items", [])
            if isinstance(items, list):
                caption_count += len(items)
        return caption_count

    def _read_int(self, value: object, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

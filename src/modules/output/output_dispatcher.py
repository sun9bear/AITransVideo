from __future__ import annotations

from pathlib import Path

from core.artifact_index import ArtifactIndex
from core.enums import OutputTarget
from core.exceptions import PublishError
from core.project_model import LocalizedProject
from modules.output.editor.editor_package_backend import EditorPackageBackend
from modules.output.editor.editor_package_models import AlignedSegment, ProjectOutput, ProjectOutputResult
from modules.output.manifest_writer import ManifestWriter
from modules.output.output_models import OutputBundleResult, OutputRequest
from modules.output.publish.publish_backend import PublishBackend
from modules.output.publish.publish_models import PublishRequest, PublishResult


class OutputDispatcher:
    """Dispatch a canonical localized project to editor and publish backends."""

    def __init__(
        self,
        *,
        editor_backend: EditorPackageBackend | None = None,
        publish_backend: PublishBackend | None = None,
        manifest_writer: ManifestWriter | None = None,
    ) -> None:
        self.editor_backend = editor_backend or EditorPackageBackend()
        self.publish_backend = publish_backend or PublishBackend()
        self.manifest_writer = manifest_writer or ManifestWriter()

    def dispatch(
        self,
        localized_project: LocalizedProject,
        artifact_index: ArtifactIndex,
        request: OutputRequest,
    ) -> OutputBundleResult:
        expanded_targets = request.expanded_targets()
        project_root = self._resolve_project_root(localized_project, artifact_index, request)
        editor_result: ProjectOutputResult | None = None
        publish_result: PublishResult | None = None

        if OutputTarget.EDITOR in expanded_targets or OutputTarget.PUBLISH in expanded_targets:
            editor_result = self.editor_backend.write(
                self._build_editor_project_output(localized_project, artifact_index, project_root)
            )
            self._register_editor_artifacts(artifact_index, editor_result)

        if OutputTarget.PUBLISH in expanded_targets:
            original_video_path = artifact_index.get("source.original_video")
            if not original_video_path:
                raise PublishError("Publish output requires source.original_video in ArtifactIndex.")
            assert editor_result is not None
            publish_result = self.publish_backend.publish(
                PublishRequest(
                    project_id=localized_project.project_id,
                    original_video_path=original_video_path,
                    dubbed_audio_path=editor_result.dubbed_audio_path,
                    output_dir=str((project_root / "publish").resolve(strict=False)),
                )
            )
            artifact_index.register("publish.dubbed_video", publish_result.dubbed_video_path)

        output_bundle = OutputBundleResult(
            editor_result=editor_result if OutputTarget.EDITOR in expanded_targets or OutputTarget.PUBLISH in expanded_targets else None,
            publish_result=publish_result,
        )
        manifest_path = self.manifest_writer.write(
            project_root=project_root,
            localized_project=localized_project,
            artifact_index=artifact_index,
            request=request,
            output_bundle=output_bundle,
        )
        artifact_index.register("manifest.root", manifest_path)
        output_bundle.manifest_path = manifest_path
        return output_bundle

    def _resolve_project_root(
        self,
        localized_project: LocalizedProject,
        artifact_index: ArtifactIndex,
        request: OutputRequest,
    ) -> Path:
        if request.output_dir:
            return Path(request.output_dir).resolve(strict=False)

        draft_dir = artifact_index.get("editor.draft_dir")
        if draft_dir:
            return Path(draft_dir).resolve(strict=False).parent

        source_path = localized_project.source_info.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            return Path(source_path).resolve(strict=False).parent / localized_project.project_id

        raise PublishError("Output dispatch requires request.output_dir or editor.draft_dir artifact.")

    def _build_editor_project_output(
        self,
        localized_project: LocalizedProject,
        artifact_index: ArtifactIndex,
        project_root: Path,
    ) -> ProjectOutput:
        source_info = localized_project.source_info
        metadata = source_info.get("metadata", {})
        title = localized_project.project_id
        if isinstance(metadata, dict):
            raw_title = metadata.get("video_title") or metadata.get("title")
            if isinstance(raw_title, str) and raw_title.strip():
                title = raw_title.strip()

        locator = source_info.get("locator")
        youtube_url = locator if isinstance(locator, str) else ""
        if not youtube_url:
            source_path = source_info.get("source_path")
            if isinstance(source_path, str):
                youtube_url = source_path

        return ProjectOutput(
            project_id=localized_project.project_id,
            youtube_url=youtube_url,
            video_title=title,
            total_duration_ms=self._compute_total_duration_ms(localized_project),
            segments=self._build_aligned_segments(localized_project),
            output_dir=str(project_root),
        )

    def _build_aligned_segments(self, localized_project: LocalizedProject) -> list[AlignedSegment]:
        segments: list[AlignedSegment] = []
        for index, block in enumerate(localized_project.aligned_blocks, start=1):
            audio_path = (block.aligned_audio_path or block.tts_audio_path or "").strip()
            if not audio_path:
                raise PublishError(f"Aligned block missing audio path: {block.block_id}")

            segment_id = self._resolve_segment_id(block, fallback=index)
            start_ms = int(block.first_start_ms)
            fallback_duration_ms = max(block.last_end_ms - block.first_start_ms, block.target_duration_ms, 0)
            actual_duration_ms = int(block.actual_audio_duration_ms or fallback_duration_ms)
            end_ms = max(int(block.last_end_ms), start_ms + actual_duration_ms)
            segments.append(
                AlignedSegment(
                    segment_id=segment_id,
                    speaker_id=block.speaker_id,
                    display_name=block.speaker_name or block.speaker_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    cn_text=self._resolve_block_text(block),
                    aligned_audio_path=audio_path,
                    actual_duration_ms=actual_duration_ms,
                    alignment_method=self._resolve_alignment_method(block),
                    needs_review=self._resolve_needs_review(block),
                )
            )
        return segments

    @staticmethod
    def _resolve_block_text(block: object) -> str:
        preferred_text = getattr(block, "get_preferred_cn_text_for_caption", None)
        if callable(preferred_text):
            resolved = preferred_text()
            if isinstance(resolved, str) and resolved.strip():
                return resolved

        merged_cn_text = getattr(block, "merged_cn_text", "")
        if isinstance(merged_cn_text, str):
            return merged_cn_text.strip()
        return ""

    @staticmethod
    def _resolve_segment_id(block: object, *, fallback: int) -> int:
        raw_segment_id = getattr(block, "segment_id", fallback)
        try:
            normalized_segment_id = int(raw_segment_id)
        except (TypeError, ValueError):
            return fallback
        return normalized_segment_id if normalized_segment_id > 0 else fallback

    @staticmethod
    def _resolve_alignment_method(block: object) -> str:
        explicit_method = getattr(block, "alignment_method", "")
        if isinstance(explicit_method, str) and explicit_method.strip():
            return explicit_method.strip()

        status = str(getattr(block, "status", "") or "").strip()
        if status == "align_done_fallback":
            return "force_dsp"
        return "direct"

    @staticmethod
    def _resolve_needs_review(block: object) -> bool:
        explicit_needs_review = getattr(block, "needs_review", None)
        if isinstance(explicit_needs_review, bool):
            return explicit_needs_review
        status = str(getattr(block, "status", "") or "").strip()
        return status != "align_done"

    @staticmethod
    def _compute_total_duration_ms(localized_project: LocalizedProject) -> int:
        duration_candidates = [caption.end_ms for caption in localized_project.captions]
        for block in localized_project.aligned_blocks:
            duration_candidates.append(max(block.last_end_ms, block.first_start_ms + int(block.actual_audio_duration_ms or 0)))
        metadata = localized_project.source_info.get("metadata", {})
        if isinstance(metadata, dict):
            raw_duration_ms = metadata.get("duration_ms")
            try:
                normalized_duration_ms = int(raw_duration_ms)
            except (TypeError, ValueError):
                normalized_duration_ms = 0
            if normalized_duration_ms > 0:
                duration_candidates.append(normalized_duration_ms)
        return max(duration_candidates, default=0)

    @staticmethod
    def _register_editor_artifacts(artifact_index: ArtifactIndex, result: ProjectOutputResult) -> None:
        artifact_index.register("editor.dubbed_audio_complete", result.dubbed_audio_path)
        artifact_index.register("editor.ambient_audio", result.ambient_audio_path)
        artifact_index.register("editor.subtitles", result.subtitles_path)
        artifact_index.register("editor.alignment_report", result.alignment_report_path)
        artifact_index.register("editor.segments_dir", result.segments_dir)

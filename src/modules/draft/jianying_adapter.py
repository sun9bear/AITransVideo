from dataclasses import dataclass

from modules.draft.export_schema import (
    JianyingExportMaterial,
    JianyingExportSegment,
    JianyingExportTimeline,
    JianyingExportTrack,
    JianyingLikeExport,
)
from modules.draft.schema import DraftProject


@dataclass(slots=True)
class JianyingAdapterConfig:
    adapter_version: str = "jianying_export_preview_v1"


class JianyingExportAdapter:
    """Map internal draft schema into a more Jianying-like export structure."""

    def __init__(self, config: JianyingAdapterConfig | None = None) -> None:
        self.config = config or JianyingAdapterConfig()

    def adapt(self, project: DraftProject) -> JianyingLikeExport:
        audio_tracks = [
            JianyingExportTrack(
                track_id=track.track_id,
                track_type="audio",
                display_name="Main Audio Track",
                layer_index=index - 1,
                segments=[
                    JianyingExportSegment(
                        segment_id=item.item_id,
                        track_id=track.track_id,
                        segment_type="audio",
                        block_id=item.block_id,
                        start_ms=item.start_ms,
                        duration_ms=item.duration_ms,
                        end_ms=item.end_ms,
                        material_id=item.material_id,
                        reserved={
                            "provider": "local_audio",
                            "future_fields": ["fade_in_ms", "fade_out_ms", "volume_envelope"],
                        },
                    )
                    for item in track.items
                ],
                reserved={"future_fields": ["jianying_track_uuid", "mix_mode"]},
            )
            for index, track in enumerate(project.timeline.audio_tracks, start=1)
        ]
        caption_tracks = [
            JianyingExportTrack(
                track_id=track.track_id,
                track_type="caption",
                display_name="Main Caption Track",
                layer_index=index - 1,
                segments=[
                    JianyingExportSegment(
                        segment_id=item.item_id,
                        track_id=track.track_id,
                        segment_type="caption",
                        block_id=item.block_id,
                        start_ms=item.start_ms,
                        duration_ms=item.end_ms - item.start_ms,
                        end_ms=item.end_ms,
                        caption_id=item.caption_id,
                        text=item.text,
                        source_srt_index=item.source_srt_index,
                        speaker_id=item.speaker_id,
                        reserved={
                            "speaker_name": item.speaker_name,
                            "future_fields": ["style_ref", "transform", "font_config"],
                        },
                    )
                    for item in track.items
                ],
                reserved={"future_fields": ["caption_style_track_id", "jianying_track_uuid"]},
            )
            for index, track in enumerate(project.timeline.caption_tracks, start=1)
        ]
        audio_materials = [
            JianyingExportMaterial(
                material_id=material.material_id,
                material_type="audio",
                block_id=material.block_id,
                relative_path=material.relative_material_path,
                source_path=material.source_audio_path,
                duration_ms=material.duration_ms,
                speaker_id=material.speaker_id,
                speaker_name=material.speaker_name,
                reserved={"future_fields": ["sample_rate", "channels", "jianying_material_id"]},
            )
            for material in project.materials
        ]

        return JianyingLikeExport(
            export_version=self.config.adapter_version,
            export_target="jianying_like_export",
            source_schema_version=project.schema_version,
            project_id=project.project_id,
            timeline=JianyingExportTimeline(
                duration_ms=project.timeline.duration_ms,
                audio_tracks=audio_tracks,
                caption_tracks=caption_tracks,
                video_tracks=[],
                style_tracks=[],
                effect_tracks=[],
            ),
            audio_materials=audio_materials,
            video_materials=[],
            style_materials=[],
            effect_materials=[],
            mapping_report={
                "mapped_fields": [
                    "project_id",
                    "source_schema_version",
                    "timeline.duration_ms",
                    "timeline.audio_tracks[].track_id",
                    "timeline.audio_tracks[].layer_index",
                    "timeline.audio_tracks[].segments[].material_id",
                    "timeline.audio_tracks[].segments[].block_id",
                    "timeline.audio_tracks[].segments[].start_ms",
                    "timeline.audio_tracks[].segments[].duration_ms",
                    "timeline.audio_tracks[].segments[].end_ms",
                    "timeline.caption_tracks[].track_id",
                    "timeline.caption_tracks[].segments[].text",
                    "timeline.caption_tracks[].segments[].start_ms",
                    "timeline.caption_tracks[].segments[].end_ms",
                    "timeline.caption_tracks[].segments[].speaker_id",
                    "materials.audio_materials[].relative_path",
                    "materials.audio_materials[].duration_ms",
                    "materials.audio_materials[].speaker_id",
                ],
                "unmapped_fields": [
                    "video_tracks",
                    "style_tracks",
                    "effect_tracks",
                    "style_refs",
                    "transition_graph",
                    "jianying_private_ids",
                    "asset_library_ids",
                    "template_bindings",
                    "render_settings",
                ],
                "assumptions": [
                    "One internal audio track maps to one Jianying-like audio track.",
                    "Caption segments use retimed internal subtitle spans directly.",
                    "Audio materials are local file references under draft/materials.",
                    "Track and segment identifiers are stable internal IDs, not real Jianying private UUIDs.",
                ],
                "future_mapping_plan": [
                    "Map internal track IDs to real Jianying private IDs after format capture.",
                    "Add style/effect/video adapters once verified field samples are available.",
                    "Split reserved metadata into dedicated provider and timeline compatibility sections.",
                    "Align materials and segment metadata with real importable Jianying manifests.",
                ],
                "track_summary": {
                    "audio_track_count": len(audio_tracks),
                    "caption_track_count": len(caption_tracks),
                    "video_track_count": 0,
                    "style_track_count": 0,
                    "effect_track_count": 0,
                },
                "material_summary": {
                    "audio_material_count": len(audio_materials),
                    "video_material_count": 0,
                    "style_material_count": 0,
                    "effect_material_count": 0,
                },
            },
            compatibility_notes=[
                "Sprint 4B Jianying-like export preview for compatibility review, not a real importable project.",
                "Audio and caption tracks are mapped from the validated internal draft schema with stable IDs.",
                "Track, segment, and material relationships are preserved to support manual field-by-field comparison.",
                "Video/style/effect structures remain reserved placeholders until real samples are collected.",
                "Timeline timing is derived from internal draft math rather than reverse-engineered Jianying private fields.",
                "Real Jianying compatibility still requires private ID mapping, style metadata, and importer-specific manifests.",
            ],
        )


class JianyingAdapterSkeleton(JianyingExportAdapter):
    """Backward-compatible alias for the upgraded export adapter."""

    pass

from core.exceptions import DraftError
from modules.draft.export_schema import (
    JianyingExportMaterial,
    JianyingExportSegment,
    JianyingExportTrack,
    JianyingLikeExport,
)


class JianyingExportValidator:
    """Validate Jianying-like export consistency."""

    def validate(self, export: JianyingLikeExport) -> dict[str, object]:
        audio_materials = self._index_materials(export.audio_materials, expected_type="audio")
        self._index_materials(export.video_materials, expected_type="video")
        self._index_materials(export.style_materials, expected_type="style")
        self._index_materials(export.effect_materials, expected_type="effect")

        max_end_ms = 0
        segment_count = 0
        seen_track_ids: set[str] = set()
        seen_segment_ids: set[str] = set()
        track_summaries: list[dict[str, object]] = []

        for track in export.timeline.audio_tracks:
            track_summaries.append(
                self._validate_track_segments(
                    track,
                    expected_type="audio",
                    timeline_duration_ms=export.timeline.duration_ms,
                    seen_track_ids=seen_track_ids,
                    seen_segment_ids=seen_segment_ids,
                    audio_materials=audio_materials,
                )
            )
            for segment in track.segments:
                max_end_ms = max(max_end_ms, segment.end_ms)
                segment_count += 1

        for track in export.timeline.caption_tracks:
            track_summaries.append(
                self._validate_track_segments(
                    track,
                    expected_type="caption",
                    timeline_duration_ms=export.timeline.duration_ms,
                    seen_track_ids=seen_track_ids,
                    seen_segment_ids=seen_segment_ids,
                    audio_materials=audio_materials,
                )
            )
            for segment in track.segments:
                max_end_ms = max(max_end_ms, segment.end_ms)
                segment_count += 1

        for track_group_name, expected_type, track_group in (
            ("video_tracks", "video", export.timeline.video_tracks),
            ("style_tracks", "style", export.timeline.style_tracks),
            ("effect_tracks", "effect", export.timeline.effect_tracks),
        ):
            for track in track_group:
                track_summary = self._validate_track_segments(
                    track,
                    expected_type=expected_type,
                    timeline_duration_ms=export.timeline.duration_ms,
                    seen_track_ids=seen_track_ids,
                    seen_segment_ids=seen_segment_ids,
                    audio_materials=audio_materials,
                )
                track_summary["group"] = track_group_name
                track_summaries.append(track_summary)
                for segment in track.segments:
                    max_end_ms = max(max_end_ms, segment.end_ms)
                    segment_count += 1

        if export.timeline.duration_ms != max_end_ms:
            raise DraftError("Jianying-like export timeline.duration_ms does not match segment spans.")

        return {
            "validation_status": "passed",
            "project_id": export.project_id,
            "timeline_duration_ms": export.timeline.duration_ms,
            "max_segment_end_ms": max_end_ms,
            "material_counts": {
                "audio": len(export.audio_materials),
                "video": len(export.video_materials),
                "style": len(export.style_materials),
                "effect": len(export.effect_materials),
            },
            "track_counts": {
                "audio": len(export.timeline.audio_tracks),
                "caption": len(export.timeline.caption_tracks),
                "video": len(export.timeline.video_tracks),
                "style": len(export.timeline.style_tracks),
                "effect": len(export.timeline.effect_tracks),
            },
            "segment_count": segment_count,
            "track_summaries": track_summaries,
            "checked_constraints": [
                "material identifiers are unique inside each material group",
                "track identifiers are unique across export timeline groups",
                "segment identifiers are unique across export timeline groups",
                "segment track_id matches parent track_id",
                "segment type matches parent track_type",
                "segment start/end/duration fields are internally consistent",
                "segment spans stay within timeline.duration_ms",
                "audio segments reference existing audio materials",
                "audio segment block_id matches referenced material block_id",
                "caption segments contain caption_id and non-empty text",
                "timeline.duration_ms matches the maximum segment end",
            ],
        }

    def _index_materials(
        self,
        materials: list[JianyingExportMaterial],
        expected_type: str,
    ) -> dict[str, JianyingExportMaterial]:
        indexed: dict[str, JianyingExportMaterial] = {}
        for material in materials:
            if material.material_id in indexed:
                raise DraftError(f"Duplicate material_id detected: {material.material_id}")
            if material.material_type != expected_type:
                raise DraftError(
                    f"Material type mismatch: {material.material_id} -> {material.material_type} != {expected_type}"
                )
            if material.duration_ms <= 0:
                raise DraftError(f"Material duration must be positive: {material.material_id}")
            indexed[material.material_id] = material
        return indexed

    def _validate_track_segments(
        self,
        track: JianyingExportTrack,
        expected_type: str,
        timeline_duration_ms: int,
        seen_track_ids: set[str],
        seen_segment_ids: set[str],
        audio_materials: dict[str, JianyingExportMaterial],
    ) -> dict[str, object]:
        if track.track_id in seen_track_ids:
            raise DraftError(f"Duplicate track_id detected: {track.track_id}")
        seen_track_ids.add(track.track_id)
        if track.track_type != expected_type:
            raise DraftError(f"Track type mismatch: {track.track_id} -> {track.track_type} != {expected_type}")

        track_max_end_ms = 0
        for segment in track.segments:
            self._validate_segment(
                track=track,
                segment=segment,
                timeline_duration_ms=timeline_duration_ms,
                seen_segment_ids=seen_segment_ids,
                audio_materials=audio_materials,
            )
            track_max_end_ms = max(track_max_end_ms, segment.end_ms)

        return {
            "track_id": track.track_id,
            "track_type": track.track_type,
            "layer_index": track.layer_index,
            "segment_count": len(track.segments),
            "max_end_ms": track_max_end_ms,
        }

    def _validate_segment(
        self,
        track: JianyingExportTrack,
        segment: JianyingExportSegment,
        timeline_duration_ms: int,
        seen_segment_ids: set[str],
        audio_materials: dict[str, JianyingExportMaterial],
    ) -> None:
        if segment.segment_id in seen_segment_ids:
            raise DraftError(f"Duplicate segment_id detected: {segment.segment_id}")
        seen_segment_ids.add(segment.segment_id)
        if segment.track_id != track.track_id:
            raise DraftError(
                f"Segment track reference mismatch: {segment.segment_id} -> {segment.track_id} != {track.track_id}"
            )
        if segment.segment_type != track.track_type:
            raise DraftError(
                f"Segment type does not match parent track: {segment.segment_id} -> {segment.segment_type}"
            )
        if segment.start_ms < 0:
            raise DraftError(f"Segment start must be non-negative: {segment.segment_id}")
        if segment.duration_ms <= 0:
            raise DraftError(f"Segment duration must be positive: {segment.segment_id}")
        if segment.end_ms != segment.start_ms + segment.duration_ms:
            raise DraftError(f"Segment duration mismatch: {segment.segment_id}")
        if segment.end_ms > timeline_duration_ms:
            raise DraftError(f"Segment exceeds timeline duration: {segment.segment_id}")

        if track.track_type == "audio":
            if segment.material_id is None or segment.material_id not in audio_materials:
                raise DraftError(f"Audio segment references missing material: {segment.segment_id}")
            material = audio_materials[segment.material_id]
            if material.block_id != segment.block_id:
                raise DraftError(
                    f"Audio segment block_id mismatch: {segment.segment_id} -> {segment.block_id} != {material.block_id}"
                )
        if track.track_type == "caption":
            if segment.caption_id is None:
                raise DraftError(f"Caption segment missing caption_id: {segment.segment_id}")
            if not isinstance(segment.text, str) or not segment.text.strip():
                raise DraftError(f"Caption segment missing text: {segment.segment_id}")

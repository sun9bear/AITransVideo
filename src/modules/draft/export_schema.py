from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class JianyingExportSegment:
    segment_id: str
    track_id: str
    segment_type: str
    block_id: str
    start_ms: int
    duration_ms: int
    end_ms: int
    material_id: str | None = None
    caption_id: str | None = None
    text: str | None = None
    source_srt_index: int | None = None
    speaker_id: str | None = None
    reserved: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class JianyingExportTrack:
    track_id: str
    track_type: str
    display_name: str
    layer_index: int
    segments: list[JianyingExportSegment] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class JianyingExportTimeline:
    duration_ms: int
    audio_tracks: list[JianyingExportTrack] = field(default_factory=list)
    caption_tracks: list[JianyingExportTrack] = field(default_factory=list)
    video_tracks: list[JianyingExportTrack] = field(default_factory=list)
    style_tracks: list[JianyingExportTrack] = field(default_factory=list)
    effect_tracks: list[JianyingExportTrack] = field(default_factory=list)


@dataclass(slots=True)
class JianyingExportMaterial:
    material_id: str
    material_type: str
    block_id: str
    relative_path: str
    source_path: str
    duration_ms: int
    speaker_id: str
    speaker_name: str | None
    reserved: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class JianyingLikeExport:
    export_version: str
    export_target: str
    source_schema_version: str
    project_id: str
    timeline: JianyingExportTimeline
    audio_materials: list[JianyingExportMaterial] = field(default_factory=list)
    video_materials: list[JianyingExportMaterial] = field(default_factory=list)
    style_materials: list[JianyingExportMaterial] = field(default_factory=list)
    effect_materials: list[JianyingExportMaterial] = field(default_factory=list)
    mapping_report: dict[str, object] = field(default_factory=dict)
    compatibility_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "export_version": self.export_version,
            "export_target": self.export_target,
            "source_schema_version": self.source_schema_version,
            "project_id": self.project_id,
            "timeline": asdict(self.timeline),
            "materials": {
                "audio_materials": [asdict(material) for material in self.audio_materials],
                "video_materials": [asdict(material) for material in self.video_materials],
                "style_materials": [asdict(material) for material in self.style_materials],
                "effect_materials": [asdict(material) for material in self.effect_materials],
            },
            "mapping_report": self.mapping_report,
            "compatibility_notes": self.compatibility_notes,
        }

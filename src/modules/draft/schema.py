from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class DraftAudioTrackItem:
    item_id: str
    material_id: str
    block_id: str
    start_ms: int
    duration_ms: int

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


@dataclass(slots=True)
class DraftCaptionItem:
    item_id: str
    caption_id: str
    block_id: str
    source_srt_index: int
    speaker_id: str
    speaker_name: str | None
    text: str
    start_ms: int
    end_ms: int


@dataclass(slots=True)
class DraftAudioTrack:
    track_id: str
    items: list[DraftAudioTrackItem] = field(default_factory=list)


@dataclass(slots=True)
class DraftCaptionTrack:
    track_id: str
    items: list[DraftCaptionItem] = field(default_factory=list)


@dataclass(slots=True)
class DraftTimeline:
    duration_ms: int
    audio_tracks: list[DraftAudioTrack] = field(default_factory=list)
    caption_tracks: list[DraftCaptionTrack] = field(default_factory=list)


@dataclass(slots=True)
class DraftMaterial:
    material_id: str
    block_id: str
    source_audio_path: str
    relative_material_path: str
    start_ms: int
    duration_ms: int
    speaker_id: str
    speaker_name: str | None


@dataclass(slots=True)
class DraftProject:
    schema_version: str
    project_id: str
    generated_at: str
    block_count: int
    caption_count: int
    material_count: int
    timeline: DraftTimeline
    materials: list[DraftMaterial] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stage_snapshot: dict[str, object] = field(default_factory=dict)

    def to_content_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "timeline": asdict(self.timeline),
            "materials": [asdict(material) for material in self.materials],
        }

    def to_meta_info_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "generated_at": self.generated_at,
            "summary": {
                "block_count": self.block_count,
                "caption_count": self.caption_count,
                "material_count": self.material_count,
            },
            "notes": self.notes,
            "stages": self.stage_snapshot,
        }

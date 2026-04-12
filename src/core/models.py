from dataclasses import dataclass, field

from core.enums import BlockStatus


@dataclass(slots=True)
class SubtitleLine:
    index: int
    start_ms: int
    end_ms: int
    speaker_id: str
    speaker_name: str | None
    en_text: str
    cn_text: str

    def __post_init__(self) -> None:
        self.speaker_id = self.speaker_id.strip()
        self.speaker_name = self.speaker_name.strip() if isinstance(self.speaker_name, str) else None
        self.speaker_name = self.speaker_name or None
        self.en_text = self.en_text.strip()
        self.cn_text = self.cn_text.strip()


@dataclass(slots=True)
class SemanticBlock:
    """Paragraph-level dubbing unit."""

    block_id: str
    speaker_id: str
    speaker_name: str | None
    original_srt_indices: list[int]
    first_start_ms: int
    last_end_ms: int
    target_duration_ms: int
    merged_cn_text: str
    cn_line_texts: list[str] = field(default_factory=list)
    actual_audio_duration_ms: int = 0
    rewrite_count: int = 0
    tts_audio_path: str | None = None
    aligned_audio_path: str | None = None
    final_cn_lines: list[str] = field(default_factory=list)
    status: str = BlockStatus.PENDING.value
    error_message: str | None = None
    error_type: str | None = None
    alignment_method: str = "direct"
    needs_review: bool = False

    def __post_init__(self) -> None:
        self.merged_cn_text = self.merged_cn_text.strip()
        self.cn_line_texts = [text.strip() for text in self.cn_line_texts]
        self.final_cn_lines = [text.strip() for text in self.final_cn_lines]

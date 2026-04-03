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
    literal_cn_text: str = ""
    tts_cn_text: str = ""

    def __post_init__(self) -> None:
        self.speaker_id = self.speaker_id.strip()
        self.speaker_name = self.speaker_name.strip() if isinstance(self.speaker_name, str) else None
        self.speaker_name = self.speaker_name or None
        self.en_text = self.en_text.strip()
        self.cn_text = self.cn_text.strip()
        self.literal_cn_text = self.literal_cn_text.strip()
        self.tts_cn_text = self.tts_cn_text.strip()

        if not self.literal_cn_text and self.cn_text:
            self.literal_cn_text = self.cn_text
        if not self.cn_text:
            self.cn_text = self.literal_cn_text or self.tts_cn_text

    def get_literal_cn_text(self) -> str:
        """Return the faithful translation layer during the migration from cn_text."""

        return self.literal_cn_text or self.cn_text

    def has_literal_cn_layer(self) -> bool:
        return bool(self.get_literal_cn_text().strip())

    def has_tts_cn_layer(self) -> bool:
        return bool(self.tts_cn_text.strip())

    def get_preferred_cn_text_for_tts(self) -> str:
        """Resolve downstream spoken-text consumption during the transition period."""

        return self.tts_cn_text or self.literal_cn_text or self.cn_text

    def get_preferred_cn_text_for_caption(self) -> str:
        """Resolve caption text so downstream captions follow spoken text first."""

        return self.tts_cn_text or self.literal_cn_text or self.cn_text


@dataclass(slots=True)
class SemanticBlock:
    """Paragraph-level dubbing unit.

    `merged_cn_text` remains the transitional compatibility field consumed
    downstream. During the migration it mirrors the selected block text with
    priority:
    merged_tts_cn_text -> merged_literal_cn_text -> merged_cn_text.
    """

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
    merged_literal_cn_text: str = ""
    merged_tts_cn_text: str = ""
    alignment_method: str = "direct"
    needs_review: bool = False

    def __post_init__(self) -> None:
        self.merged_cn_text = self.merged_cn_text.strip()
        self.merged_literal_cn_text = self.merged_literal_cn_text.strip()
        self.merged_tts_cn_text = self.merged_tts_cn_text.strip()
        self.cn_line_texts = [text.strip() for text in self.cn_line_texts]
        self.final_cn_lines = [text.strip() for text in self.final_cn_lines]

        if not self.merged_literal_cn_text and self.merged_cn_text:
            self.merged_literal_cn_text = self.merged_cn_text
        if not self.merged_cn_text:
            self.merged_cn_text = self.get_preferred_cn_text_for_tts()

    def get_literal_cn_text(self) -> str:
        """Return the faithful block-level Chinese layer during migration."""

        return self.merged_literal_cn_text or self.merged_cn_text

    def has_literal_cn_layer(self) -> bool:
        return bool(self.get_literal_cn_text().strip())

    def has_tts_cn_layer(self) -> bool:
        return bool(self.merged_tts_cn_text.strip())

    def get_preferred_cn_text_for_tts(self) -> str:
        """Resolve block-level spoken Chinese text during the transition."""

        return self.merged_tts_cn_text or self.merged_literal_cn_text or self.merged_cn_text

    def get_preferred_cn_text_for_caption(self) -> str:
        """Resolve caption-facing block text with the same spoken-text priority."""

        return self.merged_tts_cn_text or self.merged_literal_cn_text or self.merged_cn_text


def summarize_subtitle_text_layers(lines: list[SubtitleLine]) -> dict[str, int]:
    return {
        "literal_line_count": sum(1 for line in lines if line.has_literal_cn_layer()),
        "tts_line_count": sum(1 for line in lines if line.has_tts_cn_layer()),
        "compat_line_count": sum(1 for line in lines if bool(line.cn_text.strip())),
    }


def summarize_block_text_layers(blocks: list[SemanticBlock]) -> dict[str, int]:
    return {
        "literal_block_count": sum(1 for block in blocks if block.has_literal_cn_layer()),
        "tts_block_count": sum(1 for block in blocks if block.has_tts_cn_layer()),
        "compat_block_count": sum(1 for block in blocks if bool(block.merged_cn_text.strip())),
    }

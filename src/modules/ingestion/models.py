from dataclasses import dataclass


@dataclass(slots=True)
class SubtitleSeed:
    """Raw subtitle input before normalization into SubtitleLine."""

    start_ms: int
    end_ms: int
    en_text: str
    speaker_id: str
    speaker_name: str | None = None
    cn_text: str = ""
    index: int | None = None

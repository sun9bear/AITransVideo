from enum import Enum


class BlockStatus(str, Enum):
    """Lifecycle states for a semantic block."""

    PENDING = "pending"
    TTS_DONE = "tts_done"
    ALIGN_DONE = "align_done"
    ALIGN_DONE_FALLBACK = "align_done_fallback"
    FAILED = "failed"


class StageStatus(str, Enum):
    """Structured lifecycle states for pipeline stages."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class OutputTarget(str, Enum):
    """Top-level output routing modes for the post-build dispatch step."""

    PUBLISH = "publish"
    EDITOR = "editor"
    BOTH = "both"

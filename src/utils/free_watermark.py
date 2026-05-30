"""Phase 2a Task 8 (gate #8) — free-tier video watermark (ffmpeg drawtext).

Minimal implementation: a configurable brand text burned into
``publish.dubbed_video`` for **free service-mode jobs only** (paid modes ship a
clean video). Text / fontfile / position default to the constants here and are
env-overridable; an admin-config UI is deferred to Phase 2b.

Deployment note: ffmpeg ``drawtext`` needs a usable font. ``AVT_WATERMARK_FONTFILE``
should point to a ``.ttf`` present in the app container so libfreetype reads it
directly (no fontconfig dependency); if empty, drawtext relies on the system
fontconfig default. A CJK watermark text requires a CJK font file — keep the
default ASCII unless a CJK font is provisioned.
"""
from __future__ import annotations

import os

# Brand text burned into free videos. ASCII default so it renders without a CJK
# font; override via env (pair a CJK text with AVT_WATERMARK_FONTFILE).
FREE_WATERMARK_TEXT = (os.environ.get("AVT_FREE_WATERMARK_TEXT") or "AIVideoTrans").strip()
# Optional explicit font file path (recommended in containers). Empty -> rely on
# the ffmpeg/fontconfig default.
WATERMARK_FONTFILE = (os.environ.get("AVT_WATERMARK_FONTFILE") or "").strip()

WATERMARK_FONTSIZE = 24
WATERMARK_FONTCOLOR = "white@0.85"
WATERMARK_BOXCOLOR = "black@0.35"


def free_watermark_text_for(service_mode: str | None) -> str | None:
    """Return the watermark text for a job, or ``None`` for no watermark.

    Only ``service_mode == "free"`` is watermarked; paid modes ship clean video.
    Single source of truth for the free -> watermark policy.
    """
    if (service_mode or "").strip() == "free" and FREE_WATERMARK_TEXT:
        return FREE_WATERMARK_TEXT
    return None


def _escape_drawtext(value: str) -> str:
    """Escape characters special to an ffmpeg drawtext option value."""
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def build_drawtext_filter(
    text: str,
    *,
    fontfile: str = "",
    fontsize: int = WATERMARK_FONTSIZE,
    fontcolor: str = WATERMARK_FONTCOLOR,
    boxcolor: str = WATERMARK_BOXCOLOR,
) -> str:
    """Build an ffmpeg ``drawtext`` filter: bottom-right, boxed, padded.

    When ``fontfile`` is given it is embedded so libfreetype reads the font
    directly (no fontconfig requirement). Position ``x=w-tw-20 / y=h-th-20`` =
    bottom-right with 20px padding.
    """
    parts = [f"text='{_escape_drawtext(text)}'"]
    if fontfile.strip():
        parts.append(f"fontfile='{_escape_drawtext(fontfile.strip())}'")
    parts.extend(
        [
            f"fontsize={int(fontsize)}",
            f"fontcolor={fontcolor}",
            "box=1",
            f"boxcolor={boxcolor}",
            "boxborderw=8",
            "x=w-tw-20",
            "y=h-th-20",
        ]
    )
    return "drawtext=" + ":".join(parts)


__all__ = [
    "FREE_WATERMARK_TEXT",
    "WATERMARK_FONTFILE",
    "free_watermark_text_for",
    "build_drawtext_filter",
]

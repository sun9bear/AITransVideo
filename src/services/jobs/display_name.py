"""Auto-generate user-friendly job ``display_name`` values.

Implements decision D10 + M2 of the post-edit plan
(``docs/plans/2026-04-18-studio-post-edit-plan.md`` §6.2).

Decision tree:

1. **YouTube source with non-empty title**
   truncate(title, width=24) → collision suffix if needed

2. **YouTube source with empty / missing title**
   (private / deleted / 401 / yt-dlp fallback) — fall through to branch 4

3. **Local upload with a filename (non-empty, non-whitespace)**
   truncate(os.path.splitext(filename)[0], width=24) → collision suffix if needed

4. **Local upload with no filename**
   ``"上传视频 YYYY-MM-DD NNN"`` — NNN is a per-user, per-day, zero-padded
   counter; no collision suffix needed in practice.

Widths are counted in display units (CJK = 2, ASCII = 1). See ``src/utils/text_width.py``.

Collision handling: if the candidate name is already in ``existing_names``
(the caller's "this user's current display_names"), append ``_xxxx`` —
four lowercase alphanumerics — and retry. Up to ``MAX_RETRIES`` attempts.
If still colliding after that, return the last candidate anyway; callers
should treat this as "vanishingly unlikely" and not loop forever.

This module is pure-logic: no DB, no I/O, no side effects. Callers (Gateway
handlers or the migration backfill script) own ``existing_names`` lookup and
the ``NNN`` counter.
"""

from __future__ import annotations

import os
import random
import string
from dataclasses import dataclass
from datetime import date

from src.utils.text_width import truncate_to_width

__all__ = [
    "DisplayNameInput",
    "generate_display_name",
    "resolve_collision",
    "DEFAULT_TITLE_WIDTH",
    "MAX_RETRIES",
]


# Width budget for the main title portion (pre-collision-suffix).
# 12 CJK chars ≈ 24 display units; see plan §6.1.
DEFAULT_TITLE_WIDTH = 24

# Max total width including collision suffix (title + "_xxxx" = +5). Callers
# that want to enforce this can compare ``display_width(result) <= MAX_TOTAL_WIDTH``;
# we do NOT truncate past this to guarantee uniqueness (see module docstring).
MAX_TOTAL_WIDTH = 29

MAX_RETRIES = 5
_SUFFIX_CHARS = string.ascii_lowercase + string.digits
_SUFFIX_LEN = 4


@dataclass(slots=True, frozen=True)
class DisplayNameInput:
    """All the signals needed to pick a display name.

    ``user_local_date`` is the user's local date (YYYY-MM-DD). The caller
    decides the timezone; this module does not guess UTC vs local.

    ``upload_sequence_today`` is the 1-based count of "no-title" uploads this
    user has made today. Used only by branch 4. The caller increments and
    supplies it atomically (e.g. under a DB transaction).
    """

    source_type: str                           # "youtube_url" | "local_video"
    source_ref: str                            # URL or filename / empty
    youtube_title: str | None = None           # from yt-dlp; may be None/empty
    local_filename: str | None = None          # original upload filename; may be None
    user_local_date: date | None = None        # required for branch 4
    upload_sequence_today: int | None = None   # required for branch 4


def generate_display_name(
    inp: DisplayNameInput,
    existing_names: set[str],
    *,
    width: int = DEFAULT_TITLE_WIDTH,
    rng: random.Random | None = None,
) -> str:
    """Produce a display_name honouring the four-branch decision tree.

    Always returns a non-empty string. If all collision retries fail, returns
    the last candidate (with a suffix already appended) — the calling code
    should log a warning but still accept it.

    ``rng`` lets tests inject a seeded ``random.Random`` for determinism.
    """
    base = _pick_base_name(inp, width=width)
    return resolve_collision(base, existing_names, rng=rng)


def resolve_collision(
    base: str,
    existing_names: set[str],
    *,
    rng: random.Random | None = None,
    max_retries: int = MAX_RETRIES,
) -> str:
    """Return ``base`` if unused; else ``base_xxxx`` with a fresh random suffix.

    If all ``max_retries`` attempts collide, returns the last suffixed candidate
    anyway (vanishingly unlikely with 36^4 ≈ 1.7M space per prefix).
    """
    if base not in existing_names:
        return base
    picker = rng if rng is not None else random.SystemRandom()
    last_candidate = base
    for _ in range(max_retries):
        suffix = "".join(picker.choice(_SUFFIX_CHARS) for _ in range(_SUFFIX_LEN))
        candidate = f"{base}_{suffix}"
        if candidate not in existing_names:
            return candidate
        last_candidate = candidate
    return last_candidate


def _pick_base_name(inp: DisplayNameInput, *, width: int) -> str:
    source_type = (inp.source_type or "").strip().lower()

    # Branch 1+2: YouTube source
    if source_type == "youtube_url":
        title = (inp.youtube_title or "").strip()
        if title:
            truncated = truncate_to_width(title, width)
            if truncated:
                return truncated
        # Empty / blank title → fall through to branch 4
        return _branch_4_default(inp)

    # Branch 3: local upload with filename
    if source_type == "local_video":
        filename = (inp.local_filename or "").strip()
        if filename:
            stem, _ = os.path.splitext(filename)
            stem = stem.strip()
            if stem:
                truncated = truncate_to_width(stem, width)
                if truncated:
                    return truncated
        # No filename → branch 4
        return _branch_4_default(inp)

    # Unknown source_type: defensive default
    return _branch_4_default(inp)


def _branch_4_default(inp: DisplayNameInput) -> str:
    """``上传视频 YYYY-MM-DD NNN`` — counter-based, no truncation needed."""
    local_date = inp.user_local_date
    sequence = inp.upload_sequence_today
    if local_date is None or sequence is None:
        # Callers MUST supply these for branch 4. Raising here makes the
        # contract explicit; silent fallback to "job_id" would mask bugs.
        raise ValueError(
            "Branch 4 (no-title default) requires user_local_date + "
            "upload_sequence_today; caller did not supply them"
        )
    date_str = local_date.strftime("%Y-%m-%d")
    seq_str = f"{sequence:03d}"
    return f"上传视频 {date_str} {seq_str}"

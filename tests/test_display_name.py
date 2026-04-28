"""Unit tests for src/services/jobs/display_name.py.

Covers the 4-branch decision tree + collision suffix + edge cases.
All tests pure-logic — no DB, no FS, no time.monotonic.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from src.services.jobs.display_name import (
    DEFAULT_TITLE_WIDTH,
    DisplayNameInput,
    MAX_RETRIES,
    generate_display_name,
    resolve_collision,
)
from src.utils.text_width import display_width


# --- Branch 1: YouTube placeholder ----------------------------------------


def test_youtube_uses_chinese_placeholder_even_when_title_exists() -> None:
    inp = DisplayNameInput(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=abc",
        youtube_title="Short Title",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=1,
    )
    assert generate_display_name(inp, set()) == "油管视频 2026-04-18 001"


def test_youtube_placeholder_ignores_long_title() -> None:
    long_title = "这是一个非常非常非常非常非常长的视频标题"  # much > 24 units
    inp = DisplayNameInput(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=abc",
        youtube_title=long_title,
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=2,
    )
    assert generate_display_name(inp, set()) == "油管视频 2026-04-18 002"


def test_youtube_placeholder_pads_sequence() -> None:
    inp = DisplayNameInput(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=abc",
        youtube_title="你好世界12345abc",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=7,
    )
    assert generate_display_name(inp, set()) == "油管视频 2026-04-18 007"


# --- Branch 2: YouTube + empty title → falls through to Branch 4 ----------


def test_youtube_empty_title_falls_through_to_branch_4() -> None:
    inp = DisplayNameInput(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=deleted",
        youtube_title="",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=7,
    )
    assert generate_display_name(inp, set()) == "油管视频 2026-04-18 007"


def test_youtube_whitespace_only_title_falls_through() -> None:
    inp = DisplayNameInput(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=x",
        youtube_title="   \t\n  ",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=1,
    )
    assert generate_display_name(inp, set()) == "油管视频 2026-04-18 001"


def test_youtube_none_title_falls_through() -> None:
    inp = DisplayNameInput(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=x",
        youtube_title=None,
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=42,
    )
    assert generate_display_name(inp, set()) == "油管视频 2026-04-18 042"


# --- Branch 3: local upload with filename ---------------------------------


def test_local_english_filename_falls_through_to_branch_4() -> None:
    inp = DisplayNameInput(
        source_type="local_video",
        source_ref="local://upload",
        local_filename="my_video.mp4",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=3,
    )
    assert generate_display_name(inp, set()) == "上传视频 2026-04-18 003"


def test_local_with_chinese_filename() -> None:
    inp = DisplayNameInput(
        source_type="local_video",
        source_ref="local://upload",
        local_filename="采访录像.mov",
    )
    assert generate_display_name(inp, set()) == "采访录像"


def test_local_long_filename_truncated() -> None:
    inp = DisplayNameInput(
        source_type="local_video",
        source_ref="local://upload",
        local_filename="这是一个非常非常非常非常非常长的视频文件名.mp4",
    )
    result = generate_display_name(inp, set())
    assert display_width(result) <= DEFAULT_TITLE_WIDTH


def test_local_dotfile_without_chinese_name_falls_back_to_branch_4() -> None:
    inp = DisplayNameInput(
        source_type="local_video",
        source_ref="local://upload",
        local_filename=".hidden",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=4,
    )
    assert generate_display_name(inp, set()) == "上传视频 2026-04-18 004"


# --- Branch 4: no filename ------------------------------------------------


def test_local_empty_filename_falls_through_to_branch_4() -> None:
    inp = DisplayNameInput(
        source_type="local_video",
        source_ref="local://upload",
        local_filename="",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=5,
    )
    assert generate_display_name(inp, set()) == "上传视频 2026-04-18 005"


def test_branch_4_requires_date_and_sequence() -> None:
    inp = DisplayNameInput(
        source_type="local_video",
        source_ref="local://upload",
        local_filename=None,
    )
    with pytest.raises(ValueError, match="user_local_date"):
        generate_display_name(inp, set())


def test_branch_4_pads_sequence_to_three_digits() -> None:
    assert generate_display_name(
        DisplayNameInput(
            source_type="local_video",
            source_ref="local://upload",
            local_filename=None,
            user_local_date=date(2026, 4, 18),
            upload_sequence_today=1,
        ),
        set(),
    ) == "上传视频 2026-04-18 001"
    assert generate_display_name(
        DisplayNameInput(
            source_type="local_video",
            source_ref="local://upload",
            local_filename=None,
            user_local_date=date(2026, 4, 18),
            upload_sequence_today=999,
        ),
        set(),
    ) == "上传视频 2026-04-18 999"


# --- Collision handling ---------------------------------------------------


def test_no_collision_returns_base_unchanged() -> None:
    assert resolve_collision("foo", {"bar", "baz"}) == "foo"


def test_collision_appends_suffix() -> None:
    rng = random.Random(42)
    result = resolve_collision("foo", {"foo"}, rng=rng)
    assert result.startswith("foo_")
    assert len(result) == len("foo_") + 4
    # Suffix must be lowercase alnum
    suffix = result[len("foo_"):]
    assert suffix.isalnum() and suffix.islower()


def test_collision_retries_until_unique() -> None:
    # Saturate first 3 retry attempts; the 4th succeeds.
    rng = random.Random(42)
    first_three = []
    scout = random.Random(42)
    for _ in range(3):
        first_three.append(f"foo_{''.join(scout.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(4))}")
    existing = {"foo"} | set(first_three)
    result = resolve_collision("foo", existing, rng=rng)
    assert result not in existing
    assert result.startswith("foo_")


def test_collision_returns_last_candidate_when_all_retries_fail() -> None:
    # If every generated suffix collides, still return something (not a crash).
    # Simulate by making every candidate already exist.

    class AlwaysCollidingSet(set):  # type: ignore[type-arg]
        def __contains__(self, item: object) -> bool:  # noqa: D401
            return True

    result = resolve_collision(
        "foo",
        AlwaysCollidingSet(),
        rng=random.Random(1),
        max_retries=MAX_RETRIES,
    )
    assert result.startswith("foo_")


# --- End-to-end via generate_display_name ---------------------------------


def test_youtube_placeholder_collides_with_existing_gets_suffix() -> None:
    inp = DisplayNameInput(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=abc",
        youtube_title="Shared Name",
        user_local_date=date(2026, 4, 18),
        upload_sequence_today=1,
    )
    rng = random.Random(7)
    result = generate_display_name(inp, {"油管视频 2026-04-18 001"}, rng=rng)
    assert result.startswith("油管视频 2026-04-18 001_")
    assert result != "油管视频 2026-04-18 001"

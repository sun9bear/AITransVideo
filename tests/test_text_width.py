"""Unit tests for src/utils/text_width.py.

Parity fixtures below are also consumed by the frontend TypeScript tests
(``frontend-next/src/lib/text/width.test.ts``) to ensure both implementations
agree character-for-character.
"""

from __future__ import annotations

import pytest

from src.utils.text_width import display_width, truncate_to_width


# --- display_width ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", 0),
        ("a", 1),
        ("hello", 5),
        ("你", 2),
        ("你好", 4),
        ("hi你好", 6),
        ("A中B日C韩", 9),
        # Fullwidth forms
        ("ＡＢＣ", 6),
        # Hiragana / Katakana
        ("あいう", 6),
        ("カナ", 4),
        # Hangul syllable
        ("한국", 4),
        # Combining marks contribute 0 — "é" as "e" + combining acute = 1
        ("e\u0301", 1),
        # Zero-width joiner contributes 0
        ("a\u200bb", 2),
    ],
)
def test_display_width_basic(text: str, expected: int) -> None:
    assert display_width(text) == expected


def test_display_width_empty_returns_zero() -> None:
    assert display_width("") == 0


# --- truncate_to_width -----------------------------------------------------


@pytest.mark.parametrize(
    "text, max_w, expected",
    [
        ("", 10, ""),
        ("hello", 0, ""),
        ("hello", -1, ""),
        ("hello", 100, "hello"),
        ("hello world", 5, "hello"),
        # CJK: exact width cutoff
        ("你好世界", 4, "你好"),
        ("你好世界", 5, "你好"),  # 5 can't fit a 3rd CJK char (would be 6)
        ("你好世界", 6, "你好世"),
        # Mixed
        ("hi你好world", 4, "hi你"),
        ("hi你好world", 5, "hi你"),  # next char (好) would push to 6
        ("hi你好world", 6, "hi你好"),
        # Fullwidth
        ("ＡＢＣ", 4, "ＡＢ"),
        # Budget < first char width — returns empty
        ("你hello", 1, ""),
    ],
)
def test_truncate_preserves_char_boundaries(text: str, max_w: int, expected: str) -> None:
    assert truncate_to_width(text, max_w) == expected


def test_truncate_never_exceeds_max_width() -> None:
    """No matter the input, the result's display_width must be <= max_w."""
    samples = [
        "",
        "hello world",
        "你好世界",
        "hi你好",
        "ＡＢＣＤＥＦ",
        "全部是汉字",
        "mix中文and English并且还有emoji",
    ]
    for s in samples:
        for max_w in range(0, 30):
            result = truncate_to_width(s, max_w)
            assert display_width(result) <= max_w, (
                f"truncate({s!r}, {max_w}) = {result!r} "
                f"has display_width {display_width(result)} > {max_w}"
            )


def test_truncate_is_prefix() -> None:
    """The result must always be a prefix (by character, not bytes) of input."""
    samples = ["hello", "你好世界", "hi你好"]
    for s in samples:
        for max_w in range(0, len(s) * 2 + 2):
            result = truncate_to_width(s, max_w)
            assert s.startswith(result), f"truncate({s!r}, {max_w}) = {result!r} is not a prefix"

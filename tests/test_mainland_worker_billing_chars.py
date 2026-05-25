"""billing_character_count 测试（Phase 4.0b §B）。

DashScope CosyVoice 计费规则（plan §结论摘要）：
- 1 个汉字（CJK Unified Ideographs, U+4E00-U+9FFF）= 2 个 API 字符
- 其它 = 1 个 API 字符

Phase 4.0a Observation Log（Codex 2026-05-25）锁定的参考用例：
- ``"你好"`` → 4
- ``"你好，这是一次计费字符观测。"``（len=14）：12 个汉字 × 2 + 2 个中文标点 × 1
  = **26**。本实现与 Codex Observation 值完全一致。
"""
from __future__ import annotations

import pytest

from services.mainland_worker.billing_chars import (
    billing_character_count,
    is_cjk_ideograph,
)


# ---------------------------------------------------------------------------
# 基础规则
# ---------------------------------------------------------------------------

def test_empty_string_returns_zero() -> None:
    assert billing_character_count("") == 0


def test_pure_ascii_each_char_counts_one() -> None:
    assert billing_character_count("hello") == 5
    assert billing_character_count("HelloWorld!") == 11


def test_two_chinese_chars_count_four() -> None:
    """plan §Phase 4.0b 通过标准锁定的关键断言。"""
    assert billing_character_count("你好") == 4


def test_mixed_chinese_english() -> None:
    # "你好world" = 2 CJK + 5 ASCII = 4 + 5 = 9
    assert billing_character_count("你好world") == 9


def test_single_cjk_char() -> None:
    assert billing_character_count("中") == 2


def test_single_ascii_char() -> None:
    assert billing_character_count("a") == 1


# ---------------------------------------------------------------------------
# 边界字符
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ch, expected", [
    # 数字
    ("0", 1),
    ("9", 1),
    # ASCII 标点
    (".", 1),
    (",", 1),
    ("!", 1),
    # 空格 / 换行
    (" ", 1),
    ("\t", 1),
    ("\n", 1),
    # 中文标点（不在 CJK Unified Ideographs 范围）
    ("，", 1),  # U+FF0C Fullwidth Comma
    ("。", 1),  # U+3002 Ideographic Full Stop
    ("？", 1),  # U+FF1F Fullwidth Question Mark
    # CJK Han 范围内
    ("一", 2),  # U+4E00
    ("龥", 2),  # U+9FA5
    # Latin Extended
    ("é", 1),
    ("ü", 1),
    # Emoji（不在 CJK Han 范围）
    ("😀", 1),
])
def test_boundary_chars(ch: str, expected: int) -> None:
    assert billing_character_count(ch) == expected


def test_long_chinese_text_matches_codex_observation() -> None:
    """Phase 4.0a Observation Log（Codex 2026-05-25）锁定的参考用例。

    ``"你好，这是一次计费字符观测。"`` 拆解：

    - 12 个 CJK 汉字：你-好-这-是-一-次-计-费-字-符-观-测 → 12 × 2 = 24
    - 1 个 fullwidth 逗号（U+FF0C）→ 1
    - 1 个 ideographic 句号（U+3002）→ 1
    - 合计：**26**

    Codex Observation Log 的 26 数值与本实现完全一致——汉字按 2、其它
    按 1 的规则。这条测试同时锁定 Codex 4.0a 实测结果和本函数实现，
    任一漂移立刻红。
    """
    text = "你好，这是一次计费字符观测。"
    assert len(text) == 14
    assert billing_character_count(text) == 26


# ---------------------------------------------------------------------------
# is_cjk_ideograph
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ch, expected", [
    ("一", True),
    ("中", True),
    ("龥", True),  # U+9FA5 — within range
    ("a", False),
    ("，", False),  # fullwidth comma in halfwidth/fullwidth forms block
    ("。", False),  # ideographic full stop in CJK Symbols and Punctuation
])
def test_is_cjk_ideograph(ch: str, expected: bool) -> None:
    assert is_cjk_ideograph(ch) is expected


def test_is_cjk_ideograph_rejects_multi_char() -> None:
    with pytest.raises(ValueError):
        is_cjk_ideograph("你好")

"""DashScope CosyVoice 计费字符计算（Phase 4.0b）。

plan §Phase 4.0a Observation Log（Codex 2026-05-25）实测结论：
DashScope SDK live response **不暴露** `usage.characters`，所以无法
按路径 A（SDK usage）取真实计费字符数。改走路径 B：本地实现与官方
计费口径一致的 `billing_character_count()`。

阿里云 DashScope 计费规则（plan §结论摘要）：

- **1 个汉字 = 2 个 API 计费字符**
- ASCII / 标点 / 其他字符 = 1 个 API 计费字符

"汉字"的判定范围：CJK Unified Ideographs（U+4E00–U+9FFF）。

Codex 4.0a Observation Log 给的参考值：``"你好，这是一次计费字符观测。"``
``len(text)=14``，``billing_character_count = 26``。本实现按 plan 字面规则
（CJK 汉字 = 2，其它 = 1）算出**也是 26**：

- 12 个 CJK 汉字（你/好/这/是/一/次/计/费/字/符/观/测）× 2 = 24
- 中文逗号 ``，``（U+FF0C，不在 CJK Han 范围）× 1 = 1
- 中文句号 ``。``（U+3002，不在 CJK Han 范围）× 1 = 1
- 合计：**26** ✓

实现与 Codex Observation Log 完全一致；测试 ``test_long_chinese_text_matches_codex_observation``
锁定这条断言。

**Phase 4.1 行动项**（Open Question）：首次真实账单出来后，对账层比对
DashScope 实际计费字符与本函数输出的偏差。如偏差 > 5%，可能需要纳入
CJK Extension A/B / compatibility ideographs（Codex 2026-05-25 P2 建议）
或调整标点权重。
"""
from __future__ import annotations


# CJK Unified Ideographs block — 严格意义上的"汉字"，不含 CJK 标点 / 符号
_CJK_IDEOGRAPH_START = 0x4E00
_CJK_IDEOGRAPH_END = 0x9FFF

# 计费倍率
_CJK_WEIGHT = 2
_OTHER_WEIGHT = 1


def billing_character_count(text: str) -> int:
    """根据 DashScope 官方计费口径计算计费字符数。

    规则（plan §结论摘要）：

    - 汉字（CJK Unified Ideographs, U+4E00-U+9FFF）按 **2 个** API 字符
    - 其它任意 Unicode 字符（ASCII / 标点 / Emoji / Latin Extended 等）
      按 **1 个** API 字符

    Parameters
    ----------
    text : str
        待合成的中文 / 多语种文本。空字符串返 0。

    Returns
    -------
    int
        计费字符总数。

    Examples
    --------
    >>> billing_character_count("你好")
    4
    >>> billing_character_count("hello")
    5
    >>> billing_character_count("你好world")
    9
    >>> billing_character_count("")
    0
    """
    total = 0
    for ch in text:
        cp = ord(ch)
        if _CJK_IDEOGRAPH_START <= cp <= _CJK_IDEOGRAPH_END:
            total += _CJK_WEIGHT
        else:
            total += _OTHER_WEIGHT
    return total


def is_cjk_ideograph(ch: str) -> bool:
    """单字符判定是否为 CJK Unified Ideograph（用于守卫测试 / 调试）。"""
    if len(ch) != 1:
        raise ValueError(f"expected single char, got {len(ch)}")
    return _CJK_IDEOGRAPH_START <= ord(ch) <= _CJK_IDEOGRAPH_END

"""Standard calibration texts for voice speed pre-calibration.

Three Chinese oral-speech texts of different lengths, scenes and emotions.
Used by `calibrate_voice_speeds.py` to measure each voice's chars/sec.

Hanzi counts (excluding punctuation/whitespace):
  - T1: 101 汉字 (科技评测, 好奇→兴奋→惊叹)
  - T2: 153 汉字 (纪录片旁白, 平静→紧张→悲伤→温暖)
  - T3: 204 汉字 (创业演讲, 低落→犹豫→惊喜→坚定→幽默)
  Total: 458 汉字 + 48 标点 = 506 总字符

Each voice is calibrated by synthesizing all three texts, summing
the spoken-char counts and durations, then computing:
  chars_per_second = total_hanzi / (total_duration_ms / 1000)

Three texts instead of one reduces content-specific speed bias:
- T1 short sentences / tech density
- T2 medium narrative / emotion shifts
- T3 long colloquial / dialogue embedded

Do NOT edit these texts once calibration data has been collected.
Changing the corpus invalidates all previously stored values.
"""

from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Standard texts
# ---------------------------------------------------------------------------

T1_TECH_REVIEW: Final[str] = (
    "这款手机的屏幕素质让我很震惊。"
    "色彩通透，对比度极高，黑色几乎和关屏没有区别。"
    "拿来和上一代对比，亮度提升了将近四成，户外强光下也能看得清楚。"
    "最让我意外的是功耗居然还降低了，续航多了将近两小时。"
    "这块屏幕，确实是今年旗舰里最强的。"
)

T2_DOCUMENTARY: Final[str] = (
    "每年十一月，数以万计的藏羚羊从可可西里腹地向南迁徙。"
    "这是一段漫长而危险的旅程，它们要穿越结冰的河流，"
    "躲避狼群的追捕，忍受零下三十度的严寒。"
    "曾经因为盗猎，藏羚羊数量一度不足两万只。"
    "那些年，巡护员冒着生命危险日夜巡逻，有人为此献出了生命。"
    "如今种群已恢复到三十万只以上，每到迁徙季节，"
    "绵延几十公里的生命长河再次出现在高原上，壮观而又令人动容。"
)

T3_STARTUP_SPEECH: Final[str] = (
    "三年前辞职创业的时候，卡里只剩四万块。"
    "产品上线第一个月，日活用户只有七个人，其中三个是我们自己。"
    "合伙人说要不算了吧，回去上班至少能还房贷。"
    "那天晚上我确实动摇了，躺在出租屋里盯着天花板想了一整夜。"
    "但第二天早上打开后台，发现一个陌生用户连续用了我们的产品四个小时。"
    "我给他发消息问感觉怎么样，他回了一句：要是再完善一点，我愿意付费。"
    "就这句话，让我决定继续干下去。"
    "后来产品打磨了三个月，终于拿到第一笔融资。"
    "现在回头看，最庆幸的不是拿到了钱，而是那天没删掉后台。"
)


# ---------------------------------------------------------------------------
# Registry + metadata
# ---------------------------------------------------------------------------

STANDARD_TEXTS: Final[dict[str, str]] = {
    "T1_tech_review": T1_TECH_REVIEW,
    "T2_documentary": T2_DOCUMENTARY,
    "T3_startup_speech": T3_STARTUP_SPEECH,
}


TEXT_METADATA: Final[dict[str, dict[str, object]]] = {
    "T1_tech_review": {
        "scene": "tech_review",
        "emotion_arc": "curious -> excited -> amazed",
        "expected_hanzi": 101,
    },
    "T2_documentary": {
        "scene": "documentary_narration",
        "emotion_arc": "calm -> tense -> sad -> warm",
        "expected_hanzi": 153,
    },
    "T3_startup_speech": {
        "scene": "startup_speech",
        "emotion_arc": "down -> hesitant -> hopeful -> determined -> humorous",
        "expected_hanzi": 204,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CJK_RANGE_START = 0x4E00
_CJK_RANGE_END = 0x9FFF


def count_hanzi(text: str) -> int:
    """Count CJK Unified Ideographs (汉字) in *text*.

    Punctuation, digits, whitespace and other non-hanzi characters
    are excluded — this matches how TTS providers charge Chinese
    characters versus ASCII/punctuation differently, and also matches
    `TTSDurationEstimator`'s spoken-char notion closely enough for
    calibration purposes.
    """
    return sum(1 for ch in text if _CJK_RANGE_START <= ord(ch) <= _CJK_RANGE_END)


if __name__ == "__main__":
    # Self-check: verify counts match documented expectations.
    total = 0
    for name, text in STANDARD_TEXTS.items():
        hanzi = count_hanzi(text)
        expected = TEXT_METADATA[name]["expected_hanzi"]
        total_chars = len(text)
        punct = total_chars - hanzi
        mark = "OK" if hanzi == expected else f"MISMATCH expected={expected}"
        print(f"{name}: {hanzi} hanzi + {punct} punct = {total_chars} total [{mark}]")
        total += hanzi
    print(f"Total hanzi: {total}")

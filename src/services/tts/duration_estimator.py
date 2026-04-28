from __future__ import annotations

import re


_NON_SPOKEN_CHAR_PATTERN = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")


def count_spoken_chars(text: str) -> int:
    return len(_NON_SPOKEN_CHAR_PATTERN.sub("", text or ""))


class TTSDurationEstimator:
    def __init__(self, chars_per_second: float = 4.5):
        self.chars_per_second = float(chars_per_second)

    def estimate_duration_ms(self, text: str) -> int:
        char_count = count_spoken_chars(text)
        if char_count == 0 or self.chars_per_second <= 0:
            return 0
        return int(char_count / self.chars_per_second * 1000)

    def calibrate(self, samples: list[tuple[str, int]]) -> float:
        total_chars = 0
        total_ms = 0
        for text, duration_ms in samples:
            total_chars += count_spoken_chars(text)
            total_ms += int(duration_ms)
        if total_ms > 0:
            self.chars_per_second = total_chars / (total_ms / 1000)
        return self.chars_per_second

from __future__ import annotations

import re

from services.gemini.translator import (
    GeminiTranslator,
    REWRITE_PROMPT_TEMPLATE_CHANGE_PCT_TOKEN,
    REWRITE_PROMPT_TEMPLATE_CURRENT_CHARS_TOKEN,
    REWRITE_PROMPT_TEMPLATE_DIRECTION_INSTRUCTION_TOKEN,
    REWRITE_PROMPT_TEMPLATE_DIRECTION_TOKEN,
    REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_CHARS_TOKEN,
    REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_RATIO_PCT_TOKEN,
    REWRITE_PROMPT_TEMPLATE_SOURCE_TEXT_TOKEN,
    REWRITE_PROMPT_TEMPLATE_TARGET_CHARS_TOKEN,
    REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_CHARS_TOKEN,
    REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_RATIO_PCT_TOKEN,
    REWRITE_PROMPT_TEMPLATE_TEXT_TOKEN,
    TranslationError,
    get_effective_rewrite_prompt_template,
)


_NON_SPOKEN_CHAR_PATTERN = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")


class GeminiRewriter:
    def __init__(
        self,
        translator: GeminiTranslator,
        chars_per_second: float = 4.5,
        chars_per_second_by_speaker: dict[str, float] | None = None,
        rewrite_prompt_template: str | None = None,
    ):
        self.translator = translator
        self.chars_per_second = float(chars_per_second)
        self.chars_per_second_by_speaker = {
            str(speaker_id): float(value)
            for speaker_id, value in (chars_per_second_by_speaker or {}).items()
        }
        self.rewrite_prompt_template = get_effective_rewrite_prompt_template(
            rewrite_prompt_template
        )

    def rewrite_for_duration(
        self,
        tts_cn_text: str,
        actual_duration_ms: int,
        target_duration_ms: int,
        source_text: str = "",
        speaker_id: str | None = None,
    ) -> str:
        return self.rewrite_for_duration_with_profile(
            tts_cn_text,
            actual_duration_ms=actual_duration_ms,
            target_duration_ms=target_duration_ms,
            source_text=source_text,
            speaker_id=speaker_id,
        )

    def rewrite_for_duration_with_profile(
        self,
        tts_cn_text: str,
        *,
        actual_duration_ms: int,
        target_duration_ms: int,
        source_text: str = "",
        speaker_id: str | None = None,
        preferred_min_ratio: float | None = None,
        preferred_max_ratio: float | None = None,
    ) -> str:
        normalized_text = (tts_cn_text or "").strip()
        if not normalized_text:
            return normalized_text
        if target_duration_ms <= 0:
            return normalized_text

        current_chars = len(_NON_SPOKEN_CHAR_PATTERN.sub("", normalized_text))
        chars_per_second = self.chars_per_second_by_speaker.get(
            str(speaker_id).strip(),
            self.chars_per_second,
        )
        target_chars = max(1, int(target_duration_ms / 1000 * chars_per_second))
        direction = "shrink" if actual_duration_ms > target_duration_ms else "expand"
        change_pct = abs(actual_duration_ms - target_duration_ms) / target_duration_ms * 100
        min_ratio = float(preferred_min_ratio) if preferred_min_ratio is not None else 0.9
        max_ratio = float(preferred_max_ratio) if preferred_max_ratio is not None else 1.1
        target_lower_chars = max(1, int(target_chars * min_ratio))
        target_upper_chars = max(target_lower_chars, int(target_chars * max_ratio))
        prompt = self._build_rewrite_prompt(
            normalized_text,
            direction=direction,
            current_chars=current_chars,
            target_chars=target_chars,
            target_lower_chars=target_lower_chars,
            target_upper_chars=target_upper_chars,
            target_lower_ratio_pct=min_ratio * 100.0,
            target_upper_ratio_pct=max_ratio * 100.0,
            change_pct=change_pct,
            source_text=source_text,
        )
        rewritten_text = self.translator._call_task_with_fallback(
            "s5_rewrite",
            prompt,
            json_mode=False,
        ).strip()
        return rewritten_text or normalized_text

    def _build_rewrite_prompt(
        self,
        tts_cn_text: str,
        direction: str,
        current_chars: int,
        target_chars: int,
        target_lower_chars: int | None = None,
        target_upper_chars: int | None = None,
        target_lower_ratio_pct: float | None = None,
        target_upper_ratio_pct: float | None = None,
        change_pct: float = 0.0,
        source_text: str = "",
    ) -> str:
        if direction == "shrink":
            direction_desc = "缩短"
            instruction = "删减冗余词汇、连接词，精简表达"
        else:
            direction_desc = "扩充"
            instruction = "把隐含意思说得更完整，增加适当口语化过渡语"

        normalized_source_text = (source_text or "").strip() or "未提供"

        target_lower_chars = int(target_lower_chars) if target_lower_chars is not None else target_chars
        target_upper_chars = int(target_upper_chars) if target_upper_chars is not None else target_chars
        target_lower_ratio_pct = (
            float(target_lower_ratio_pct) if target_lower_ratio_pct is not None else 100.0
        )
        target_upper_ratio_pct = (
            float(target_upper_ratio_pct) if target_upper_ratio_pct is not None else 100.0
        )

        return (
            self.rewrite_prompt_template
            .replace(REWRITE_PROMPT_TEMPLATE_DIRECTION_TOKEN, direction_desc)
            .replace(REWRITE_PROMPT_TEMPLATE_DIRECTION_INSTRUCTION_TOKEN, instruction)
            .replace(REWRITE_PROMPT_TEMPLATE_CURRENT_CHARS_TOKEN, str(current_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TEXT_TOKEN, tts_cn_text)
            .replace(REWRITE_PROMPT_TEMPLATE_SOURCE_TEXT_TOKEN, normalized_source_text)
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_CHARS_TOKEN, str(target_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_CHARS_TOKEN, str(target_lower_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_CHARS_TOKEN, str(target_upper_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_RATIO_PCT_TOKEN, f"{target_lower_ratio_pct:.0f}")
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_RATIO_PCT_TOKEN, f"{target_upper_ratio_pct:.0f}")
            .replace(REWRITE_PROMPT_TEMPLATE_CHANGE_PCT_TOKEN, f"{change_pct:.0f}")
        )

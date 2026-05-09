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
        usage_phase: str = "",
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
        self.usage_phase = usage_phase

    def rewrite_for_duration(
        self,
        cn_text: str,
        actual_duration_ms: int,
        target_duration_ms: int,
        source_text: str = "",
        speaker_id: str | None = None,
    ) -> str:
        return self.rewrite_for_duration_with_profile(
            cn_text,
            actual_duration_ms=actual_duration_ms,
            target_duration_ms=target_duration_ms,
            source_text=source_text,
            speaker_id=speaker_id,
        )

    def rewrite_for_duration_with_profile(
        self,
        cn_text: str,
        *,
        actual_duration_ms: int,
        target_duration_ms: int,
        source_text: str = "",
        speaker_id: str | None = None,
        preferred_min_ratio: float | None = None,
        preferred_max_ratio: float | None = None,
        target_lower_chars: int | None = None,
        target_upper_chars: int | None = None,
        task_name: str = "s5_rewrite",
        strict_retry_reason: str = "",
    ) -> str:
        normalized_text = (cn_text or "").strip()
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
        if target_lower_chars is None:
            target_lower_chars = max(1, int(target_chars * min_ratio))
        else:
            target_lower_chars = max(1, int(target_lower_chars))
        if target_upper_chars is None:
            target_upper_chars = max(target_lower_chars, int(target_chars * max_ratio))
        else:
            target_upper_chars = max(target_lower_chars, int(target_upper_chars))
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
            strict_retry_reason=strict_retry_reason,
        )
        rewritten_text = self._call_task_with_usage_phase(
            task_name,
            prompt,
            json_mode=False,
        ).strip()
        return rewritten_text or normalized_text

    def rewrite_short_content_compact(
        self,
        cn_text: str,
        *,
        source_text: str,
        target_duration_ms: int,
        target_lower_chars: int,
        target_upper_chars: int,
        task_name: str = "s5_short_content_compact",
        strict_retry_reason: str = "",
    ) -> str:
        """Rewrite a short contentful segment as compact spoken Chinese.

        This is intentionally separate from the generic duration rewrite:
        short interview questions and quick answers need aggressive oral
        compression, while the generic prompt tries harder to preserve literal
        wording and often remains too long for a 2-8s slot.

        ``strict_retry_reason`` (2026-05-09): when the first compact attempt
        was rejected (e.g. ``above_ceiling:84>72``), the pipeline retries via
        ``_rewrite_short_content_compact_with_guardrails`` which forwards this
        kwarg. Mirrors the strict-retry behaviour of
        ``rewrite_for_duration_with_profile`` — see ``_build_rewrite_prompt``.
        Empty string disables the strict-retry tail block.
        """
        normalized_text = (cn_text or "").strip()
        if not normalized_text:
            return normalized_text
        lower_chars = max(1, int(target_lower_chars))
        upper_chars = max(lower_chars, int(target_upper_chars))
        prompt = self._build_short_content_compact_prompt(
            normalized_text,
            source_text=source_text,
            target_duration_ms=target_duration_ms,
            target_lower_chars=lower_chars,
            target_upper_chars=upper_chars,
            strict_retry_reason=strict_retry_reason,
        )
        rewritten_text = self._call_task_with_usage_phase(
            task_name,
            prompt,
            json_mode=False,
        ).strip()
        return rewritten_text or normalized_text

    def _call_task_with_usage_phase(
        self,
        task_name: str,
        prompt: str,
        *,
        json_mode: bool,
    ) -> str:
        previous_phase = getattr(self.translator, "_metering_usage_context", "")
        if self.usage_phase:
            setattr(self.translator, "_metering_usage_context", self.usage_phase)
        try:
            return self.translator._call_task_with_fallback(
                task_name,
                prompt,
                json_mode=json_mode,
            )
        finally:
            if self.usage_phase:
                setattr(self.translator, "_metering_usage_context", previous_phase)

    @staticmethod
    def _build_short_content_compact_prompt(
        cn_text: str,
        *,
        source_text: str,
        target_duration_ms: int,
        target_lower_chars: int,
        target_upper_chars: int,
        strict_retry_reason: str = "",
    ) -> str:
        current_chars = len(_NON_SPOKEN_CHAR_PATTERN.sub("", cn_text or ""))
        source = (source_text or "").strip() or "未提供"
        target_seconds = max(0.0, target_duration_ms / 1000.0)
        prompt = (
            "你是视频配音口播压缩编辑。请把下面的中文翻译压缩成自然、短促、可直接配音的中文，"
            "用于一个很短的真实内容段。\n\n"
            f"英文原文：{source}\n"
            f"当前中文：{cn_text}\n"
            f"目标槽位：约 {target_seconds:.1f} 秒\n"
            f"当前 spoken chars：{current_chars}\n"
            f"目标 spoken chars：{target_lower_chars}~{target_upper_chars}\n\n"
            "压缩规则：\n"
            "1. 只保留核心意思，允许牺牲寒暄、填充词、重复主语、弱连接词和口头停顿。\n"
            "2. 问句优先改成短中文问法；多个连续问题可合并为一个核心问题。\n"
            "3. 短回答优先保留结论、比较对象、立场和动作。\n"
            "4. 必须保留数字、否定、关键专名、公司/产品名、时间和方向性判断。\n"
            "5. 不新增解释，不补背景，不改变说话人的立场。\n"
            "6. 输出前按 spoken-char 口径自检：只统计中文、英文、数字，不统计标点、空格、换行。\n"
            f"7. 最终必须落在 {target_lower_chars}~{target_upper_chars} 个 spoken chars 之间。\n\n"
            "只输出压缩后的中文口播文本，不要输出解释、字数、引号或多方案。"
        )
        # 2026-05-09: mirror the strict-retry tail used by _build_rewrite_prompt
        # (see line 263-269) so the compact path's retry can also surface the
        # rejection reason. Without this, _rewrite_short_content_compact_with_guardrails
        # raises TypeError when forwarding strict_retry_reason — the retry
        # mechanism only worked for the generic rewrite path.
        if strict_retry_reason:
            prompt += (
                "\n\n严格重试：上一版输出因"
                f"{strict_retry_reason}未通过字数保护。"
                "这一次必须优先满足上述字数下限和上限，"
                "不得反向改写，不得输出解释。"
            )
        return prompt

    def _build_rewrite_prompt(
        self,
        cn_text: str,
        direction: str,
        current_chars: int,
        target_chars: int,
        target_lower_chars: int | None = None,
        target_upper_chars: int | None = None,
        target_lower_ratio_pct: float | None = None,
        target_upper_ratio_pct: float | None = None,
        change_pct: float = 0.0,
        source_text: str = "",
        strict_retry_reason: str = "",
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

        rendered_prompt = (
            self.rewrite_prompt_template
            .replace(REWRITE_PROMPT_TEMPLATE_DIRECTION_TOKEN, direction_desc)
            .replace(REWRITE_PROMPT_TEMPLATE_DIRECTION_INSTRUCTION_TOKEN, instruction)
            .replace(REWRITE_PROMPT_TEMPLATE_CURRENT_CHARS_TOKEN, str(current_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TEXT_TOKEN, cn_text)
            .replace(REWRITE_PROMPT_TEMPLATE_SOURCE_TEXT_TOKEN, normalized_source_text)
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_CHARS_TOKEN, str(target_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_CHARS_TOKEN, str(target_lower_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_CHARS_TOKEN, str(target_upper_chars))
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_RATIO_PCT_TOKEN, f"{target_lower_ratio_pct:.0f}")
            .replace(REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_RATIO_PCT_TOKEN, f"{target_upper_ratio_pct:.0f}")
            .replace(REWRITE_PROMPT_TEMPLATE_CHANGE_PCT_TOKEN, f"{change_pct:.0f}")
        )
        prompt = (
            f"{rendered_prompt}\n\n"
            "字数硬约束：输出前请自行按 spoken-char 口径检查一次，"
            f"只统计中文、英文、数字，不统计标点、空格、换行。"
            f"最终文本必须落在 {target_lower_chars}~{target_upper_chars} 个 spoken chars 之间。"
            "如果会低于下限，请保留必要信息和自然口语连接；"
            "如果会高于上限，请继续压缩冗余表达。"
            "最终只输出改写后的中文文本，不要输出字数、解释或引号。"
        )
        if strict_retry_reason:
            prompt += (
                "\n\n严格重试：上一版输出因"
                f"{strict_retry_reason}未通过字数保护。"
                "这一次必须优先满足上述字数下限和上限，"
                "不得反向改写，不得输出解释。"
            )
        return prompt

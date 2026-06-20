from __future__ import annotations

import re

from services.language_registry import get_language_descriptor
from services.gemini.translator import (
    DEFAULT_REWRITE_PROMPT_TEMPLATE,
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
    _REWRITE_TEMPLATE_BY_TARGET,
    get_effective_rewrite_prompt_template,
)


_NON_SPOKEN_CHAR_PATTERN = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")

#: Mean spoken chars (letters+digits, no spaces) per English word, used to convert
#: the pipeline's CHARACTER-based rewrite bounds into the WORD budget the Latin
#: prompt + self-check use. PROVISIONAL (v3 line 128/216 defers per-language cps
#: recalibration); kept consistent with LanguageDescriptor.spoken_units_per_second
#: (en 2.6 words/s * 4.7 chars/word ~= 12.2 chars/s, the measured English char-rate).
_LATIN_CHARS_PER_WORD = 4.7


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
        # Language pair from the translator (set during translate()). Default
        # en->zh-CN → Chinese rewrite + char counting, byte-identical.
        self._source_language = getattr(translator, "_translate_source_language", "en")
        self._target_language = getattr(translator, "_translate_target_language", "zh-CN")
        _tgt_desc = get_language_descriptor(self._target_language)
        self._target_is_latin = _tgt_desc is not None and _tgt_desc.script_family == "latin"
        # Latin targets need a word-rate (the probe calibrates char-rate only, so
        # the per-voice cps is the wrong unit for a word budget). Default 2.6 wps.
        self._target_units_per_second_default = (
            _tgt_desc.spoken_units_per_second if _tgt_desc is not None else 2.6
        )
        self.rewrite_prompt_template = self._select_rewrite_template(
            get_effective_rewrite_prompt_template(rewrite_prompt_template)
        )
        self.usage_phase = usage_phase

    def _select_rewrite_template(self, configured: str) -> str:
        """Pick the rewrite template for the dub's TARGET language. Default
        zh-CN → the configured template (override or DEFAULT, byte-identical). A
        non-default target honors an admin override only when it declares the
        canonical 'src->tgt' key (§2.3 fail-closed), else the per-target template."""
        if self._target_language == "zh-CN":
            return configured
        marker = f"{self._source_language}->{self._target_language}"
        if configured != DEFAULT_REWRITE_PROMPT_TEMPLATE and marker in configured:
            return configured
        return _REWRITE_TEMPLATE_BY_TARGET.get(self._target_language, DEFAULT_REWRITE_PROMPT_TEMPLATE)

    def _spoken_units(self, text: str) -> int:
        """Spoken-length unit for the TARGET language: CJK → per-char (legacy,
        byte-identical), Latin → word count (matches the word-based budget)."""
        if self._target_is_latin:
            return len(re.findall(r"[A-Za-z0-9']+", text or ""))
        return len(_NON_SPOKEN_CHAR_PATTERN.sub("", text or ""))

    def _spoken_units_per_second(self, speaker_id: str | None) -> float:
        """Rate mapping a duration to a TARGET-unit budget, in the SAME unit as
        :meth:`_spoken_units`. CJK → the per-voice calibrated char-rate (legacy,
        byte-identical). Latin → the language word-rate constant: the probe
        calibrates char-rate only, so the per-voice cps is the wrong unit for a
        word budget and must not be used here (see CodeX PR-CD P2)."""
        if self._target_is_latin:
            return self._target_units_per_second_default
        return self.chars_per_second_by_speaker.get(
            str(speaker_id).strip(),
            self.chars_per_second,
        )

    def _to_target_budget_units(self, char_count: int) -> int:
        """Convert a CHARACTER-based budget bound (computed by the pipeline from the
        char-rate cps) into the TARGET spoken unit. CJK → unchanged (byte-identical).
        Latin → words (÷ mean chars/word), so the prompt label, the model's word
        self-check, and the pipeline's char guard all stay mutually consistent."""
        if self._target_is_latin:
            return max(1, round(char_count / _LATIN_CHARS_PER_WORD))
        return char_count

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

        current_chars = self._spoken_units(normalized_text)
        chars_per_second = self._spoken_units_per_second(speaker_id)
        target_chars = max(1, int(target_duration_ms / 1000 * chars_per_second))
        direction = "shrink" if actual_duration_ms > target_duration_ms else "expand"
        change_pct = abs(actual_duration_ms - target_duration_ms) / target_duration_ms * 100
        min_ratio = float(preferred_min_ratio) if preferred_min_ratio is not None else 0.9
        max_ratio = float(preferred_max_ratio) if preferred_max_ratio is not None else 1.1
        if target_lower_chars is None:
            target_lower_chars = max(1, int(target_chars * min_ratio))
        else:
            target_lower_chars = max(1, self._to_target_budget_units(int(target_lower_chars)))
        if target_upper_chars is None:
            target_upper_chars = max(target_lower_chars, int(target_chars * max_ratio))
        else:
            target_upper_chars = max(
                target_lower_chars, self._to_target_budget_units(int(target_upper_chars))
            )
        if self._target_is_latin:
            # target_chars uses the fixed word-rate (2.6 wps) while explicit bounds
            # are ÷ _LATIN_CHARS_PER_WORD from the per-voice char-rate; when that rate
            # != ~12.2 the two diverge and the prompt could show a target outside its
            # own band. Clamp the displayed target into the band (re-CodeX P2). CJK is
            # untouched (target_chars already sits inside its char-based band).
            target_chars = max(target_lower_chars, min(target_chars, target_upper_chars))
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
        # CHAR-based bounds from the pipeline → TARGET spoken unit (Latin: words).
        lower_chars = max(1, self._to_target_budget_units(int(target_lower_chars)))
        upper_chars = max(lower_chars, self._to_target_budget_units(int(target_upper_chars)))
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

    def _build_short_content_compact_prompt(
        self,
        cn_text: str,
        *,
        source_text: str,
        target_duration_ms: int,
        target_lower_chars: int,
        target_upper_chars: int,
        strict_retry_reason: str = "",
    ) -> str:
        current_chars = self._spoken_units(cn_text or "")
        target_seconds = max(0.0, target_duration_ms / 1000.0)
        if self._target_is_latin:
            source = (source_text or "").strip() or "(none)"
            prompt = (
                "You are a video-dubbing voice-over compression editor. Compress the "
                "English text below into natural, short, directly speakable English, for a "
                "very short real-content segment.\n\n"
                f"Source transcript: {source}\n"
                f"Current English: {cn_text}\n"
                f"Target slot: about {target_seconds:.1f} seconds\n"
                f"Current spoken units: {current_chars}\n"
                f"Target spoken units: {target_lower_chars}~{target_upper_chars}\n\n"
                "Compression rules:\n"
                "1. Keep only the core meaning; you may drop pleasantries, fillers, repeated subjects, weak connectors and verbal pauses.\n"
                "2. Turn questions into short forms; merge consecutive questions into one core question.\n"
                "3. For short answers, keep the conclusion, comparison target, stance and action.\n"
                "4. Always keep numbers, negations, key proper nouns, company/product names, time and directional judgments.\n"
                "5. Add no explanation or background; do not change the speaker's stance.\n"
                "6. Before output, self-check the spoken-unit count (count words, ignore punctuation/spaces/newlines).\n"
                f"7. The final text must land within {target_lower_chars}~{target_upper_chars} spoken units.\n\n"
                "Output only the compressed English voice-over text — no explanation, counts, quotes or alternatives."
            )
            if strict_retry_reason:
                prompt += (
                    "\n\nStrict retry: the previous output failed the length guard "
                    f"({strict_retry_reason}). This time you MUST satisfy the lower and upper "
                    "bounds above; do not rewrite in the wrong direction, and add no explanation."
                )
            return prompt
        source = (source_text or "").strip() or "未提供"
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
            if self._target_is_latin:
                direction_desc = "shorten"
                instruction = "remove redundant words and connectors; tighten the phrasing"
            else:
                direction_desc = "缩短"
                instruction = "删减冗余词汇、连接词，精简表达"
        else:
            if self._target_is_latin:
                direction_desc = "expand"
                instruction = "state the implied meaning more fully; add natural spoken transitions"
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
        if self._target_is_latin:
            prompt = (
                f"{rendered_prompt}\n\n"
                "Length constraint: before output, self-check the spoken-unit count once "
                "(count words, ignore punctuation/spaces/newlines). "
                f"The final text must land within {target_lower_chars}~{target_upper_chars} spoken units. "
                "If it would fall below the lower bound, keep necessary information and natural spoken connectors; "
                "if above the upper bound, keep compressing redundant phrasing. "
                "Output only the rewritten English text — no counts, explanation or quotes."
            )
            if strict_retry_reason:
                prompt += (
                    "\n\nStrict retry: the previous output failed the length guard "
                    f"({strict_retry_reason}). This time you MUST satisfy the lower and upper "
                    "bounds above; do not rewrite in the wrong direction, and add no explanation."
                )
            return prompt
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

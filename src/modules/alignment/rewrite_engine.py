from typing import Protocol

from core.exceptions import RewriteError


class LLMServiceProtocol(Protocol):
    def rewrite_text(
        self,
        prompt: str,
        source_text: str,
        actual_duration_ms: int,
        target_duration_ms: int,
    ) -> str:
        """Return rewritten Chinese text only."""


class RewriteEngine:
    def __init__(self, llm_service: LLMServiceProtocol) -> None:
        self.llm_service = llm_service

    def rewrite_for_duration(
        self,
        text: str,
        actual_duration_ms: int,
        target_duration_ms: int,
    ) -> str:
        if not text.strip():
            raise RewriteError("Cannot rewrite empty text.")
        if actual_duration_ms <= 0 or target_duration_ms <= 0:
            raise RewriteError("Durations must be positive for rewrite.")

        diff_ratio = (actual_duration_ms - target_duration_ms) / target_duration_ms
        direction = "缩短" if diff_ratio > 0 else "扩写"
        prompt = self._build_prompt(
            text=text,
            actual_duration_ms=actual_duration_ms,
            target_duration_ms=target_duration_ms,
            diff_ratio=diff_ratio,
            direction=direction,
        )
        rewritten_text = self.llm_service.rewrite_text(
            prompt=prompt,
            source_text=text,
            actual_duration_ms=actual_duration_ms,
            target_duration_ms=target_duration_ms,
        ).strip()
        normalized_text = rewritten_text.strip("\"'“” \n\t")
        if not normalized_text:
            raise RewriteError("LLM service returned empty rewritten text.")
        return normalized_text

    def _build_prompt(
        self,
        text: str,
        actual_duration_ms: int,
        target_duration_ms: int,
        diff_ratio: float,
        direction: str,
    ) -> str:
        return (
            "你是字幕中文改写助手。\n"
            "任务：在保持原意的前提下，让文本更适合配音时长。\n"
            "要求：\n"
            "1. 只输出纯中文改写文本。\n"
            "2. 不要解释，不要加说话人前缀。\n"
            "3. 保持信息核心不变。\n"
            f"原文：{text}\n"
            f"当前时长(ms)：{actual_duration_ms}\n"
            f"目标时长(ms)：{target_duration_ms}\n"
            f"偏差比例：{diff_ratio:.2%}\n"
            f"方向：需要{direction}\n"
        )

from dataclasses import dataclass

from core.exceptions import RewriteError


@dataclass(slots=True)
class MockLLMConfig:
    min_chars: int = 1
    padding_seed: str = "补充内容以保持表达完整"


class MockLLMService:
    def __init__(self, config: MockLLMConfig | None = None) -> None:
        self.config = config or MockLLMConfig()

    def rewrite_text(
        self,
        prompt: str,
        source_text: str,
        actual_duration_ms: int,
        target_duration_ms: int,
    ) -> str:
        del prompt

        if actual_duration_ms <= 0 or target_duration_ms <= 0:
            raise RewriteError("Durations must be positive in mock LLM service.")

        normalized_text = "".join(source_text.split())
        if not normalized_text:
            raise RewriteError("Source text cannot be empty in mock LLM service.")

        target_chars = max(
            self.config.min_chars,
            round(len(normalized_text) * target_duration_ms / actual_duration_ms),
        )

        if target_chars <= len(normalized_text):
            return normalized_text[:target_chars]

        extra_chars = target_chars - len(normalized_text)
        padding = self._make_padding(extra_chars)
        return normalized_text + padding

    def _make_padding(self, target_length: int) -> str:
        repeated_padding = self.config.padding_seed
        while len(repeated_padding) < target_length:
            repeated_padding += self.config.padding_seed
        return repeated_padding[:target_length]

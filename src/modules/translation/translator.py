from dataclasses import dataclass
from typing import Any

from core.models import SubtitleLine
from modules.translation.providers import TranslationProvider
from modules.translation.router import TranslationChunkRouter
from modules.translation.sanitizer import SanitizedBatchResult, TranslationSanitizer
from modules.translation.validators import (
    validate_source_lines,
    validate_translated_texts,
)


@dataclass(slots=True)
class MockTranslatorConfig:
    prefix: str = "CN:"


class MockTranslator:
    def __init__(self, config: MockTranslatorConfig | None = None) -> None:
        self.config = config or MockTranslatorConfig()

    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        return [f"{self.config.prefix}{line.en_text.strip()}" for line in lines]

    def get_cache_context(self) -> dict[str, Any]:
        return {"provider_variant": "mock_translator_v1"}


class TranslationPipeline:
    def __init__(
        self,
        router: TranslationChunkRouter,
        translator: TranslationProvider,
        sanitizer: TranslationSanitizer | None = None,
        fallback_translator: TranslationProvider | None = None,
    ) -> None:
        self.router = router
        self.translator = translator
        self.sanitizer = sanitizer or TranslationSanitizer()
        self.fallback_translator = fallback_translator

    def translate_lines(self, lines: list[SubtitleLine]) -> list[SubtitleLine]:
        validate_source_lines(lines)
        translated_lines: list[SubtitleLine] = []

        for chunk in self.router.route(lines):
            raw_translated_texts = self.translator.translate_batch(chunk)
            processed_batch = self.process_batch_output(chunk, raw_translated_texts, sanitize=True)
            translated_lines.extend(self.merge_batch(chunk, processed_batch.texts))

        return translated_lines

    def process_batch_output(
        self,
        lines: list[SubtitleLine],
        translated_texts: list[str],
        sanitize: bool = True,
    ) -> SanitizedBatchResult:
        processed_batch = (
            self.sanitizer.sanitize_batch(translated_texts)
            if sanitize
            else SanitizedBatchResult(texts=translated_texts, changed_line_count=0, action_counts={})
        )
        validate_translated_texts(lines, processed_batch.texts)
        return processed_batch

    def merge_batch(self, lines: list[SubtitleLine], translated_texts: list[str]) -> list[SubtitleLine]:
        merged_lines: list[SubtitleLine] = []
        for line, translated_text in zip(lines, translated_texts):
            cn_text = translated_text.strip()
            merged_lines.append(
                SubtitleLine(
                    index=line.index,
                    start_ms=line.start_ms,
                    end_ms=line.end_ms,
                    speaker_id=line.speaker_id,
                    speaker_name=line.speaker_name,
                    en_text=line.en_text,
                    cn_text=cn_text,
                )
            )
        return merged_lines

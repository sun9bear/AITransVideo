from core.exceptions import (
    TranslationProviderLineCountError,
    TranslationProviderOutputError,
    TranslationValidationError,
)
from core.models import SubtitleLine


_SUSPICIOUS_EXPLANATORY_PREFIXES = (
    "translation:",
    "translation\uff1a",
    "translated:",
    "translated text:",
    "translated text\uff1a",
    "translation result:",
    "translation result\uff1a",
    "here is the translation",
    "\u4ee5\u4e0b\u662f\u7ffb\u8bd1",
    "\u4ee5\u4e0b\u7ffb\u8bd1",
    "\u7ffb\u8bd1\uff1a",
    "\u8bd1\u6587\uff1a",
    "\u4e2d\u6587\u7ffb\u8bd1\uff1a",
)


def validate_source_lines(lines: list[SubtitleLine]) -> None:
    for line in lines:
        if not line.speaker_id.strip():
            raise TranslationValidationError(
                f"speaker_id is required for translation flow; invalid line index={line.index}."
            )


def validate_translated_line_count(lines: list[SubtitleLine], translated_texts: list[str]) -> None:
    if len(lines) != len(translated_texts):
        raise TranslationProviderLineCountError(
            f"Translated line count mismatch: expected {len(lines)}, got {len(translated_texts)}."
        )


def validate_translated_texts(lines: list[SubtitleLine], translated_texts: list[str]) -> None:
    validate_translated_line_count(lines, translated_texts)

    for line, translated_text in zip(lines, translated_texts):
        if not isinstance(translated_text, str):
            raise TranslationProviderOutputError(
                f"Translated text must be a string; invalid line index={line.index}."
            )

        cleaned_text = translated_text.strip()
        if not cleaned_text:
            raise TranslationProviderOutputError(
                f"Translated text is empty after sanitization; invalid line index={line.index}."
            )
        if _has_suspicious_explanatory_prefix(cleaned_text):
            raise TranslationProviderOutputError(
                f"Translated text contains suspicious explanatory output; invalid line index={line.index}."
            )


def _has_suspicious_explanatory_prefix(text: str) -> bool:
    normalized_text = text.strip().lower()
    return any(normalized_text.startswith(prefix) for prefix in _SUSPICIOUS_EXPLANATORY_PREFIXES)

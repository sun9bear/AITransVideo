import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class SanitizedTextResult:
    text: str
    applied_actions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SanitizedBatchResult:
    texts: list[str]
    changed_line_count: int
    action_counts: dict[str, int] = field(default_factory=dict)


class TranslationSanitizer:
    """Clean obvious wrapper formatting from provider output without changing line count."""

    _CODE_FENCE_PATTERN = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)
    _WRAPPER_PAIRS = (
        ("**", "**"),
        ("__", "__"),
        ("`", "`"),
        ("*", "*"),
        ("_", "_"),
    )

    def sanitize_batch(self, translated_texts: list[str]) -> SanitizedBatchResult:
        sanitized_results = [self.sanitize_text(text) for text in translated_texts]
        action_counts: dict[str, int] = {}
        changed_line_count = 0

        for original_text, sanitized_result in zip(translated_texts, sanitized_results):
            if sanitized_result.text != original_text:
                changed_line_count += 1
            for action in sanitized_result.applied_actions:
                action_counts[action] = action_counts.get(action, 0) + 1

        return SanitizedBatchResult(
            texts=[result.text for result in sanitized_results],
            changed_line_count=changed_line_count,
            action_counts=action_counts,
        )

    def sanitize_text(self, text: str) -> SanitizedTextResult:
        cleaned_text = text
        applied_actions: list[str] = []

        stripped_text = cleaned_text.strip()
        if stripped_text != cleaned_text:
            cleaned_text = stripped_text
            applied_actions.append("trim_whitespace")

        previous_text = None

        while cleaned_text != previous_text:
            previous_text = cleaned_text
            cleaned_text, code_fence_changed = self._strip_code_fence(cleaned_text)
            if code_fence_changed:
                applied_actions.append("strip_code_fence")
            cleaned_text, wrapper_changed = self._strip_markdown_wrapper(cleaned_text)
            if wrapper_changed:
                applied_actions.append("strip_markdown_wrapper")

        return SanitizedTextResult(
            text=cleaned_text.strip(),
            applied_actions=applied_actions,
        )

    def _strip_code_fence(self, text: str) -> tuple[str, bool]:
        match = self._CODE_FENCE_PATTERN.match(text)
        if match is None:
            return text, False
        return match.group(1).strip(), True

    def _strip_markdown_wrapper(self, text: str) -> tuple[str, bool]:
        for prefix, suffix in self._WRAPPER_PAIRS:
            if text.startswith(prefix) and text.endswith(suffix):
                inner_text = text[len(prefix) : len(text) - len(suffix)].strip()
                if inner_text:
                    return inner_text, True
        return text, False

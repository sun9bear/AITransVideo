import pytest

from core.exceptions import TranslationValidationError
from core.models import SubtitleLine
from modules.translation.router import TranslationChunkRouter, TranslationRouterConfig
from modules.translation.translator import MockTranslator, TranslationPipeline


class BrokenTranslator:
    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        del lines
        return ["CN:only-one-line"]


class EmptyTranslator:
    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        return ["   " for _ in lines]


class WrappedTranslator:
    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        return [
            f"```text\nCN:{line.en_text.strip()}\n```" if index == 0 else f"**CN:{line.en_text.strip()}**"
            for index, line in enumerate(lines)
        ]


class ExplanatoryTranslator:
    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        return [f"Translation: CN:{line.en_text.strip()}" for line in lines]


def _make_lines(count: int) -> list[SubtitleLine]:
    return [
        SubtitleLine(
            index=index,
            start_ms=(index - 1) * 1_000,
            end_ms=index * 1_000,
            speaker_id=f"speaker_{index}",
            speaker_name="SharedName" if index % 2 == 0 else "Narrator",
            en_text=f"Line {index}",
            cn_text="",
        )
        for index in range(1, count + 1)
    ]


def test_translation_router_splits_into_configured_batches() -> None:
    router = TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=4))

    chunks = router.route(_make_lines(5))

    assert [len(chunk) for chunk in chunks] == [2, 2, 1]


def test_translation_router_splits_when_char_threshold_is_hit() -> None:
    router = TranslationChunkRouter(
        TranslationRouterConfig(batch_size=3, max_batch_size=4, max_chars_per_batch=12)
    )
    lines = [
        SubtitleLine(1, 0, 900, "speaker_1", "Narrator", "abcdefgh", ""),
        SubtitleLine(2, 1_000, 1_900, "speaker_2", "Narrator", "ijklmnop", ""),
        SubtitleLine(3, 2_000, 2_900, "speaker_3", "Narrator", "qrst", ""),
    ]

    chunks = router.route(lines)

    assert [len(chunk) for chunk in chunks] == [1, 2]


def test_translation_pipeline_validates_line_count_match() -> None:
    router = TranslationChunkRouter(TranslationRouterConfig(batch_size=3, max_batch_size=3))
    pipeline = TranslationPipeline(router=router, translator=BrokenTranslator())

    with pytest.raises(TranslationValidationError, match="Translated line count mismatch"):
        pipeline.translate_lines(_make_lines(3))


def test_translation_pipeline_preserves_speaker_identity_fields() -> None:
    router = TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=2))
    pipeline = TranslationPipeline(router=router, translator=MockTranslator())
    lines = [
        SubtitleLine(1, 0, 900, "speaker_a", "Host", "First", ""),
        SubtitleLine(2, 1_000, 1_900, "speaker_b", "Host", "Second", ""),
    ]

    translated = pipeline.translate_lines(lines)

    assert [line.speaker_id for line in translated] == ["speaker_a", "speaker_b"]
    assert [line.speaker_name for line in translated] == ["Host", "Host"]
    assert [line.cn_text for line in translated] == ["CN:First", "CN:Second"]


def test_translation_pipeline_rejects_empty_translated_text() -> None:
    router = TranslationChunkRouter(TranslationRouterConfig(batch_size=1, max_batch_size=1))
    pipeline = TranslationPipeline(router=router, translator=EmptyTranslator())

    with pytest.raises(TranslationValidationError, match="empty after sanitization"):
        pipeline.translate_lines(_make_lines(1))


def test_translation_pipeline_strips_wrapper_formatting() -> None:
    router = TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=2))
    pipeline = TranslationPipeline(router=router, translator=WrappedTranslator())

    translated = pipeline.translate_lines(_make_lines(2))

    assert [line.cn_text for line in translated] == ["CN:Line 1", "CN:Line 2"]

    processed_batch = pipeline.process_batch_output(
        _make_lines(2),
        ["```text\nCN:Line 1\n```", "**CN:Line 2**"],
        sanitize=True,
    )
    assert processed_batch.changed_line_count == 2
    assert processed_batch.action_counts["strip_code_fence"] == 1
    assert processed_batch.action_counts["strip_markdown_wrapper"] == 1


def test_translation_pipeline_writes_cn_text() -> None:
    router = TranslationChunkRouter(TranslationRouterConfig(batch_size=1, max_batch_size=1))
    pipeline = TranslationPipeline(router=router, translator=MockTranslator())

    translated = pipeline.translate_lines(_make_lines(1))

    assert translated[0].cn_text == "CN:Line 1"


def test_translation_pipeline_rejects_suspicious_explanatory_output() -> None:
    router = TranslationChunkRouter(TranslationRouterConfig(batch_size=1, max_batch_size=1))
    pipeline = TranslationPipeline(router=router, translator=ExplanatoryTranslator())

    with pytest.raises(TranslationValidationError, match="suspicious explanatory output"):
        pipeline.translate_lines(_make_lines(1))

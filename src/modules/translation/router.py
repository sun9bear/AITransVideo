from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from core.models import SubtitleLine


@runtime_checkable
class BatchCostEstimatorProtocol(Protocol):
    """Estimate batch routing cost for future char/token strategies."""

    def estimate_line_cost(self, line: SubtitleLine) -> int:
        """Return a routing cost unit for one subtitle line."""


class CharacterCountEstimator:
    """Current Sprint 2B routing cost estimator based on character count."""

    def estimate_line_cost(self, line: SubtitleLine) -> int:
        return len(line.en_text.strip()) + len(line.cn_text.strip())


@dataclass(slots=True)
class TranslationRouterConfig:
    """Dual-threshold routing tuned for mock providers and future window strategies.

    `max_chars_per_batch` is currently a soft limit: if one line alone exceeds it,
    that line is still emitted as a single-line batch instead of being split.
    Token/window-aware routing is intentionally deferred to future provider work.
    """

    batch_size: int = 50
    max_batch_size: int = 100
    max_chars_per_batch: int = 2_000
    cost_estimator: BatchCostEstimatorProtocol = field(default_factory=CharacterCountEstimator)
    token_window_hint: int | None = None

    def __post_init__(self) -> None:
        if self.batch_size <= 0 or self.max_batch_size <= 0:
            raise ValueError("Translation batch sizes must be positive.")
        if self.batch_size > self.max_batch_size:
            raise ValueError("batch_size cannot exceed max_batch_size.")
        if self.max_chars_per_batch <= 0:
            raise ValueError("max_chars_per_batch must be positive.")
        if self.token_window_hint is not None and self.token_window_hint <= 0:
            raise ValueError("token_window_hint must be positive when provided.")


class TranslationChunkRouter:
    """Route subtitle lines by line-count and character-count thresholds.

    The current implementation keeps oversized individual lines intact as their own
    batch and serves as a scaffold until real token/window constraints are added.
    """

    def __init__(self, config: TranslationRouterConfig | None = None) -> None:
        self.config = config or TranslationRouterConfig()

    def route(self, lines: list[SubtitleLine]) -> list[list[SubtitleLine]]:
        if not lines:
            return []

        batches: list[list[SubtitleLine]] = []
        current_batch: list[SubtitleLine] = []
        current_chars = 0

        for line in lines:
            line_cost = self.config.cost_estimator.estimate_line_cost(line)
            exceeds_line_target = len(current_batch) >= self.config.batch_size
            exceeds_char_target = current_batch and current_chars + line_cost > self.config.max_chars_per_batch

            if current_batch and (exceeds_line_target or exceeds_char_target):
                batches.append(current_batch)
                current_batch = []
                current_chars = 0

            current_batch.append(line)
            current_chars += line_cost

            if len(current_batch) >= self.config.max_batch_size:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0

        if current_batch:
            batches.append(current_batch)

        return batches

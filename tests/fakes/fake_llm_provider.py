"""Fake LLMProvider — placeholder for P3/P4 verifier work.

Smart MVP doesn't call LLMs (auto-decision is deterministic). This
fake exists so the LLMProvider Protocol shape has at least one
concrete implementation in the test tree — both for documentation
("here's what an LLM call would look like in Smart land") and for
the verifier work in P3/P4 that will mock LLM outputs.

Echo behaviour: returns the prompt verbatim with deterministic token
counts. Override ``response`` to return a pinned answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.smart.contracts import LLMResult


@dataclass
class FakeLLMProvider:
    """Test double for LLMProvider — echo by default."""

    response: str | None = None  # pin to a fixed answer; None = echo prompt
    input_tokens_per_call: int = 10
    output_tokens_per_call: int = 5

    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        *,
        prompt: str,
        model_name: str,
    ) -> LLMResult:
        self.calls.append({"prompt": prompt, "model_name": model_name})
        return LLMResult(
            text=self.response if self.response is not None else prompt,
            input_tokens=self.input_tokens_per_call,
            output_tokens=self.output_tokens_per_call,
            model_name=model_name,
        )

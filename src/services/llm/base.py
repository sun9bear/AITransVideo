from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class LLMCallConfig:
    provider: str
    model_name: str
    api_key: str | None = None
    api_key_env_var: str | None = None
    base_url: str | None = None
    temperature: float = 0.3
    max_output_tokens: int = 8192
    timeout_seconds: float = 120.0


class LLMProviderError(Exception):
    pass


class LLMProvider(Protocol):
    def generate_text(
        self,
        *,
        prompt: str,
        model_name: str,
        temperature: float,
        max_output_tokens: int,
        json_mode: bool,
    ) -> str:
        ...

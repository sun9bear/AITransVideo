from __future__ import annotations

from typing import Any

import requests

from services.llm.base import LLMCallConfig, LLMProviderError


class AnthropicProvider:
    def __init__(self, config: LLMCallConfig):
        self.config = config

    def generate_text(
        self,
        *,
        prompt: str,
        model_name: str,
        temperature: float,
        max_output_tokens: int,
        json_mode: bool,
    ) -> str:
        api_key = (self.config.api_key or "").strip()
        if not api_key:
            env_var = self.config.api_key_env_var or "ANTHROPIC_API_KEY"
            raise LLMProviderError(f"Anthropic API key is required via autodub.local.json or env {env_var}.")

        base_url = (self.config.base_url or "https://api.anthropic.com").rstrip("/")
        content = prompt
        if json_mode:
            content = f"{prompt}\n\nReturn only valid JSON."

        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": content}],
        }

        try:
            response = requests.post(
                f"{base_url}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LLMProviderError(f"Anthropic request failed: {exc}") from exc

        if response.status_code >= 400:
            raise LLMProviderError(f"Anthropic request failed: {response.status_code} {response.text}")

        try:
            payload = response.json()
            blocks = payload["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMProviderError("Anthropic returned an invalid response payload.") from exc

        text_parts: list[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))

        normalized_message = "".join(text_parts).strip()
        if not normalized_message:
            raise LLMProviderError("Anthropic returned an empty response.")
        return normalized_message

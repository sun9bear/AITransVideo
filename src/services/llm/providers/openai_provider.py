from __future__ import annotations

from typing import Any

import requests

from services.llm.base import LLMCallConfig, LLMProviderError


class OpenAIProvider:
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
            env_var = self.config.api_key_env_var or "OPENAI_API_KEY"
            raise LLMProviderError(f"OpenAI API key is required via autodub.local.json or env {env_var}.")

        base_url = (self.config.base_url or "https://api.openai.com/v1").rstrip("/")
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LLMProviderError(f"OpenAI request failed: {exc}") from exc

        if response.status_code >= 400:
            raise LLMProviderError(f"OpenAI request failed: {response.status_code} {response.text}")

        try:
            payload = response.json()
            message = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("OpenAI returned an invalid response payload.") from exc

        normalized_message = str(message or "").strip()
        if not normalized_message:
            raise LLMProviderError("OpenAI returned an empty response.")
        return normalized_message

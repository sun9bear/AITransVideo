from __future__ import annotations

from typing import Any

import requests

from services.llm.base import LLMCallConfig, LLMProviderError


class DeepSeekProvider:
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
            env_var = self.config.api_key_env_var or "DEEPSEEK_API_KEY"
            raise LLMProviderError(f"DeepSeek API key is required via autodub.local.json or env {env_var}.")

        base_url = (self.config.base_url or "https://api.deepseek.com").rstrip("/")
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if model_name.startswith("deepseek-v4-"):
            payload["thinking"] = {"type": "disabled"}

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
            raise LLMProviderError(f"DeepSeek request failed: {exc}") from exc

        if response.status_code >= 400:
            raise LLMProviderError(f"DeepSeek request failed: {response.status_code} {response.text}")

        try:
            response_payload = response.json()
            message = response_payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("DeepSeek returned an invalid response payload.") from exc

        normalized_message = str(message or "").strip()
        if not normalized_message:
            raise LLMProviderError("DeepSeek returned an empty response.")
        return normalized_message

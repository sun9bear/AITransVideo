"""Shared helpers for provider config diagnostics.

Both ``modules.translation.providers`` and ``services.tts_provider``
previously carried identical copies of these functions.
"""
from __future__ import annotations

from services import config_loader


def summarize_config_source(*sources: str | None) -> str:
    """Classify the overall config source family across multiple resolved values."""
    source_families = {
        family
        for family in (config_loader.summarize_source_family(source) for source in sources)
        if family is not None and family != "default"
    }
    if not source_families:
        return "default"
    if len(source_families) == 1:
        return next(iter(source_families))
    return "mixed"


def classify_api_key_source_type(
    source: str | None,
    *,
    resolved_api_key_present: bool,
    api_key_env_var: str | None,
    api_key_env_var_source: str | None,
    direct_env_keys: tuple[str, ...],
) -> str:
    """Return a human-friendly classification for an API key source."""
    if not resolved_api_key_present:
        return "missing"
    if source is None:
        return "missing"
    if source == "direct_config" or source.startswith("config_file:"):
        return "legacy_file"
    if source.startswith("user:") or source.startswith("machine:"):
        return "persisted_env"
    if source.startswith("process:"):
        source_key = source.split(":", 1)[1]
        if (
            api_key_env_var_source is not None
            and api_key_env_var is not None
            and source_key == api_key_env_var.strip()
        ):
            return "api_key_env_var"
        normalized_direct_env_keys = {
            candidate.strip() for candidate in direct_env_keys if candidate.strip()
        }
        if source_key in normalized_direct_env_keys:
            return "direct_env"
        if api_key_env_var is not None and source_key == api_key_env_var.strip():
            return "api_key_env_var"
        return "direct_env"
    return "missing"

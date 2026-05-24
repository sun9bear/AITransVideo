"""Runtime feature flags with admin-settings overrides.

Environment variables remain the bootstrap/default source.  When an
admin-setting key is explicitly present, it overrides the matching env value so
operators can change Phase 1b rollout state without rebuilding containers.
"""

from __future__ import annotations

from utils.env_flags import env_flag

from .admin_settings import read_admin_setting


_FLAG_TO_ADMIN_SETTING = {
    "AVT_TRANSLATION_SCRIPT_GATE_SHADOW": "phase1b_translation_script_gate_shadow",
    "AVT_TRANSLATION_SCRIPT_GATE_DETECT_ONLY": "phase1b_translation_script_gate_shadow",
    "AVT_VOICE_SAMPLE_SCORING_SHADOW": "phase1b_voice_sample_scoring_shadow",
    "AVT_TRANSLATION_SCRIPT_GATE": "phase1b_translation_script_gate_enabled",
    "AVT_VOICE_SAMPLE_SCORING": "phase1b_voice_sample_scoring_enabled",
    "AVT_AUDIO_TAIL_TRIM": "phase1b_audio_tail_trim_enabled",
    "AVT_WHISPER_QUALITY_GATE": "phase1b_whisper_quality_gate_enabled",
}


def runtime_flag(name: str, *, default: bool = False) -> bool:
    """Return an effective feature flag.

    Precedence:
      1. explicit bool in ``admin_settings.json``
      2. environment variable
      3. caller default

    Missing admin keys intentionally fall through to env so existing deployments
    keep their current behavior until an operator edits the new rollout panel.
    """

    setting_key = _FLAG_TO_ADMIN_SETTING.get(name)
    if setting_key:
        value = read_admin_setting(setting_key, default=None)
        if isinstance(value, bool):
            return value
    return env_flag(name, default=default)


__all__ = ["runtime_flag"]

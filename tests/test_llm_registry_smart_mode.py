"""Smart-mode admin model configuration (2026-05-16 feature).

User feedback after first real smart submission:
    "为什么选的是 Deepseek V4 flash, 我记得方案里要用最好的多模态大模型，
     也就是 Gemini 3.1 Pro. 改一下吧, 让智能版要用到的大模型也让管理员
     在模型管理页面可以配置, 默认都用 Gemini 3.1 Pro"

Before this fix, ``get_prompt_model("smart", "translate")`` fell straight
through to flat ``_DEFAULTS["translate"] = "deepseek"`` because the
admin settings file had no ``prompt_models["smart"]`` entry AND there
was no per-mode default lookup for smart in ``llm_registry``.

After the fix:
1. ``llm_registry._MODE_DEFAULTS["smart"]`` defines per-stage defaults
   (all ``gemini_pro`` per user choice).
2. ``get_prompt_model`` consults ``_MODE_DEFAULTS[mode]`` BEFORE the
   flat ``_DEFAULTS`` fallback.
3. Admin can still override any stage via the new 智能版 tab in the
   model management UI — those overrides land in
   ``admin_settings.json::prompt_models["smart"]`` and take precedence
   over ``_MODE_DEFAULTS``.

Backward compat (per user choice "向后兼容"): no migration of existing
admin_settings.json. Settings file with no ``prompt_models.smart`` key
still works — runtime resolves to the code-built-in defaults.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class TestSmartModeDefaults:
    """Pin smart-mode per-prompt defaults to gemini_pro per user request."""

    def _reload_registry(self):
        """Force fresh load of llm_registry (settings cache reset)."""
        import services.llm_registry as llm_registry
        llm_registry.invalidate_cache()
        return llm_registry

    def test_smart_translate_defaults_to_gemini_pro_when_no_admin_config(
        self, tmp_path, monkeypatch
    ):
        """Real production bug: smart submission used deepseek-v4-flash
        for s3_translate because the registry had no per-mode default
        for smart and flat ``_DEFAULTS["translate"] = "deepseek"`` won."""
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        # Empty settings — admin has not configured smart mode.
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({"prompt_models": {}}), encoding="utf-8"
        )

        llm_registry = self._reload_registry()
        # _SETTINGS_PATH is computed at import time from env var; reload to repoint.
        import importlib
        llm_registry = importlib.reload(llm_registry)

        assert llm_registry.get_prompt_model("smart", "translate") == "gemini_pro"

    def test_smart_all_prompt_keys_default_to_gemini_pro(
        self, tmp_path, monkeypatch
    ):
        """All 7 smart-mode prompt keys must default to gemini_pro per user
        request '默认都用 Gemini 3.1 Pro'."""
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text("{}", encoding="utf-8")

        import importlib
        import services.llm_registry as llm_registry
        llm_registry = importlib.reload(llm_registry)

        smart_keys = (
            "pass1", "pass2", "pass3",
            "translate", "rewrite",
            "probe_translate", "content_compliance",
        )
        actual = {k: llm_registry.get_prompt_model("smart", k) for k in smart_keys}
        assert all(v == "gemini_pro" for v in actual.values()), (
            f"All smart keys must default to gemini_pro; got: {actual}"
        )

    def test_admin_smart_override_takes_precedence(self, tmp_path, monkeypatch):
        """Admin can override smart-mode defaults via prompt_models.smart."""
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({
                "prompt_models": {
                    "smart": {
                        "translate": "deepseek_v4_pro",
                        "rewrite": "gemini_31_flash_lite",
                    },
                },
            }),
            encoding="utf-8",
        )

        import importlib
        import services.llm_registry as llm_registry
        llm_registry = importlib.reload(llm_registry)

        # Admin override
        assert llm_registry.get_prompt_model("smart", "translate") == "deepseek_v4_pro"
        assert llm_registry.get_prompt_model("smart", "rewrite") == "gemini_31_flash_lite"
        # Unset keys still fall to the smart default
        assert llm_registry.get_prompt_model("smart", "pass1") == "gemini_pro"

    def test_studio_and_express_defaults_unchanged_by_smart_addition(
        self, tmp_path, monkeypatch,
    ):
        """Adding smart-mode defaults MUST NOT change studio/express behavior.

        Before this feature, studio/express used the flat ``_DEFAULTS``
        fallback (because they too had no per-mode default lookup).
        Studio/express behavior is critical for existing users; we only
        add a NEW lookup path for smart without altering the flat
        fallback that studio/express depend on.
        """
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text("{}", encoding="utf-8")

        import importlib
        import services.llm_registry as llm_registry
        llm_registry = importlib.reload(llm_registry)

        # These reflect the flat ``_DEFAULTS`` — same as pre-fix behavior.
        assert llm_registry.get_prompt_model("studio", "translate") == "deepseek"
        assert llm_registry.get_prompt_model("express", "translate") == "deepseek"
        assert llm_registry.get_prompt_model("studio", "pass1") == "gemini_pro"

    def test_mode_defaults_constant_exposes_smart_entry(self):
        """Pin the existence of _MODE_DEFAULTS['smart'] for downstream
        callers (admin_settings.py UI imports this to render defaults
        in the smart tab dropdown)."""
        import importlib
        import services.llm_registry as llm_registry
        llm_registry = importlib.reload(llm_registry)

        assert hasattr(llm_registry, "_MODE_DEFAULTS"), (
            "llm_registry must expose _MODE_DEFAULTS so gateway/admin_settings.py "
            "can render the smart tab dropdown with correct pre-selected values"
        )
        smart_defaults = llm_registry._MODE_DEFAULTS.get("smart", {})
        assert smart_defaults, "_MODE_DEFAULTS['smart'] missing"
        assert all(v == "gemini_pro" for v in smart_defaults.values()), (
            f"All smart-mode defaults must be gemini_pro: {smart_defaults}"
        )

    def test_gemini_35_flash_is_selectable_audio_capable_model(self):
        """Gemini 3.5 Flash is opt-in via admin model selection."""
        import importlib
        import services.llm_registry as llm_registry
        llm_registry = importlib.reload(llm_registry)

        model = llm_registry.MODEL_REGISTRY["gemini_35_flash"]
        assert model["api_model_id"] == "gemini-3.5-flash"
        assert model["provider"] == "gemini"
        assert model["supports_audio"] is True

        pass1_options = {
            item["value"]
            for item in llm_registry.get_available_models_for_prompt("pass1")
        }
        assert "gemini_35_flash" in pass1_options

"""Regression guards for the `mimo_omni` → `mimo-v2.5` forced migration.

Plan 2026-05-27 Phase 0b: official `mimo-v2-omni` retires 2026-06-30
(auto-forwards to V2.5 from 2026-06-01). The logical model `mimo_omni`
is kept so historical admin settings keep resolving, but its
``api_model_id`` is repointed to ``mimo-v2.5`` (migration plan A) and it is
flagged ``deprecated``. Nothing in the runtime may still resolve to the
retiring ``mimo-v2-omni`` model id.
"""

from __future__ import annotations

import services.llm_registry as registry


def test_mimo_omni_logical_name_preserved() -> None:
    # Keep the key so existing admin_settings.prompt_models references load.
    assert "mimo_omni" in registry.MODEL_REGISTRY


def test_mimo_omni_resolves_to_v25_not_retiring_omni() -> None:
    assert registry.resolve_model_id("mimo_omni") == "mimo-v2.5"
    assert registry.resolve_model_id("mimo_omni") != "mimo-v2-omni"


def test_mimo_omni_marked_deprecated() -> None:
    assert registry.MODEL_REGISTRY["mimo_omni"].get("deprecated") is True


def test_no_registry_entry_targets_retiring_omni() -> None:
    # Source guard: no logical model may keep pointing at the dead model id.
    targets = {info.get("api_model_id") for info in registry.MODEL_REGISTRY.values()}
    assert "mimo-v2-omni" not in targets


def test_deprecated_model_surfaced_in_admin_listings() -> None:
    statuses = {m["value"]: m for m in registry.get_all_models_with_status()}
    assert statuses["mimo_omni"]["deprecated"] is True
    # A current model must not be flagged deprecated.
    assert statuses["mimo_v25"]["deprecated"] is False


def test_mimo_cost_ranks_unchanged() -> None:
    # PR 0b / PR 1 must NOT touch cost_rank — that would alter the global
    # fallback chain (get_fallback_candidates sorts by cost_rank). cost_rank
    # changes belong in Phase 4 / PR 5 with an explicit fallback diff.
    assert registry.MODEL_REGISTRY["mimo_v25"]["cost_rank"] == 2
    assert registry.MODEL_REGISTRY["mimo_v25_pro"]["cost_rank"] == 4
    assert registry.MODEL_REGISTRY["mimo_omni"]["cost_rank"] == 1


def test_get_prompt_model_warns_once_for_deprecated(monkeypatch, caplog) -> None:
    # Admin selected the deprecated logical model for a stage.
    monkeypatch.setattr(
        registry,
        "_load_settings",
        lambda: {"prompt_models": {"studio": {"pass1": "mimo_omni"}}},
    )
    registry._warned_deprecated_models.discard("mimo_omni")

    import logging

    with caplog.at_level(logging.WARNING):
        first = registry.get_prompt_model("studio", "pass1")
        second = registry.get_prompt_model("studio", "pass1")

    # Resolution itself is unchanged: the logical name is still returned.
    assert first == "mimo_omni"
    assert second == "mimo_omni"
    # Deduped: only one warning even across repeated calls.
    deprecation_warnings = [
        r for r in caplog.records if "deprecated" in r.getMessage()
    ]
    assert len(deprecation_warnings) == 1

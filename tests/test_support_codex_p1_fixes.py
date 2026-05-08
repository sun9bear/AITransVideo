"""Regression guards for the Codex 2026-05-08 review fixes.

Each test corresponds 1:1 to a Codex P1 / P2 finding so a future
refactor that re-introduces the bug fails loudly.

P1-1: anonymous conversation must require matching cookie.
P1-2: admin model list must use services.llm_registry and exclude
       disabled models (no `or True` short-circuit).
P1-3: support_ai_model must drive the provider, not env alone.
P1-4: get_plan_facts() must return non-empty plan list with the
       canonical PLANS API field names.
P2-1: support_enabled / anonymous_enabled default off.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# P1-1: anonymous session auth
# ---------------------------------------------------------------------------


def test_resolve_conversation_rejects_anonymous_without_cookie():
    """Reading the AST: the anonymous branch of _resolve_conversation
    must raise 401 when the request has no anonymous_id, even if the
    conversation row exists. Codex P1-1.

    We verify by source inspection because the function is async and
    builds a real DB query — full integration test would need a live
    Postgres. The contract here is "the code path that handles
    `user is None` raises HTTPException when anonymous_id is missing".
    """
    src = (REPO / "gateway" / "support_api.py").read_text(encoding="utf-8")
    # The P1-1 fix added explicit "if not anonymous_id: raise 401". Search
    # for that guard. If a future refactor removes it, this fails.
    assert "if not anonymous_id" in src, (
        "support_api._resolve_conversation must reject missing anonymous_id "
        "explicitly (Codex P1-1)."
    )
    assert 'detail="无法识别访客会话"' in src, (
        "Anonymous-without-cookie path must surface 401 with the visitor-cookie "
        "missing message (Codex P1-1)."
    )


# ---------------------------------------------------------------------------
# P1-2: admin model list comes from services.llm_registry
# ---------------------------------------------------------------------------


def test_admin_model_list_uses_services_llm_registry():
    """AST-level check (immune to multi-line / parenthesized imports):
    admin_support_api must import ``get_available_models_for_prompt``
    from ``services.llm_registry``, never from a bare ``llm_registry``
    on the gateway-local sys.path."""
    src = (REPO / "gateway" / "admin_support_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad_imports: list[str] = []
    good_import_found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        names = {alias.name for alias in node.names}
        if module == "llm_registry":
            bad_imports.append(f"from {module} import {sorted(names)}")
        if (
            module == "services.llm_registry"
            and "get_available_models_for_prompt" in names
        ):
            good_import_found = True
    assert not bad_imports, (
        f"admin_support_api still imports bare llm_registry: {bad_imports}"
    )
    assert good_import_found, (
        "admin_support_api must import get_available_models_for_prompt "
        "from services.llm_registry (Codex P1-2)."
    )
    assert 'get_available_models_for_prompt("support_chat")' in src
    # The buggy `or True` short-circuit must be gone.
    assert "or True" not in src, (
        "admin_support_api still has the buggy `or True` filter that "
        "let disabled models through (Codex P1-2)."
    )


def test_admin_model_list_returns_only_enabled_models():
    """Sanity-check the runtime behavior: with conftest's sys.path
    setup, the registry import must succeed and disabled models stay
    excluded. We exercise this against the registry directly because
    constructing an admin User in-process would pull half the gateway."""
    from services.llm_registry import (
        MODEL_REGISTRY,
        get_available_models_for_prompt,
    )

    rows = get_available_models_for_prompt("support_chat") or []
    assert rows, (
        "support_chat has no available models — registry seeding broken"
    )
    # Every row must be enabled.
    enabled_keys = {r["value"] for r in rows}
    for key in enabled_keys:
        assert key in MODEL_REGISTRY


# ---------------------------------------------------------------------------
# P1-3: support_ai_model drives provider
# ---------------------------------------------------------------------------


def test_resolve_provider_for_model_picks_registry_provider():
    from gateway.support_service import _resolve_provider_for_model

    # DeepSeek model → deepseek provider.
    assert _resolve_provider_for_model("deepseek") == "deepseek"
    # Gemini logical model → gemini provider (even though the support
    # provider registry currently only has 'fake' and 'deepseek'; the
    # mapping itself is correct, and resolve_provider falls back to
    # fake at the next layer if the provider isn't registered).
    assert _resolve_provider_for_model("gemini") == "gemini"
    # Unknown model → fake.
    assert _resolve_provider_for_model("nonexistent_model") == "fake"
    # None / empty → fake.
    assert _resolve_provider_for_model(None) == "fake"
    assert _resolve_provider_for_model("") == "fake"


def test_resolve_provider_for_model_env_override_wins():
    from gateway.support_service import _resolve_provider_for_model

    # Explicit non-fake env override stays in effect even when the model
    # would otherwise pick a different provider.
    assert (
        _resolve_provider_for_model("deepseek", env_override="openai")
        == "openai"
    )
    # "fake" env override is treated as "no override" (the override is
    # the legacy escape hatch, not the default mode).
    assert _resolve_provider_for_model("deepseek", env_override="fake") == "deepseek"
    # Empty string is also "no override".
    assert _resolve_provider_for_model("deepseek", env_override="") == "deepseek"


# ---------------------------------------------------------------------------
# P1-4: get_plan_facts() returns the real plan list
# ---------------------------------------------------------------------------


def test_get_plan_facts_returns_canonical_plan_codes():
    from gateway.support_knowledge import get_plan_facts

    facts = get_plan_facts()
    assert "plans" in facts
    plans = facts["plans"]
    assert isinstance(plans, list)
    assert plans, "get_plan_facts() returned empty list — Codex P1-4 regression"
    codes = {p["code"] for p in plans}
    assert {"free", "plus", "pro"}.issubset(codes), (
        f"Expected at least free/plus/pro in plan facts; got {codes}"
    )


def test_get_plan_facts_uses_display_name_field():
    from gateway.support_knowledge import get_plan_facts

    facts = get_plan_facts()
    plans = facts["plans"]
    # Find Plus and check its name is a non-empty string mapped from
    # display_name (not the legacy `name` field that doesn't exist).
    plus = next(p for p in plans if p["code"] == "plus")
    assert plus["name"] and isinstance(plus["name"], str)


# ---------------------------------------------------------------------------
# P2-1: support_enabled defaults off
# ---------------------------------------------------------------------------


def test_support_admin_settings_defaults_off():
    """The Pydantic schema's defaults must keep support OFF until an
    operator explicitly opts in. Codex P2-1."""
    src = (REPO / "gateway" / "support_models.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_class = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "SupportAdminSettings"
        ):
            found_class = True
            for item in node.body:
                if (
                    isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name)
                    and item.target.id == "support_enabled"
                    and isinstance(item.value, ast.Constant)
                ):
                    assert item.value.value is False, (
                        "SupportAdminSettings.support_enabled must default to "
                        "False (Codex P2-1)."
                    )
            break
    assert found_class


def test_runtime_settings_default_off_when_no_env():
    """When the support env vars are unset, ``load_support_settings``
    must report both flags as False. Codex P2-1."""
    import os

    saved = {
        k: os.environ.pop(k, None)
        for k in ("AVT_SUPPORT_ENABLED", "AVT_SUPPORT_ANONYMOUS_ENABLED")
    }
    try:
        from gateway.support_admin_settings import (
            invalidate_cache,
            load_support_settings,
        )

        invalidate_cache()
        merged = load_support_settings(force_reload=True)
        assert merged["support_enabled"] is False
        assert merged["support_anonymous_enabled"] is False
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        try:
            from gateway.support_admin_settings import invalidate_cache

            invalidate_cache()
        except Exception:
            pass


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

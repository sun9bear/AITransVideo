"""Regression guards for the Codex review round 2 (2026-05-08).

P1: DeepSeek stub must never be reported "ready" while
    ``_IMPLEMENTED_REAL_PROVIDERS`` is empty, regardless of the env
    state. The ``provider.reply()`` path must also catch
    NotImplementedError defensively and fall back to fake instead of
    surfacing a 500.

P2 (anonymous admin toggle): SupportAdminSettings exposes
    ``support_anonymous_enabled`` so an operator can flip the visitor
    chat path on without redeploying.

Nit (model implementation status): the admin model-options endpoint
    must annotate each row with ``support_implemented``.
"""
from __future__ import annotations

import asyncio
import os


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# P1: DeepSeek stub never marked ready
# ---------------------------------------------------------------------------


def test_implemented_real_providers_set_is_explicit_rendezvous():
    """The set tracks providers whose .reply() has reviewed HTTP wiring.

    As of 2026-05-08 the set contains exactly ``{"deepseek"}``. To add
    a new provider you MUST land all of:

      1. A real ``.reply()`` implementation (returns AIReply, never raises
         NotImplementedError).
      2. Unit tests with mocked HTTP exercising 200 / 4xx / 5xx /
         timeout / parse-failure paths.
      3. This test updated to the new expected set.
      4. ``test_support_ai_fake_provider.py`` updated if the provider
         class previously raised.

    Adding entries without (1)–(3) is a regression we want to catch
    here, not in production. Removing the existing ``deepseek`` entry
    without removing the wiring is also a regression.
    """
    from gateway.support_ai import _IMPLEMENTED_REAL_PROVIDERS

    assert _IMPLEMENTED_REAL_PROVIDERS == {"deepseek"}, (
        f"_IMPLEMENTED_REAL_PROVIDERS changed unexpectedly: "
        f"{_IMPLEMENTED_REAL_PROVIDERS}. See test docstring."
    )


def test_is_real_provider_ready_for_deepseek_tracks_api_key():
    """DeepSeek readiness: only True when (provider in implemented set)
    AND (DEEPSEEK_API_KEY set). Both conditions enforced.
    """
    from gateway.support_ai import is_real_provider_ready

    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    saved_config = os.environ.pop("AIVIDEOTRANS_CONFIG_DIR", None)
    try:
        # Force-clean admin override path so llm_registry's get_api_key
        # falls through to the env var only.
        os.environ["AIVIDEOTRANS_CONFIG_DIR"] = "/nonexistent/path/for/test"
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass

        # No API key → not ready, regardless of implemented set.
        assert is_real_provider_ready("deepseek") is False
        # API key set + provider in implemented set → ready.
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-ready-flag-only"
        assert is_real_provider_ready("deepseek") is True
        # Fake / unknown / empty / None → never ready.
        assert is_real_provider_ready("fake") is False
        assert is_real_provider_ready("") is False
        assert is_real_provider_ready(None) is False
        assert is_real_provider_ready("nonexistent") is False
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved
        os.environ.pop("AIVIDEOTRANS_CONFIG_DIR", None)
        if saved_config is not None:
            os.environ["AIVIDEOTRANS_CONFIG_DIR"] = saved_config
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# P1: NotImplementedError defensively caught
# ---------------------------------------------------------------------------


def test_deepseek_provider_has_real_reply_method():
    """DeepseekProvider.reply must NOT raise NotImplementedError as of
    2026-05-08 — the wired HTTP path is in place. If someone reverts to
    a stub, this test catches it.

    This test does NOT actually call DeepSeek; it uses a clean env to
    exercise the missing-API-key branch, which returns an AIReply
    object instead of raising.
    """
    from gateway.support_ai import AIReply, DeepseekProvider

    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    saved_config = os.environ.pop("AIVIDEOTRANS_CONFIG_DIR", None)
    try:
        os.environ["AIVIDEOTRANS_CONFIG_DIR"] = "/nonexistent/path/for/test"
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass
        provider = DeepseekProvider()
        # Should NOT raise NotImplementedError or any other exception.
        out = _run(
            provider.reply(
                message="x",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=5.0,
            )
        )
        assert isinstance(out, AIReply)
        # No API key path → polite busy reply, no token consumption.
        assert out.handoff_recommended is True
        assert out.input_tokens == 0
        assert out.output_tokens == 0
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved
        os.environ.pop("AIVIDEOTRANS_CONFIG_DIR", None)
        if saved_config is not None:
            os.environ["AIVIDEOTRANS_CONFIG_DIR"] = saved_config
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass


def test_support_service_has_not_implemented_fallback():
    """AST-level: support_service must catch NotImplementedError around
    provider.reply() and fall back to fake. Codex round 2 P1."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "gateway" / "support_service.py"
    ).read_text(encoding="utf-8")
    assert "except NotImplementedError" in src, (
        "support_service must wrap provider.reply() with "
        "`except NotImplementedError` so a stub provider never 500s."
    )
    assert 'resolve_provider("fake")' in src, (
        "After NotImplementedError, support_service must explicitly "
        "swap to the fake provider, not just retry the same one."
    )


# ---------------------------------------------------------------------------
# P2: anonymous toggle in admin Pydantic
# ---------------------------------------------------------------------------


def test_admin_settings_exposes_anonymous_toggle():
    from gateway.support_models import SupportAdminSettings

    fields = SupportAdminSettings.model_fields
    assert "support_anonymous_enabled" in fields, (
        "SupportAdminSettings must expose support_anonymous_enabled "
        "so admin can flip visitor chat without redeploying."
    )
    default = fields["support_anonymous_enabled"].default
    assert default is False, (
        "support_anonymous_enabled must default to False (Codex P2-1)."
    )


def test_admin_settings_projection_round_trips_anonymous_flag():
    from gateway.admin_support_api import _settings_to_model

    merged_on = {"support_anonymous_enabled": True}
    assert _settings_to_model(merged_on).support_anonymous_enabled is True
    merged_off = {"support_anonymous_enabled": False}
    assert _settings_to_model(merged_off).support_anonymous_enabled is False
    # Missing key — must default to False, not propagate "True from earlier".
    assert _settings_to_model({}).support_anonymous_enabled is False


# ---------------------------------------------------------------------------
# Nit: model list annotates support_implemented
# ---------------------------------------------------------------------------


def test_admin_model_list_annotates_support_implemented():
    """Each model row carries ``support_implemented``; the value matches
    whether the model's provider is in ``_IMPLEMENTED_REAL_PROVIDERS``.

    As of 2026-05-08 only ``deepseek`` is wired, so:
      - deepseek (and any future deepseek_* variant) → True
      - everything else → False (silent fallback to fake)
    """
    from gateway.admin_support_api import _list_text_models
    from gateway.support_ai import _IMPLEMENTED_REAL_PROVIDERS

    rows = _list_text_models()
    assert rows, "expected at least one model row"
    saw_implemented = False
    saw_unimplemented = False
    for row in rows:
        assert "support_implemented" in row, (
            f"Model row {row!r} missing support_implemented flag — "
            "Codex round 2 nit."
        )
        provider = (row.get("provider") or "").strip().lower()
        expected = provider in _IMPLEMENTED_REAL_PROVIDERS
        assert row["support_implemented"] is expected, (
            f"Model {row.get('value')} (provider={provider!r}) reported "
            f"support_implemented={row['support_implemented']}, expected {expected}"
        )
        if row["support_implemented"]:
            saw_implemented = True
        else:
            saw_unimplemented = True

    # Sanity: as long as the registry has at least one deepseek model
    # AND at least one non-deepseek model, both branches should appear.
    # If this fails, either the registry shape changed or
    # _IMPLEMENTED_REAL_PROVIDERS no longer contains deepseek.
    assert saw_implemented, (
        "No model row reported support_implemented=True. Either the "
        "registry no longer contains a deepseek model, or "
        "_IMPLEMENTED_REAL_PROVIDERS lost its deepseek entry."
    )
    assert saw_unimplemented, (
        "Every model reported support_implemented=True. That would mean "
        "_IMPLEMENTED_REAL_PROVIDERS now covers every registered provider — "
        "verify intentional and update this guard."
    )


# ---------------------------------------------------------------------------
# Plan doc sync
# ---------------------------------------------------------------------------


def test_plan_doc_anonymous_default_is_false():
    """Codex P2 round 2: the plan §11 env table must reflect the
    actual default ``AVT_SUPPORT_ANONYMOUS_ENABLED=false``."""
    from pathlib import Path

    plan = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "plans"
        / "2026-05-08-ai-customer-support-handoff-plan.md"
    ).read_text(encoding="utf-8")
    assert "AVT_SUPPORT_ANONYMOUS_ENABLED=false" in plan
    assert "AVT_SUPPORT_ANONYMOUS_ENABLED=true" not in plan

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


def test_implemented_real_providers_is_empty_in_p1():
    """Future-proof: if someone adds DeepSeek (or any other provider)
    to the implemented set, this test must be updated AND the
    corresponding HTTP wiring + test coverage must land at the same
    time. Adding entries here is a deliberate rendezvous point with a
    code review."""
    from gateway.support_ai import _IMPLEMENTED_REAL_PROVIDERS

    assert _IMPLEMENTED_REAL_PROVIDERS == set(), (
        "_IMPLEMENTED_REAL_PROVIDERS must stay empty until a real "
        "provider's .reply() is implemented and reviewed. Adding a "
        "provider here without wiring will surface NotImplementedError "
        "to end users."
    )


def test_is_real_provider_ready_returns_false_for_deepseek_with_api_key():
    """Even with DEEPSEEK_API_KEY set (likely in production for
    translation), is_real_provider_ready must return False because
    DeepSeek isn't in the implemented set yet."""
    from gateway.support_ai import is_real_provider_ready

    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        os.environ["DEEPSEEK_API_KEY"] = "sk-fake-test-key-must-not-trigger-real-call"
        assert is_real_provider_ready("deepseek") is False
        assert is_real_provider_ready("fake") is False
        assert is_real_provider_ready("") is False
        assert is_real_provider_ready(None) is False
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved


# ---------------------------------------------------------------------------
# P1: NotImplementedError defensively caught
# ---------------------------------------------------------------------------


def test_deepseek_stub_still_raises_not_implemented_error():
    """The stub itself must keep raising — the fallback in
    support_service is what swallows it. If someone removes the raise
    by accident, the test catches it before deploy."""
    from gateway.support_ai import DeepseekProvider

    provider = DeepseekProvider()
    raised = False
    try:
        _run(
            provider.reply(
                message="x",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=5.0,
            )
        )
    except NotImplementedError:
        raised = True
    assert raised, "DeepseekProvider.reply must raise NotImplementedError in P1"


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
    from gateway.admin_support_api import _list_text_models

    rows = _list_text_models()
    assert rows, "expected at least one model row"
    for row in rows:
        assert "support_implemented" in row, (
            f"Model row {row!r} missing support_implemented flag — "
            "Codex round 2 nit."
        )
        # P1 implemented set is empty, so every row must report False.
        assert row["support_implemented"] is False, (
            f"Model {row.get('value')} reported support_implemented=True "
            "but _IMPLEMENTED_REAL_PROVIDERS is empty in P1."
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

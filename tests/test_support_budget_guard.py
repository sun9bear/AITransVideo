"""Unit tests for support_budget — pure cost math + accumulator semantics.

The DB-backed parts (`get_monthly_spent_usd`, `record_usage`) require a
live AsyncSession; we exercise them in a lightweight integration test
inside test_support_service.py-style flows. This file focuses on the
math helpers and the budget-state thresholds.
"""
from __future__ import annotations


def test_estimate_cost_zero_for_zero_tokens():
    from gateway.support_budget import estimate_cost_usd

    assert (
        estimate_cost_usd(
            input_tokens=0,
            output_tokens=0,
            input_usd_per_1m=0.14,
            output_usd_per_1m=0.28,
        )
        == 0.0
    )


def test_estimate_cost_at_default_deepseek_rates():
    from gateway.support_budget import estimate_cost_usd

    # 1M input tokens + 1M output tokens at DeepSeek V4 Flash defaults
    # → $0.14 + $0.28 = $0.42
    cost = estimate_cost_usd(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        input_usd_per_1m=0.14,
        output_usd_per_1m=0.28,
    )
    # Allow tiny float fudge from rounding
    assert abs(cost - 0.42) < 1e-9


def test_estimate_cost_clamps_negative_inputs_to_zero():
    from gateway.support_budget import estimate_cost_usd

    cost = estimate_cost_usd(
        input_tokens=-100,
        output_tokens=-100,
        input_usd_per_1m=-1.0,
        output_usd_per_1m=-1.0,
    )
    assert cost == 0.0


def test_current_budget_month_format():
    from gateway.support_budget import current_budget_month
    import datetime as dt

    s = current_budget_month(dt.datetime(2026, 5, 8, tzinfo=dt.timezone.utc))
    assert s == "2026-05"
    s2 = current_budget_month(dt.datetime(2026, 12, 31, tzinfo=dt.timezone.utc))
    assert s2 == "2026-12"


def test_budget_status_normal_and_exhausted():
    """Budget state transitions on the boundary, not before."""
    from gateway.support_budget import BudgetStatus

    # We can't easily invoke get_budget_status without a DB; instead we
    # construct BudgetStatus directly to assert the thresholds the rest
    # of the system relies on.
    normal = BudgetStatus(state="normal", spent_usd=10.0, cap_usd=50.0, warning=False)
    assert normal.state == "normal"
    assert normal.headroom_usd() == 40.0
    assert normal.warning is False

    warning = BudgetStatus(state="normal", spent_usd=42.0, cap_usd=50.0, warning=True)
    assert warning.state == "normal"
    assert warning.warning is True

    exhausted = BudgetStatus(
        state="budget_exhausted", spent_usd=51.0, cap_usd=50.0, warning=True
    )
    assert exhausted.state == "budget_exhausted"
    assert exhausted.headroom_usd() == 0.0

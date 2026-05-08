"""AI cost ledger + monthly budget guard for the customer support flow.

Plan §5.6 — the project's hard constraint is "no silent paid-API drift",
so every real LLM call writes a row to ``support_ai_usage`` and the
monthly accumulator decides whether the next call goes through or
falls back to template / handoff.

Two states only (P1):

- ``normal`` — accumulator below 100 % of the monthly cap.
- ``budget_exhausted`` — accumulator at or over the monthly cap. New
  LLM calls are short-circuited; templates and handoff still work.

The 80 % threshold mentioned in the plan is treated as an alert signal
(``warning`` flag returned alongside the state) but does NOT change
routing. Plan §5.6 deliberately avoids fractional / random degradation
to keep behavior reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import SupportAIUsage


BudgetState = Literal["normal", "budget_exhausted"]


@dataclass(frozen=True)
class BudgetStatus:
    state: BudgetState
    spent_usd: float
    cap_usd: float
    warning: bool  # True when spent >= 80% of cap (alert-only signal)

    def headroom_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)


def current_budget_month(now: datetime | None = None) -> str:
    """Return the YYYY-MM string the accumulator groups by."""
    n = now or datetime.now(timezone.utc)
    return n.strftime("%Y-%m")


def estimate_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_usd_per_1m: float,
    output_usd_per_1m: float,
) -> float:
    """Compute ``estimated_cost_usd`` from token counts and admin-set rates.

    We deliberately do NOT consume any cost field returned by the provider
    response. Plan §7.2 — providers either don't return costs, or return
    them in inconsistent units. The accumulator must be auditable from
    pure (token_count × admin_rate) math.
    """
    in_cost = max(0, input_tokens) / 1_000_000.0 * max(0.0, input_usd_per_1m)
    out_cost = max(0, output_tokens) / 1_000_000.0 * max(0.0, output_usd_per_1m)
    return round(in_cost + out_cost, 6)


async def get_monthly_spent_usd(
    db: AsyncSession,
    *,
    month: str | None = None,
) -> float:
    """Sum ``estimated_cost_usd`` across the given month."""
    target = month or current_budget_month()
    stmt = select(func.coalesce(func.sum(SupportAIUsage.estimated_cost_usd), 0.0)).where(
        SupportAIUsage.budget_month == target
    )
    result = await db.execute(stmt)
    value = result.scalar()
    return float(value or 0.0)


async def get_budget_status(
    db: AsyncSession,
    *,
    cap_usd: float,
    month: str | None = None,
) -> BudgetStatus:
    spent = await get_monthly_spent_usd(db, month=month)
    state: BudgetState = "budget_exhausted" if spent >= cap_usd > 0 else "normal"
    warning = cap_usd > 0 and spent >= 0.8 * cap_usd
    return BudgetStatus(state=state, spent_usd=spent, cap_usd=cap_usd, warning=warning)


async def record_usage(
    db: AsyncSession,
    *,
    conversation_id,
    user_id,
    anonymous_id: str | None,
    provider: str,
    model: str,
    route: Literal["template", "faq", "llm", "handoff"],
    input_tokens: int = 0,
    output_tokens: int = 0,
    input_usd_per_1m: float = 0.0,
    output_usd_per_1m: float = 0.0,
) -> None:
    """Append a ledger row.

    Templates and FAQ paths still write a row (with cost=0) so the admin
    dashboard can compute "% of conversations short-circuited by template".
    """
    row = SupportAIUsage(
        conversation_id=conversation_id,
        user_id=user_id,
        anonymous_id=anonymous_id,
        provider=provider,
        model=model,
        input_tokens=int(max(0, input_tokens)),
        output_tokens=int(max(0, output_tokens)),
        input_usd_per_1m_tokens=float(max(0.0, input_usd_per_1m)),
        output_usd_per_1m_tokens=float(max(0.0, output_usd_per_1m)),
        estimated_cost_usd=estimate_cost_usd(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_usd_per_1m=input_usd_per_1m,
            output_usd_per_1m=output_usd_per_1m,
        ),
        budget_month=current_budget_month(),
        route=route,
    )
    db.add(row)
    await db.flush()

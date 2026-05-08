"""Chatwoot adapter — P3 stub.

Plan §9.2 / §13 P3: only deploy Chatwoot after the email channel
demonstrates real ticket volume. Until that happens, this adapter
intentionally raises ``NotImplementedError`` so any code path that tries
to route through Chatwoot is loud rather than silent.

To activate later:

1. Deploy a Chatwoot instance.
2. Set ``AVT_CHATWOOT_*`` env vars per ``config.py``.
3. Replace this stub with real ``httpx`` calls into the Chatwoot API.
4. Update ``support_handoff.create_handoff`` to dispatch ``provider="chatwoot"``.
"""

from __future__ import annotations

from typing import Any


async def send_chatwoot_handoff(**_: Any) -> dict[str, Any]:
    raise NotImplementedError(
        "Chatwoot adapter is intentionally a stub in P1. "
        "Plan §9.2 / §13 P3 — Chatwoot deploy is deferred until email "
        "ticket volume justifies the additional infra."
    )

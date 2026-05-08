"""WeChat customer service (微信客服) adapter — P4/P5 stub.

Plan §9.4 splits this into two phases:

- P4 — link/QR entrypoint only. No backend integration; users follow a
  WeChat link and the conversation happens on WeChat directly.
- P5 — real API: callback verification, message decrypt, access-token
  management, chatbot-account linking.

Neither phase is implemented in P1. The adapter raises so accidental
usage surfaces immediately.
"""

from __future__ import annotations

from typing import Any


async def send_wechat_kf_handoff(**_: Any) -> dict[str, Any]:
    raise NotImplementedError(
        "WeChat customer service adapter is a P4/P5 stub. "
        "P1 does not route handoffs to WeChat; surface the contact link "
        "on the marketing page if WeChat triage is desired."
    )

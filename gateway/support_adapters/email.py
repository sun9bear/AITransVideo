"""Email handoff adapter — P2 default human channel.

P1 ships with a "log-only" implementation: when a handoff is created,
this adapter writes a structured ticket entry to ``support_handoff_*.log``
inside the runtime logs dir and returns success. Real SMTP wiring is
deferred until a real ops process is in place; the operator runs on the
log file initially (the plan §9.1 explicitly said "verify ticket volume
before adding more infra").

If ``AVT_SUPPORT_SMTP_*`` env vars are set later, this adapter will be
extended to actually send mail. The function signature is stable so the
admin UI / ``support_handoff`` orchestrator do not need to change.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _runtime_logs_dir() -> Path:
    """Resolve the runtime logs directory (bind-mounted in production).

    Falls back to ``/tmp/aivideotrans/runtime_logs`` for local dev where
    the bind mount may not exist. The directory is created if missing.
    """
    base = os.environ.get(
        "AIVIDEOTRANS_RUNTIME_LOGS_DIR",
        "/opt/aivideotrans/data/runtime_logs",
    )
    p = Path(base)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        fallback = Path("/tmp/aivideotrans/runtime_logs")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


async def send_handoff_email(
    *,
    to_email: str,
    conversation_id: str,
    user_id: str | None,
    user_email: str | None,
    user_phone: str | None,
    plan_code: str | None,
    page_url: str | None,
    job_id: str | None,
    reason: str,
    summary: str,
    last_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Record a handoff "email" — currently writes a JSONL log line.

    Returns a payload suitable for ``support_handoff_requests.provider_payload``.

    The structured fields chosen here are the ones an operator actually
    needs to triage: who the user is, what page they were on, the
    bucketed reason, and the last few messages so they don't have to ask
    the user to repeat themselves.
    """
    if not to_email or "@" not in to_email:
        raise ValueError("invalid ops_email")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "to": to_email,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "user_email": user_email,
        "user_phone": user_phone,
        "plan_code": plan_code,
        "page_url": page_url,
        "job_id": job_id,
        "reason": reason,
        "summary": summary,
        "last_messages": last_messages,
    }
    log_path = _runtime_logs_dir() / "support_handoff_email.log"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Failed to append handoff email log: %s", exc)
        # Do NOT raise — the handoff record itself was written to the DB
        # by the caller; the log is best-effort triage data.
    logger.info(
        "support handoff queued: conversation=%s reason=%s to=%s",
        conversation_id,
        reason,
        to_email,
    )
    return {
        "channel": "email_log",
        "recipient": to_email,
        "logged_at": record["ts"],
    }

"""Phase 2a LAUNCH GATE — free-tier voice-rights consent validator (HARD-fail).

The free tier's MiMo voiceclone reproduces the SOURCE video speaker's voice, so
《民法典》第 1023 条 requires the user to attest they hold the rights to that content
and to reproducing its speakers' voices (design §5.3 / plan
2026-05-30-mimo-free-tier-launch-gate.md).

HARD-fail (unlike the soft-skip ``gateway/express_consent.py``; like the hard-fail
``gateway/smart_consent.py``): a free job WITHOUT confirmed consent is rejected at
create time. ``validate_free_consent`` returns a payload ONLY when
``voice_rights_confirmed`` is exactly ``True``; otherwise ``(None, reason)`` and the
caller returns ``403 consent_required``.

Schema (strict types — no int/str coercion, mirrors express_consent strict-bool):
- ``voice_rights_confirmed: bool`` — required, must be ``True``.
- ``client_confirmed_at: str | None`` — untrusted UI timestamp (audit assist only;
  the authoritative time is the ``server_confirmed_at`` the caller stamps).
"""
from __future__ import annotations

from typing import Any


def validate_free_consent(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the ``free_consent`` payload (HARD).

    Returns ``(payload, None)`` ONLY when ``raw`` is a dict with
    ``voice_rights_confirmed is True``. Otherwise ``(None, reason)``:

    - ``free_consent_missing_or_invalid_type``: not a dict (absent / wrong type)
    - ``voice_rights_not_confirmed``: field absent, or present and not ``True``
    - ``voice_rights_confirmed_not_bool``: present but not a bool (rejects 1 / "true")
    - ``client_confirmed_at_not_string``: present but not a string

    The caller stamps the authoritative ``server_confirmed_at`` on the payload.
    """
    if not isinstance(raw, dict):
        return None, "free_consent_missing_or_invalid_type"

    if "voice_rights_confirmed" not in raw:
        return None, "voice_rights_not_confirmed"
    confirmed = raw["voice_rights_confirmed"]
    if not isinstance(confirmed, bool):
        # Reject int (0/1) and str ("true"/"false") so accidental coercion
        # cannot manufacture consent. Matches express/smart strict-bool style.
        return None, "voice_rights_confirmed_not_bool"
    if confirmed is not True:
        return None, "voice_rights_not_confirmed"

    client_confirmed_at: str | None = None
    if "client_confirmed_at" in raw and raw["client_confirmed_at"] is not None:
        if not isinstance(raw["client_confirmed_at"], str):
            return None, "client_confirmed_at_not_string"
        normalized = raw["client_confirmed_at"].strip()
        if normalized:
            client_confirmed_at = normalized

    return {
        "voice_rights_confirmed": True,
        "client_confirmed_at": client_confirmed_at,
    }, None


__all__ = ["validate_free_consent"]

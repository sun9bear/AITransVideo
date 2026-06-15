"""Smart MVP P3-b Phase 2: post-settle cost_summary backfill helper.

Per decision log §2 Phase 2: after Gateway's settlement (credit ledger
+ quota lookup) finishes, the ``audit/smart_cost_summary.json`` file
on disk has its two ``pending_*`` fields replaced with real values.

Field-level semantics:

  - ``pending_credits_charged`` ← net credits captured from this job's
    CreditsLedger entries (sum capture deltas - sum refund/rollback
    deltas). For ``capture_full`` smart jobs this matches the value
    captured. For ``refund_full`` it's 0. For partial capture
    (``capture_actual_cost_capped_at_studio_price``) it's the net
    after refund.
  - ``cost_breakdown_internal_only.pending_minimax_quota_used_after``
    ← current ``used`` count from the user's voice library quota
    snapshot at settle time.

Failure semantics:

  - Non-smart / missing project_dir / missing cost_summary.json →
    return False, no-op.
  - Malformed file / I/O error → log + return False (must NOT block
    the mirror callback per plan §6.4 末段).
  - ``quota_used=None`` upstream (Codex 第二十七轮 P0 fail-closed
    contract — unknown quota stays None rather than faking a value)
    → leave that field as None, still backfill credits.

Idempotency: calling twice with the same inputs produces the same
numeric fields. ``settled_at`` timestamp differs across calls but
admin tooling reads it for "when was this backfilled" not for
content-hash purposes.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)

_COST_SUMMARY_FILENAME = "smart_cost_summary.json"
_CAPTURE_DIRECTIONS = frozenset({"capture"})
# Reversal directions — subtract from gross captures to get NET charged.
# "release" is a reserve-back-out, not a capture reversal, so excluded.
_REFUND_DIRECTIONS = frozenset({"refund", "rollback"})


def _compute_net_credits_charged(credit_entries: Iterable[Any]) -> int:
    """Sum captures - refunds/rollbacks. Uses abs() per
    gateway/credits_service.py:693 convention (sign inconsistent
    across paths)."""
    captured = 0
    refunded = 0
    for entry in credit_entries or []:
        direction = str(getattr(entry, "direction", "") or "")
        delta = int(abs(getattr(entry, "credits_delta", 0) or 0))
        if direction in _CAPTURE_DIRECTIONS:
            captured += delta
        elif direction in _REFUND_DIRECTIONS:
            refunded += delta
    return max(0, captured - refunded)


def _atomic_write_json(target: Path, payload: dict) -> None:
    """Write JSON atomically via temp file + os.replace. Mirrors the
    pattern in services.smart.sidecar_emitter._atomic_write_json but
    Gateway-side (we don't import pipeline modules)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(encoded)
            tmp_f.flush()
            os.fsync(tmp_f.fileno())
        os.replace(tmp_path_str, str(target))
    except Exception:
        # Clean up temp file on failure.
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


def backfill_smart_cost_summary(
    *,
    service_mode: str | None,
    project_dir: str | None,
    credit_entries: Iterable[Any],
    quota_used: int | None,
    carryover_applied_credits: int | None = None,
    carryover_source_job_id: str | None = None,
) -> bool:
    """Read-modify-write ``{project_dir}/audit/smart_cost_summary.json``
    with post-settle real values for the two ``pending_*`` fields.

    P3e D-C (CodeX 复审 P1): ``carryover_applied_credits`` /
    ``carryover_source_job_id`` (read by the caller from
    ``db_job.metering_snapshot``, stamped at settle by
    ``_smart_clone_minute_offset``) are written into
    ``cost_breakdown_internal_only`` so the convert minute减免 is
    **auditable** — otherwise a convert F's lower ``pending_credits_charged``
    looks like an unexplained under-charge. None → not a convert / no
    carryover → field omitted (inert).

    Returns True when the file was updated successfully, False
    otherwise (non-smart job / missing project_dir / missing file /
    malformed file / I/O failure).
    """
    if (service_mode or "").lower() != "smart":
        return False
    if not project_dir:
        return False

    target = Path(project_dir) / "audit" / _COST_SUMMARY_FILENAME
    if not target.is_file():
        return False

    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "backfill: failed to read cost_summary at %s: %s",
            target, exc,
        )
        return False

    if not isinstance(existing, dict):
        logger.warning(
            "backfill: cost_summary at %s is not a dict (got %r)",
            target, type(existing).__name__,
        )
        return False

    # Compute net credits from ledger entries.
    net_charged = _compute_net_credits_charged(credit_entries)

    # Backfill top-level pending_credits_charged.
    updated = dict(existing)
    updated["pending_credits_charged"] = int(net_charged)

    # Backfill cost_breakdown.pending_minimax_quota_used_after.
    breakdown = dict(updated.get("cost_breakdown_internal_only") or {})
    if quota_used is not None:
        breakdown["pending_minimax_quota_used_after"] = int(quota_used)
    # When quota_used is None, leave breakdown field as-is (preserves
    # the original null from pipeline emit time — admin sees "待查询"
    # rather than a fake 0 per Codex 第二十七轮 P0).
    # P3e D-C (CodeX P1): make the convert 600-carryover minute减免 auditable
    # in cost summary. Only stamped when a positive carryover was applied
    # (convert F); single-task full-smart / non-convert → omitted (inert).
    if carryover_applied_credits:
        breakdown["clone_carryover_applied_credits"] = int(carryover_applied_credits)
        if carryover_source_job_id:
            breakdown["clone_carryover_source_job_id"] = str(carryover_source_job_id)
    updated["cost_breakdown_internal_only"] = breakdown

    # Stamp settled_at so admin tooling can distinguish pre/post
    # settle.
    updated["settled_at"] = datetime.now(timezone.utc).isoformat()

    try:
        _atomic_write_json(target, updated)
    except Exception as exc:
        logger.warning(
            "backfill: atomic write failed for %s: %s",
            target, exc,
        )
        return False
    return True


__all__ = ["backfill_smart_cost_summary"]

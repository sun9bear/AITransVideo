"""Smart MVP §6.4 — sidecar trio emitter (PR#3A, business-logic only).

Three sinks per plan §6.4 + §12 (separation-of-concerns enforced):

  - ``smart_decisions.jsonl`` — append-only audit of automatic decisions
    (speaker_gate / voice_clone / voice_selection_auto_approve /
    translation_auto_approve / tts_retry / split_proposal /
    downgrade_handoff / budget_exhausted). One line per decision event.
  - ``smart_quality_report.json`` — terminal-time aggregate of how the
    Smart job behaved (per-speaker decisions, retry counts, drift, etc.)
  - ``smart_cost_summary.json`` — terminal-time internal cost summary
    (UsageMeter-derived; admin-only display per §7.3 / Codex Q2)

Boundary discipline (plan §12 + §6.4):
  - These sinks are SYSTEM behaviour records — NOT user behaviour.
    user_edit_events.jsonl stays untouched by this module.
  - smart_decisions.jsonl uses the SAME ``audit/`` subdir as
    user_edit_events.jsonl (per plan §12 末段) so admin tooling has
    one bind-mounted location to scrape.
  - All three sinks carry ``schema_version: 1`` for forward compat.

Failure semantics (plan §6.4 末段 + 风险表 §11):
  - emit failure must NOT block the user-facing pipeline. logger.exception
    inline + return False so caller can record a JobEvent WARNING for
    gateway visibility.
  - The QA report rendering layer reads the three sinks; missing files
    are surfaced to the user as "数据不完整, 缺失 <X>" rather than
    pretending data exists.

This module is the only Smart module that does file I/O. It uses
``services._file_lock.file_lock`` to serialise the load → modify → save
sequence on the JSON registries (compat with the existing
voice_registry pattern + multi-process safety on Windows).

Acceptance tests in tests/test_smart_business_logic.py.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from services._file_lock import file_lock


logger = logging.getLogger(__name__)


# Schema versions — bump on shape change so QA report renderer can branch.
SMART_DECISIONS_SCHEMA_VERSION = 1
SMART_QUALITY_REPORT_SCHEMA_VERSION = 1
SMART_COST_SUMMARY_SCHEMA_VERSION = 1


# Allowed decision_type values per plan §4.4. Keeping it as a frozen set
# means typos surface here (TypeError on unknown), not as silently-
# malformed audit lines.
_ALLOWED_DECISION_TYPES = frozenset(
    {
        "speaker_gate",
        "voice_clone",
        "voice_selection_auto_approve",
        "translation_auto_approve",
        "tts_retry",
        "split_proposal",
        "downgrade_handoff",
        "budget_exhausted",
    }
)

# Allowed decision values — same rationale as above.
_ALLOWED_DECISIONS = frozenset({"approved", "rejected", "deferred"})


# ---------------------------------------------------------------------------
# Path helpers — separated so tests can override the audit subdir name
# ---------------------------------------------------------------------------


_AUDIT_SUBDIR = "audit"
_SMART_DECISIONS_FILENAME = "smart_decisions.jsonl"
_SMART_QUALITY_REPORT_FILENAME = "smart_quality_report.json"
_SMART_COST_SUMMARY_FILENAME = "smart_cost_summary.json"


def _audit_dir(project_dir: Path) -> Path:
    """Return ``{project_dir}/audit`` (created if missing)."""
    audit = project_dir / _AUDIT_SUBDIR
    audit.mkdir(parents=True, exist_ok=True)
    return audit


def smart_decisions_path(project_dir: Path) -> Path:
    return _audit_dir(project_dir) / _SMART_DECISIONS_FILENAME


def smart_quality_report_path(project_dir: Path) -> Path:
    return _audit_dir(project_dir) / _SMART_QUALITY_REPORT_FILENAME


def smart_cost_summary_path(project_dir: Path) -> Path:
    return _audit_dir(project_dir) / _SMART_COST_SUMMARY_FILENAME


# ---------------------------------------------------------------------------
# emit_smart_decision — append-only JSONL
# ---------------------------------------------------------------------------


def emit_smart_decision(
    project_dir: Path,
    *,
    decision_type: str,
    decision: str,
    evidence: Mapping[str, Any] | None = None,
    reason_code: str | None = None,
    smart_decision_id: str,
    created_at: str,
    auto_approved: bool = True,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """Append one decision event to ``smart_decisions.jsonl``.

    Returns:
      True on successful write. False if the write failed (logged as
      exception). Caller should observe the False return and emit a
      gateway-visible JobEvent WARNING per plan §6.4 末段.

    Raises:
      ValueError: when decision_type or decision is not in the allowed
      enum. This is a programming-time error (typo) rather than runtime
      I/O; failing fast makes the typo obvious.

    Schema (matches plan §4.4):
      {
        "schema_version": 1,
        "event_id": "<smart_decision_id>",
        "decision_type": "<decision_type>",
        "decision": "<decision>",
        "evidence": {...},
        "reason_code": "<reason_code>",
        "auto_approved": true,
        "created_at": "<iso8601>",
        "smart_decision_id": "<smart_decision_id>",
        ...extra
      }
    """
    if decision_type not in _ALLOWED_DECISION_TYPES:
        raise ValueError(
            f"unknown decision_type {decision_type!r}; "
            f"allowed: {sorted(_ALLOWED_DECISION_TYPES)}"
        )
    if decision not in _ALLOWED_DECISIONS:
        raise ValueError(
            f"unknown decision {decision!r}; allowed: {sorted(_ALLOWED_DECISIONS)}"
        )
    if not smart_decision_id:
        raise ValueError("smart_decision_id is required")

    line: dict[str, Any] = {
        "schema_version": SMART_DECISIONS_SCHEMA_VERSION,
        "event_id": smart_decision_id,
        "decision_type": decision_type,
        "decision": decision,
        "evidence": dict(evidence or {}),
        "reason_code": reason_code,
        "auto_approved": bool(auto_approved),
        "created_at": created_at,
        "smart_decision_id": smart_decision_id,
    }
    if extra:
        # extra fields land at the top level so the JSONL stays one-flat-
        # object per line (matches user_edit_events.jsonl shape).
        for k, v in extra.items():
            if k in line:
                # Don't let extra clobber required fields.
                continue
            line[k] = v

    encoded = json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n"
    # Codex 第九轮 P1-4: path/dir computation can also fail (mkdir
    # permission errors, disk full). Pull both into the try so any I/O
    # failure returns False rather than bubbling up — plan §6.4 末段
    # explicitly requires "emit failure must NOT block the user-facing
    # pipeline".
    try:
        target = smart_decisions_path(project_dir)
        with file_lock(target):
            with open(target, "a", encoding="utf-8") as fp:
                fp.write(encoded)
        return True
    except Exception:
        logger.exception(
            "smart.sidecar.decision_emit_failed: project_dir=%s decision_type=%s "
            "decision_id=%s — caller should emit a JobEvent WARNING",
            project_dir, decision_type, smart_decision_id,
        )
        return False


# ---------------------------------------------------------------------------
# write_smart_quality_report / write_smart_cost_summary — atomic full writes
# ---------------------------------------------------------------------------


def _atomic_write_json(target: Path, payload: Mapping[str, Any]) -> bool:
    """Write JSON via tempfile + os.replace under file_lock so concurrent
    readers never see a half-written file. Mirrors the
    voice_registry.save() pattern called out in _file_lock.py docstring.

    Returns True on success, False (with logged exception) on failure.
    """
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        # Codex 第九轮 P1-4 (atomic-write counterpart): the parent.mkdir
        # call must also live inside this try so a permission/disk error
        # returns False rather than bubbling up. Same plan §6.4 末段
        # rationale as emit_smart_decision.
        target.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(target):
            with open(tmp_path, "w", encoding="utf-8") as fp:
                fp.write(encoded)
            os.replace(tmp_path, target)
        return True
    except Exception:
        logger.exception(
            "sidecar emitter atomic write failed: target=%s", target
        )
        # Best-effort cleanup of the half-written tmp.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False


def _stamp_schema_version(payload: Mapping[str, Any], version: int) -> dict[str, Any]:
    """Codex 第九轮 P2: schema_version must be authoritative — payload
    cannot override it. Earlier ``{"schema_version": v, **payload}`` form
    let callers (accidentally or via shape drift) clobber the version
    stamp, breaking renderers that branch on it.

    Build the dict with payload first then stamp the version on top so
    the version is always exactly what this module declared.
    """
    final = dict(payload)
    final["schema_version"] = version
    return final


def write_smart_quality_report(
    project_dir: Path,
    payload: Mapping[str, Any],
) -> bool:
    """Atomic write of the terminal-time quality report.

    Caller is responsible for building ``payload`` (per-speaker decisions,
    retry counts, subtitle drift refs, etc. — see plan §4.5). This function
    just stamps schema_version + persists.

    Returns False on I/O failure; caller emits JobEvent WARNING and the
    QA report renderer downgrades the section per plan §6.4 末段.
    """
    target = _safe_path(project_dir, smart_quality_report_path)
    if target is None:
        return False
    return _atomic_write_json(
        target,
        _stamp_schema_version(payload, SMART_QUALITY_REPORT_SCHEMA_VERSION),
    )


def write_smart_cost_summary(
    project_dir: Path,
    payload: Mapping[str, Any],
) -> bool:
    """Atomic write of the terminal-time cost summary (admin-only display).

    Caller derives ``payload`` from UsageMeter.summarize() per plan §4.6.
    This function stamps schema_version + persists. Display layer is
    admin-only per Codex Q2 (user QA report does NOT show cost / margin).

    Returns False on I/O failure; caller emits JobEvent WARNING and admin
    dashboard marks ``cost_summary_missing`` per plan §6.4 末段.
    """
    target = _safe_path(project_dir, smart_cost_summary_path)
    if target is None:
        return False
    return _atomic_write_json(
        target,
        _stamp_schema_version(payload, SMART_COST_SUMMARY_SCHEMA_VERSION),
    )


def _safe_path(project_dir: Path, path_fn) -> Path | None:
    """Codex 第九轮 P1-4: defensive wrapper for the path helpers used by
    the atomic writers. ``smart_*_path()`` calls ``_audit_dir()`` which
    calls ``mkdir`` — that can raise. Catch here so the writer's caller
    sees False rather than an unhandled exception.

    Returns None on failure (caller short-circuits to False return).
    """
    try:
        return path_fn(project_dir)
    except Exception:
        logger.exception(
            "sidecar emitter: failed to compute / create audit dir for %s",
            project_dir,
        )
        return None


__all__ = [
    "SMART_COST_SUMMARY_SCHEMA_VERSION",
    "SMART_DECISIONS_SCHEMA_VERSION",
    "SMART_QUALITY_REPORT_SCHEMA_VERSION",
    "emit_smart_decision",
    "smart_cost_summary_path",
    "smart_decisions_path",
    "smart_quality_report_path",
    "write_smart_cost_summary",
    "write_smart_quality_report",
]

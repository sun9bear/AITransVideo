"""Admin content-compliance contract (2026-05-20).

== User spec ==

    "Admin的合规检查如果有问题，只做任务通知，不影响流程"

That is:
- **Non-admin** + compliance block → raise ContentPolicyViolationError
  → pipeline exits with failed status + user-visible error message
- **Admin** + compliance block → dispatch a task notification + return
  payload + **pipeline continues** to completion

After 2026-05-20 commit 7aa0abc (smart full-auto spec), admin path
is also no longer re-prompted at translation_review (the legacy
compliance_block kwarg on evaluate_translation_review is now
ignored). So admin's compliance-flagged smart job runs to TTS +
delivery without intervention, with one popup-style warning
notification reaching the admin's notifications drawer.

== This test ==

Pins the contract end-to-end via three layers:

1. ``_run_content_compliance_review`` admin/non-admin branching
   (source-anchor — actual runtime test would need a real
   ContentComplianceReviewer + sample transcript).
2. Notification dispatch helper signature + event constant.
3. Gateway notification dispatch map entry shape (severity,
   popup, action_url).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
_GATEWAY = _REPO / "gateway"
for p in (_SRC, _GATEWAY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

_PROCESS_PY = _SRC / "pipeline" / "process.py"
_NOTIFICATION_MAP = _GATEWAY / "notification_dispatch_map.py"


class TestProcessPyAdminComplianceBranch:
    """The admin override branch in ``_run_content_compliance_review``
    must dispatch notification + return payload (NOT raise)."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_admin_override_branch_does_not_raise(self):
        """Anchor on the ``if admin_override_applied:`` branch and
        confirm it ends with ``return payload`` (not ``raise``)."""
        source = self._source()
        # The branch starts with this distinctive line
        anchor = "admin_override_applied = bool(admin_override and final_result.blocked)"
        idx = source.find(anchor)
        assert idx >= 0, (
            "admin_override_applied derivation missing from "
            "_run_content_compliance_review. The admin notify-only "
            "contract depends on this branch."
        )

        # Look forward ~2500 chars (enough to span the if-block).
        window = source[idx : idx + 2500]

        # The branch dispatches notification.
        assert "_dispatch_content_compliance_admin_override_notification(" in window, (
            "Admin override branch must dispatch a task notification — "
            "spec 'Admin的合规检查如果有问题，只做任务通知' requires this."
        )

        # The branch returns payload (does NOT raise) — this is what
        # makes pipeline continue.
        assert "return payload" in window, (
            "Admin override branch must return payload (not raise) so "
            "pipeline continues. If you re-add a raise here, smart "
            "admin jobs will fail at S2 instead of running to "
            "completion."
        )

        # CRITICAL: the raise must be in the NON-admin fallthrough
        # branch, AFTER the admin return. Verify the raise lives
        # after the admin-block-and-return.
        raise_idx = source.find(
            "raise ContentPolicyViolationError(final_result)", idx,
        )
        admin_return_idx = source.find("return payload", idx)
        assert raise_idx >= 0 and admin_return_idx >= 0
        assert admin_return_idx < raise_idx, (
            "Admin ``return payload`` must precede the non-admin "
            "``raise ContentPolicyViolationError``. If the order is "
            "reversed, admin paths would raise too — breaking "
            "'不影响流程' contract."
        )

    def test_admin_override_branch_logs_chinese_message(self):
        """Admin sees a clear server log when compliance is bypassed —
        not silently swallowed. The log is part of audit trail."""
        source = self._source()
        anchor = "if admin_override_applied:"
        idx = source.find(anchor)
        assert idx >= 0
        window = source[idx : idx + 2500]
        # The Chinese log line is a stable anchor (the structured
        # notification handles the user-facing UX; this log is the
        # ops/admin server-side trail).
        assert "已记录警告并继续流程" in window, (
            "Admin override branch should log a clear continuing-"
            "pipeline marker so server logs explain why a flagged "
            "job still ran. Marker substring not found."
        )


class TestProcessPyNonAdminComplianceBranch:
    """Non-admin path MUST still raise (preserves legal gate)."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_non_admin_blocked_raises(self):
        """The fallthrough ``if final_result.blocked:`` branch must
        raise ContentPolicyViolationError so pipeline marks job failed."""
        source = self._source()
        # Anchor on the non-admin branch
        anchor = "if final_result.blocked:"
        # Skip occurrences inside other functions — find the one in
        # _run_content_compliance_review specifically (after the admin
        # override branch).
        function_idx = source.find("def _run_content_compliance_review")
        assert function_idx >= 0
        # Find anchor AFTER the function start
        idx = source.find(anchor, function_idx)
        assert idx >= 0, (
            "Non-admin compliance branch missing from "
            "_run_content_compliance_review. The legal gate depends "
            "on this raise."
        )
        window = source[idx : idx + 500]
        assert "raise ContentPolicyViolationError(final_result)" in window, (
            "Non-admin compliance branch must raise "
            "ContentPolicyViolationError so the pipeline marks the "
            "job failed with stage=S2. Removing the raise would let "
            "non-admin jobs bypass the legal gate silently."
        )


class TestNotificationDispatchMapEntry:
    """Gateway notification map must have the admin override event
    with popup + warning severity so admin sees it prominently."""

    def _source(self) -> str:
        return _NOTIFICATION_MAP.read_text(encoding="utf-8")

    def test_admin_override_event_constant_defined(self):
        source = self._source()
        assert "EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE" in source
        assert '"job.content_compliance_admin_override"' in source

    def test_admin_override_map_entry_has_required_fields(self):
        """The map entry drives both the notification card UI + popup
        behavior. Pin the fields to prevent silent UX regressions."""
        from notification_dispatch_map import (
            EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE,
            DISPATCH_MAP,
        )
        entry = DISPATCH_MAP.get(
            EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE
        )
        assert entry is not None, (
            "EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE missing from "
            "EVENT_DISPATCH_MAP. Admin would get no UI notification."
        )
        assert entry.get("severity") == "warning", (
            "Admin compliance override should be 'warning' severity — "
            "not 'success' (it's a legal bypass) and not 'error' "
            "(pipeline still completes successfully)."
        )
        assert entry.get("popup") is True, (
            "Admin compliance override notification should pop up so "
            "admin sees it immediately, not just in the drawer. "
            "Without popup the admin might miss that they bypassed "
            "compliance on a delivered job."
        )
        # Action URL routes to workspace where admin can review output.
        assert "/workspace/{job_id}" in str(entry.get("action_url", "")), (
            "Admin override notification action_url should route to "
            "the job workspace so admin can review the output."
        )
        # Body mentions admin bypass + has summary placeholder.
        body = str(entry.get("body", ""))
        assert "管理员" in body or "admin" in body.lower(), (
            "Notification body should make clear this is an admin "
            "bypass event (so the admin understands why a flagged "
            "job is in their queue)."
        )
        assert "{display_name}" in body and "{summary}" in body, (
            "Notification body should template both display_name + "
            "summary so the admin sees what job + what compliance "
            "finding triggered it."
        )


class TestDispatchHelperContract:
    """The dispatch helper must POST to the gateway notifications
    endpoint with the correct event payload shape."""

    def test_dispatch_helper_signature(self):
        from pipeline.process import (
            _dispatch_content_compliance_admin_override_notification,
        )
        import inspect

        sig = inspect.signature(
            _dispatch_content_compliance_admin_override_notification
        )
        # All four kwargs required for cross-referencing in notification
        # body + admin UI deeplink.
        for kw in ("job_id", "user_id", "display_name", "summary"):
            assert kw in sig.parameters, (
                f"Dispatch helper missing required kwarg {kw!r}. "
                f"Without it the notification body / deeplink would "
                f"miss the corresponding placeholder."
            )

    def test_dispatch_helper_uses_correct_event_constant(self):
        """The helper POSTs an event_type that must match the gateway
        map key — otherwise gateway returns 400 unknown_event_type."""
        source = _PROCESS_PY.read_text(encoding="utf-8")
        # Find the helper function
        anchor = "def _dispatch_content_compliance_admin_override_notification("
        idx = source.find(anchor)
        assert idx >= 0
        window = source[idx : idx + 3000]
        # Must reference the canonical event constant (not a literal
        # string — keep it DRY with the gateway map).
        assert "CONTENT_COMPLIANCE_ADMIN_OVERRIDE_EVENT" in window, (
            "Helper should reference the CONTENT_COMPLIANCE_ADMIN_OVERRIDE_EVENT "
            "constant, not hardcode the string. If you inline the "
            "literal, gateway map drift can silently break dispatch."
        )

    def test_dispatch_helper_fails_silently_on_missing_ids(self):
        """Helper returns False (not raises) when job_id / user_id
        are missing — backbone of 'best-effort' notification."""
        from pipeline.process import (
            _dispatch_content_compliance_admin_override_notification,
        )

        # Missing both IDs → returns False, no exception
        assert _dispatch_content_compliance_admin_override_notification(
            job_id=None,
            user_id=None,
            display_name=None,
            summary="some compliance summary",
        ) is False

        # Empty strings also count as missing
        assert _dispatch_content_compliance_admin_override_notification(
            job_id="",
            user_id="",
            display_name="",
            summary="",
        ) is False


class TestAutoTranslationReviewIgnoresComplianceBlock:
    """The 2026-05-20 spec change removed the redundant second
    compliance gate at translation_review. Admin (and all other smart
    paths) no longer get re-prompted there."""

    def test_compliance_block_kwarg_ignored_after_admin_bypass(self):
        """Simulate the admin path: compliance_block=True passed to
        evaluate_translation_review — must auto-approve regardless."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        passing = {
            "translation_result": {
                "glossary_total_terms": 10,
                "glossary_preserved_terms": 10,
                "length_overflow_rate": 0.0,
                "rewrite_attempted": False,
                "subtitle_source_text_sha256": "x",
                "final_spoken_text_sha256": "x",
                "segments": [{"segment_id": "s1", "speaker_id": "speaker_a"}],
            },
            "speaker_stats": {
                "uncertain_speaker_duration_share": 0.0,
                "asr_speaker_count": 1,
            },
            "clone_sample_stats": {"eligible_speakers": 1},
        }
        # Admin scenario: compliance was bypassed at S2, payload
        # status is still "blocked" → compliance_block=True reaches
        # translation_review. Must NOT block.
        decision = evaluate_translation_review(
            **passing, compliance_block=True,
        )
        assert decision.auto_approved is True, (
            "Admin scenario broken: compliance_block=True caused "
            "translation_review to block. Admin would get re-prompted "
            "at S3 stage, contradicting '不影响流程' contract."
        )

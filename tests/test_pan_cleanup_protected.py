"""Cleanup protected status extension for pan backup.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 1.4
"""


def test_archiving_and_restoring_protected_from_cleanup():
    from services.web_ui.cleanup import _CLEANUP_PROTECTED_STATUSES

    # Pan transient states must be protected — cleanup must NOT touch them
    assert 'archiving' in _CLEANUP_PROTECTED_STATUSES, (
        "archiving is transient (backup uploading); cleanup must not race-delete project_dir"
    )
    assert 'restoring' in _CLEANUP_PROTECTED_STATUSES, (
        "restoring is transient (restore extracting); cleanup must not race-delete partial extraction"
    )

    # archived is terminal — by the time job hits archived, backup_executor
    # already removed project_dir. Cleanup seeing archived = no-op (nothing on disk).
    # Adding to protected set would be incorrect — it would block future
    # cleanup paths if they ever needed to act on archived rows.
    assert 'archived' not in _CLEANUP_PROTECTED_STATUSES, (
        "archived is terminal; should not block cleanup"
    )


def test_existing_protected_statuses_unchanged():
    """Regression guard: T1.4 must not remove existing protections."""
    from services.web_ui.cleanup import _CLEANUP_PROTECTED_STATUSES

    # These are the well-known active states that have been protected for a while.
    # Adjust this list if the project's protected set differs — the goal is
    # to ensure we DIDN'T REMOVE protections, not to enforce an exact set.
    expected_protected = {'queued', 'running', 'editing', 'waiting_for_review'}
    missing = expected_protected - _CLEANUP_PROTECTED_STATUSES
    assert not missing, f"Removed/missing existing protections: {missing}"

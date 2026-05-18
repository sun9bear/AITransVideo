"""Pan backup status vocab extension test.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 1.3
"""


def test_three_new_pan_statuses_in_supported_set():
    from services.jobs.models import (
        SUPPORTED_JOB_STATUSES,
        ACTIVE_JOB_STATUSES,
        WORKER_ACTIVE_STATUSES,
        JOB_STATUS_ARCHIVING,
        JOB_STATUS_ARCHIVED,
        JOB_STATUS_RESTORING,
    )

    # All 3 constants exist and have expected values
    assert JOB_STATUS_ARCHIVING == 'archiving'
    assert JOB_STATUS_ARCHIVED == 'archived'
    assert JOB_STATUS_RESTORING == 'restoring'

    # All 3 are in SUPPORTED_JOB_STATUSES
    assert JOB_STATUS_ARCHIVING in SUPPORTED_JOB_STATUSES
    assert JOB_STATUS_ARCHIVED in SUPPORTED_JOB_STATUSES
    assert JOB_STATUS_RESTORING in SUPPORTED_JOB_STATUSES

    # archiving + restoring are transient — must be ACTIVE so cleanup
    # path doesn't reap them
    assert JOB_STATUS_ARCHIVING in ACTIVE_JOB_STATUSES, (
        "archiving is transient/in-progress; cleanup must not reap it"
    )
    assert JOB_STATUS_RESTORING in ACTIVE_JOB_STATUSES, (
        "restoring is transient/in-progress; cleanup must not reap it"
    )

    # archived is the terminal state for archived jobs.
    # It is intentionally NOT in ACTIVE_JOB_STATUSES — cleanup is allowed
    # to see archived jobs (though backup_executor proactively removed the
    # project_dir; cleanup just won't find anything to delete).
    assert JOB_STATUS_ARCHIVED not in ACTIVE_JOB_STATUSES, (
        "archived is terminal; should not block cleanup paths"
    )

    # None of the 3 belong in WORKER_ACTIVE_STATUSES — they don't have a
    # pipeline worker process. They run as gateway background tasks.
    assert JOB_STATUS_ARCHIVING not in WORKER_ACTIVE_STATUSES
    assert JOB_STATUS_ARCHIVED not in WORKER_ACTIVE_STATUSES
    assert JOB_STATUS_RESTORING not in WORKER_ACTIVE_STATUSES


def test_existing_statuses_unchanged():
    """Regression guard: T1.3 must NOT remove or alter any existing status constants."""
    from services.jobs.models import (
        SUPPORTED_JOB_STATUSES,
        ACTIVE_JOB_STATUSES,
        WORKER_ACTIVE_STATUSES,
        JOB_STATUS_QUEUED,
        JOB_STATUS_RUNNING,
        JOB_STATUS_SUCCEEDED,
        JOB_STATUS_FAILED,
        JOB_STATUS_CANCELLED,
        JOB_STATUS_EDITING,
        JOB_STATUS_WAITING_FOR_REVIEW,
        JOB_STATUS_PURGED,
    )

    # All existing statuses must still be in SUPPORTED
    for s in (
        JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_SUCCEEDED,
        JOB_STATUS_FAILED, JOB_STATUS_CANCELLED, JOB_STATUS_EDITING,
        JOB_STATUS_WAITING_FOR_REVIEW, JOB_STATUS_PURGED,
    ):
        assert s in SUPPORTED_JOB_STATUSES, f"{s} missing from SUPPORTED"

    # ACTIVE_JOB_STATUSES should still contain in-progress states
    for s in (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_EDITING):
        assert s in ACTIVE_JOB_STATUSES, f"{s} missing from ACTIVE"

    # WORKER_ACTIVE_STATUSES should still be just queued + running
    assert WORKER_ACTIVE_STATUSES == {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}, (
        "WORKER_ACTIVE_STATUSES should not be touched by pan backup task"
    )

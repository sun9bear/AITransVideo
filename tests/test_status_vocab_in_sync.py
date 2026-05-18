"""Contract guard: status vocab must agree between Python backend and TS frontend.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 1.6

Failure modes this prevents:
- Backend adds new status, frontend not updated → user sees default fallback
  label ("已取消" if normalizeStatus falls back to 'cancelled' or similar)
- Frontend adds label key for status backend doesn't recognize → broken
  state machine assumption

Parses `frontend-next/src/types/jobs.ts` with a regex over JOB_STATUS_LABELS
keys. This is fragile if the file format changes — but the format is
"object literal with string keys" which is stable.
"""
import re
from pathlib import Path


def _read_ts_status_labels() -> set[str]:
    """Parse JOB_STATUS_LABELS keys from frontend-next/src/types/jobs.ts.

    Returns the set of TypeScript keys (= the JobStatus union members).
    """
    ts_file = Path(__file__).resolve().parents[1] / 'frontend-next' / 'src' / 'types' / 'jobs.ts'
    assert ts_file.exists(), f"jobs.ts not found at {ts_file}"
    content = ts_file.read_text(encoding='utf-8')

    # Locate the JOB_STATUS_LABELS object body
    m = re.search(
        r'JOB_STATUS_LABELS\s*=\s*\{(.*?)\}\s*as\s+const',
        content,
        re.DOTALL,
    )
    assert m, "JOB_STATUS_LABELS = { ... } as const not found in jobs.ts"
    body = m.group(1)

    # Each line is like:  cancelled: '已取消',
    # Capture the bareword key before the colon.
    keys = set(re.findall(r"^\s*(\w+):\s*['\"]", body, re.MULTILINE))
    assert keys, "no keys parsed from JOB_STATUS_LABELS body — regex broken?"
    return keys


def test_python_and_typescript_status_vocab_in_sync():
    from services.jobs.models import SUPPORTED_JOB_STATUSES
    ts_keys = _read_ts_status_labels()

    py_only = SUPPORTED_JOB_STATUSES - ts_keys
    ts_only = ts_keys - SUPPORTED_JOB_STATUSES

    assert not py_only, (
        f"SUPPORTED_JOB_STATUSES has statuses not in JOB_STATUS_LABELS (frontend will show fallback label): "
        f"{sorted(py_only)}. Fix: add label to frontend-next/src/types/jobs.ts."
    )
    assert not ts_only, (
        f"JOB_STATUS_LABELS has keys not in SUPPORTED_JOB_STATUSES (frontend declares fictional statuses): "
        f"{sorted(ts_only)}. Fix: add constant + set member in src/services/jobs/models.py, or remove from jobs.ts."
    )


def test_pan_triplet_present_in_both_sides():
    """Specific signal-of-life check: the 3 statuses we just added must exist on both sides."""
    from services.jobs.models import SUPPORTED_JOB_STATUSES
    ts_keys = _read_ts_status_labels()

    for status in ('archiving', 'archived', 'restoring'):
        assert status in SUPPORTED_JOB_STATUSES, (
            f"'{status}' missing from Python SUPPORTED_JOB_STATUSES "
            f"(should have been added in Task 1.3)"
        )
        assert status in ts_keys, (
            f"'{status}' missing from TS JOB_STATUS_LABELS "
            f"(should have been added in Task 1.5)"
        )


def test_api_status_union_includes_pan_triplet():
    """Parallel check: frontend ApiJobStatus union (in api.ts) must also list
    the 3 new statuses. Drift here was caught by T1.5 subagent.
    """
    api_file = Path(__file__).resolve().parents[1] / 'frontend-next' / 'src' / 'types' / 'api.ts'
    if not api_file.exists():
        # api.ts not present in this repo layout — skip rather than fail
        import pytest
        pytest.skip("frontend-next/src/types/api.ts not found")
    content = api_file.read_text(encoding='utf-8')

    for status in ('archiving', 'archived', 'restoring'):
        # Match patterns like `| 'archiving'` or `"archiving"`
        if re.search(rf"['\"]{status}['\"]", content):
            continue
        raise AssertionError(
            f"'{status}' not found in frontend-next/src/types/api.ts. "
            f"ApiJobStatus wire union likely missing this value — clients "
            f"will reject server responses with this status."
        )

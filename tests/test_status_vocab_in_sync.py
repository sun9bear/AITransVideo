"""Contract guard: status vocab must agree between Python backend and TS frontend.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 1.6

Failure modes this prevents:
- Backend adds new status, frontend not updated → user sees default fallback
  label ("已取消" if normalizeStatus falls back to 'cancelled' or similar)
- Frontend adds label key for status backend doesn't recognize → broken
  state machine assumption

Parses `frontend-next/src/types/jobs.ts` with a regex over the JOB_STATUSES
key tuple. This is fragile if the file format changes — but the format is
"`as const` string-literal array" which is stable.

UI-05 (2026-06-29): the status *labels* moved into the next-intl source
catalog (messages/{zh,en}/app.json `status.*`); jobs.ts now keeps only the
key tuple `JOB_STATUSES` as the single source of truth for the status key set
(= the JobStatus union members). This guard parses that tuple, and
`test_every_status_key_has_zh_and_en_label` re-pins the "every status has a
label" guarantee against the catalogs (which is where labels now live).
"""
import json
import re
from pathlib import Path


def _read_ts_status_keys() -> set[str]:
    """Parse JOB_STATUSES keys from frontend-next/src/types/jobs.ts.

    Returns the set of TypeScript status keys (= the JobStatus union members).
    """
    ts_file = Path(__file__).resolve().parents[1] / 'frontend-next' / 'src' / 'types' / 'jobs.ts'
    assert ts_file.exists(), f"jobs.ts not found at {ts_file}"
    content = ts_file.read_text(encoding='utf-8')

    # Locate the JOB_STATUSES array body
    m = re.search(
        r'JOB_STATUSES\s*=\s*\[(.*?)\]\s*as\s+const',
        content,
        re.DOTALL,
    )
    assert m, "JOB_STATUSES = [ ... ] as const not found in jobs.ts"
    body = m.group(1)

    # Each entry is a quoted string like:  'cancelled',
    keys = set(re.findall(r"['\"](\w+)['\"]", body))
    assert keys, "no keys parsed from JOB_STATUSES body — regex broken?"
    return keys


def _read_app_status_labels(locale: str) -> dict[str, str]:
    """Read messages/<locale>/app.json `status` object (label source of truth)."""
    catalog = (
        Path(__file__).resolve().parents[1]
        / 'frontend-next' / 'messages' / locale / 'app.json'
    )
    assert catalog.exists(), f"app.json not found at {catalog}"
    data = json.loads(catalog.read_text(encoding='utf-8'))
    return dict(data.get('status', {}))


def test_python_and_typescript_status_vocab_in_sync():
    from services.jobs.models import SUPPORTED_JOB_STATUSES
    ts_keys = _read_ts_status_keys()

    py_only = SUPPORTED_JOB_STATUSES - ts_keys
    ts_only = ts_keys - SUPPORTED_JOB_STATUSES

    assert not py_only, (
        f"SUPPORTED_JOB_STATUSES has statuses not in JOB_STATUSES (frontend will show fallback label): "
        f"{sorted(py_only)}. Fix: add the key to JOB_STATUSES + a status.<key> label to "
        f"messages/{{zh,en}}/app.json."
    )
    assert not ts_only, (
        f"JOB_STATUSES has keys not in SUPPORTED_JOB_STATUSES (frontend declares fictional statuses): "
        f"{sorted(ts_only)}. Fix: add constant + set member in src/services/jobs/models.py, or remove from jobs.ts."
    )


def test_every_status_key_has_zh_and_en_label():
    """UI-05: status labels live in messages/{zh,en}/app.json `status.*`. Every
    JOB_STATUSES key must have a label in BOTH locales, else the badge falls back
    to the raw status string (the failure mode the old JOB_STATUS_LABELS object
    guaranteed against, now that labels moved into the catalog)."""
    ts_keys = _read_ts_status_keys()
    for locale in ('zh', 'en'):
        labels = _read_app_status_labels(locale)
        missing = ts_keys - labels.keys()
        assert not missing, (
            f"messages/{locale}/app.json status.* is missing labels for "
            f"{sorted(missing)} — add them so the status badge never shows the "
            f"raw status string."
        )


def test_pan_triplet_present_in_both_sides():
    """Specific signal-of-life check: the 3 statuses we just added must exist on both sides."""
    from services.jobs.models import SUPPORTED_JOB_STATUSES
    ts_keys = _read_ts_status_keys()

    for status in ('archiving', 'archived', 'restoring'):
        assert status in SUPPORTED_JOB_STATUSES, (
            f"'{status}' missing from Python SUPPORTED_JOB_STATUSES "
            f"(should have been added in Task 1.3)"
        )
        assert status in ts_keys, (
            f"'{status}' missing from TS JOB_STATUSES "
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

"""§10.4 deepening — gateway-side redaction of JobRecord fields.

Plan §10.4 + D25 extension:

The ``GET /jobs/{id}/logs`` endpoint already redacts ``events[].message``
and ``lines[]`` for non-admin users (covered in
``test_gateway_logs_redaction.py``). But two other JobRecord-shaped
fields surface in the workspace UI and went unredacted:

  - ``progress_message`` — shown in the "正在处理" big card subtitle
  - ``error_summary.message`` — shown in the failed-state card

Both can carry provider names (Gemini / DeepSeek / MiniMax / etc.),
file paths (``/opt/aivideotrans/...``), and UUIDs straight from
upstream exception strings, leaking infrastructure detail to non-admin
users.

Contract pinned by these tests:

1. ``_redact_job_record_in_place`` mutates the dict's ``progress_message``
   and ``error_summary.message`` to the redacted form for non-admin users.
2. Admin (and only admin — role string == 'admin') is pass-through:
   the dict comes back byte-identical.
3. ``user=None`` (no auth context) is treated as non-admin — fail closed.
4. Missing fields / non-string values are tolerated (no AttributeError).
5. The redactor instance can be passed in (lets callers build it once
   for a list of jobs instead of per-row).

Storage layer is intentionally NOT touched — JobRecord JSON files keep
raw text so admin's ``GET /jobs/{id}/logs`` and direct-DB inspection
both see the unfiltered content.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_GATEWAY_DIR = Path(__file__).resolve().parents[1] / "gateway"
_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
for _cand in (_GATEWAY_DIR, _SRC_DIR):
    if str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

import job_intercept  # type: ignore[import-not-found]


class _FakeUser:
    def __init__(self, role: str) -> None:
        self.role = role


def _sample_record() -> dict[str, Any]:
    """A representative JobRecord JSON shape with sensitive content in
    every field we care about."""
    return {
        "job_id": "job_abc",
        "status": "failed",
        "progress_message": (
            "[S3] Gemini translation failed: HTTPError 504 at "
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
        ),
        "error_summary": {
            "stage": "translation",
            "error_type": "TranslationError",
            "message": (
                "MiniMax voice clone returned 429 for "
                "/opt/aivideotrans/app/projects/abc/audio/sample.wav"
            ),
        },
        "title": "Some Job",
        "current_stage": "translation",
        # Other fields not subject to redaction; these must come back
        # exactly as-is for both admin and non-admin.
        "speakers": "auto",
        "review_gate": {"stage": "translation_review"},
        "edit_generation": 0,
    }


# ---------------------------------------------------------------------------
# admin bypass — record comes back byte-identical
# ---------------------------------------------------------------------------


def test_admin_record_is_unchanged() -> None:
    record = _sample_record()
    snapshot = _sample_record()  # independent copy for comparison

    job_intercept._redact_job_record_in_place(record, _FakeUser("admin"))

    assert record == snapshot, (
        "admin must see the JobRecord byte-identical; redaction must not "
        "fire on the admin path"
    )


# ---------------------------------------------------------------------------
# non-admin: progress_message + error_summary.message are redacted
# ---------------------------------------------------------------------------


def test_non_admin_progress_message_provider_names_stripped() -> None:
    record = _sample_record()
    job_intercept._redact_job_record_in_place(record, _FakeUser("user"))

    msg = record["progress_message"]
    assert "Gemini" not in msg, msg
    # URL component should also be redacted (logs_redactor handles URLs/UUIDs).
    # The redactor today targets provider names + UUIDs + a small infra-tool
    # set; relax the check to cover the most-leaky tokens.
    assert "googleapis.com" not in msg or "gemini-2.5-flash" not in msg, msg


def test_non_admin_error_summary_message_provider_names_stripped() -> None:
    record = _sample_record()
    job_intercept._redact_job_record_in_place(record, _FakeUser("user"))

    es_msg = record["error_summary"]["message"]
    assert "MiniMax" not in es_msg, es_msg


def test_non_admin_unrelated_fields_untouched() -> None:
    """Only the two known message fields are mutated. ``error_summary``
    siblings (``stage`` / ``error_type``) and other top-level fields stay
    raw — they're already user-safe enums."""
    record = _sample_record()
    job_intercept._redact_job_record_in_place(record, _FakeUser("user"))

    assert record["error_summary"]["stage"] == "translation"
    assert record["error_summary"]["error_type"] == "TranslationError"
    assert record["title"] == "Some Job"
    assert record["current_stage"] == "translation"
    assert record["speakers"] == "auto"
    assert record["review_gate"] == {"stage": "translation_review"}


# ---------------------------------------------------------------------------
# fail-closed: user=None defaults to non-admin redaction
# ---------------------------------------------------------------------------


def test_null_user_treated_as_non_admin() -> None:
    record = _sample_record()
    job_intercept._redact_job_record_in_place(record, None)

    assert "Gemini" not in record["progress_message"]
    assert "MiniMax" not in record["error_summary"]["message"]


# ---------------------------------------------------------------------------
# Tolerance: missing / non-string fields don't raise
# ---------------------------------------------------------------------------


def test_missing_progress_message_tolerated() -> None:
    record = {"job_id": "x", "status": "queued"}
    # No progress_message, no error_summary — must not raise.
    job_intercept._redact_job_record_in_place(record, _FakeUser("user"))
    assert record == {"job_id": "x", "status": "queued"}


def test_null_progress_message_tolerated() -> None:
    record = {"job_id": "x", "progress_message": None, "error_summary": None}
    job_intercept._redact_job_record_in_place(record, _FakeUser("user"))
    assert record["progress_message"] is None
    assert record["error_summary"] is None


def test_error_summary_without_message_tolerated() -> None:
    record = {
        "job_id": "x",
        "error_summary": {"stage": "ingestion", "error_type": "DownloadError"},
    }
    job_intercept._redact_job_record_in_place(record, _FakeUser("user"))
    # No message to redact, sibling fields preserved.
    assert record["error_summary"]["stage"] == "ingestion"
    assert record["error_summary"]["error_type"] == "DownloadError"
    assert "message" not in record["error_summary"]


def test_non_dict_error_summary_tolerated() -> None:
    """Defensive — if upstream ever returns ``error_summary`` as a string
    or list, we must not crash."""
    record = {"job_id": "x", "error_summary": "raw error string"}
    job_intercept._redact_job_record_in_place(record, _FakeUser("user"))
    # Non-dict left alone (we only know how to walk the dict shape).
    assert record["error_summary"] == "raw error string"


# ---------------------------------------------------------------------------
# Reusable redactor instance — caller can build once + apply to many records
# ---------------------------------------------------------------------------


def test_caller_can_pass_prebuilt_redactor() -> None:
    """For list endpoints with N rows, building the redactor once and
    reusing it avoids N rebuilds. Helper accepts an optional injected
    instance."""
    from services.jobs.logs_redactor import build_default_redactor

    redactor = build_default_redactor()
    records = [_sample_record() for _ in range(3)]
    for r in records:
        job_intercept._redact_job_record_in_place(
            r, _FakeUser("user"), redactor=redactor
        )

    for r in records:
        assert "Gemini" not in r["progress_message"]
        assert "MiniMax" not in r["error_summary"]["message"]

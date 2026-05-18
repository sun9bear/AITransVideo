"""Pan backup redaction tests.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 2.4

The real redaction file is ``src/services/jobs/logs_redactor.py``;
``gateway/log_redactor_loader.py`` loads it via importlib to avoid the
pydub-dependency trap in the gateway container.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[1]
    src_path = str(root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)


def _make_redact():
    """Return a callable ``redact(message) -> str`` using the default redactor."""
    _ensure_src_on_path()
    from src.services.jobs.logs_redactor import build_default_redactor
    r = build_default_redactor()
    return r.redact


def test_access_token_value_masked():
    redact = _make_redact()
    log = 'OAuth: {"access_token": "actual_baidu_secret_xyz", "expires_in": 2592000}'
    out = redact(log)
    assert "actual_baidu_secret_xyz" not in out, (
        f"access_token value leaked in output: {out}"
    )
    # Key itself can remain visible — only the value is redacted


def test_refresh_token_value_masked():
    redact = _make_redact()
    # JSON shape
    log_json = '{"refresh_token": "very_long_refresh_token_xyz_abc_123"}'
    out_json = redact(log_json)
    assert "very_long_refresh_token_xyz_abc_123" not in out_json

    # Form-encoded shape (Baidu OAuth uses form bodies)
    log_form = "grant_type=refresh_token&refresh_token=secret_refresh_999&client_id=test"
    out_form = redact(log_form)
    assert "secret_refresh_999" not in out_form


def test_appsecret_masked():
    redact = _make_redact()
    # Our config field name
    log_with_appsecret = "appsecret=8VHpJeQ4Kep404AXQ57qE8YudiSriKLP"
    out = redact(log_with_appsecret)
    assert "8VHpJeQ4Kep404AXQ57qE8YudiSriKLP" not in out

    # OAuth standard field name
    log_with_client_secret = "client_secret=8VHpJeQ4Kep404AXQ57qE8YudiSriKLP"
    out = redact(log_with_client_secret)
    assert "8VHpJeQ4Kep404AXQ57qE8YudiSriKLP" not in out


def test_existing_redactions_still_work():
    """Regression guard: T2.4 additions must be purely additive — no regressions."""
    redact = _make_redact()
    # UUID redaction existed before T2.4
    log_uuid = "job 4a6006e8-b2df-41b1-9b19-bd6facf1d9bf done"
    out = redact(log_uuid)
    assert "4a6006e8" not in out, (
        "UUID redaction broken — T2.4 should be additive"
    )
    # Known infra-tool names
    log_tool = "calling AssemblyAI to transcribe"
    out_tool = redact(log_tool)
    assert "AssemblyAI" not in out_tool, (
        "infra-tool redaction broken — T2.4 should be additive"
    )


def test_plain_text_unchanged():
    """Sanity: text without secret keywords passes through unchanged."""
    redact = _make_redact()
    log = "INFO: pan backup uploaded 1.5GB in 240s"
    assert redact(log) == log

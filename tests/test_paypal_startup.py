"""startup_checks.validate_paypal_config — fail-graceful, never raises."""
from __future__ import annotations

import logging
import sys

_gateway_dir = str(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "gateway"
)
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from startup_checks import validate_paypal_config  # noqa: E402


def _scrub(monkeypatch):
    for name in (
        "AVT_PAYPAL_ENABLED",
        "AVT_PAYPAL_CLIENT_ID",
        "AVT_PAYPAL_SECRET",
        "AVT_PAYPAL_WEBHOOK_ID",
        "AVT_PAYPAL_ENV",
    ):
        monkeypatch.delenv(name, raising=False)


def test_disabled_is_noop(monkeypatch, caplog):
    _scrub(monkeypatch)
    with caplog.at_level(logging.CRITICAL):
        validate_paypal_config()  # must not raise
    assert not caplog.records


def test_enabled_but_missing_logs_critical(monkeypatch, caplog):
    _scrub(monkeypatch)
    monkeypatch.setenv("AVT_PAYPAL_ENABLED", "true")  # no creds
    with caplog.at_level(logging.CRITICAL):
        validate_paypal_config()  # never raises
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert any("missing" in r.getMessage() for r in caplog.records)


def test_enabled_and_complete_is_clean(monkeypatch, caplog):
    _scrub(monkeypatch)
    monkeypatch.setenv("AVT_PAYPAL_ENABLED", "true")
    monkeypatch.setenv("AVT_PAYPAL_CLIENT_ID", "cid")
    monkeypatch.setenv("AVT_PAYPAL_SECRET", "sec")
    monkeypatch.setenv("AVT_PAYPAL_WEBHOOK_ID", "WH-1")
    with caplog.at_level(logging.CRITICAL):
        validate_paypal_config()
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)

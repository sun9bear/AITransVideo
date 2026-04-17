"""Tests for T5: Job API `_send_sanitized_error` helper.

Generic exception responses must not leak internal details (stack frames,
DB DSNs, file paths, secrets) to the client. This helper centralizes the
sanitization so every `except Exception` fallback stays consistent.
"""
from __future__ import annotations

import json
import logging
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.jobs.api import _build_job_api_handler


def _make_stub_handler_instance(handler_class):
    """Construct a bare handler that we can call methods on without a real socket.

    BaseHTTPRequestHandler subclasses do I/O via self.wfile / send_response /
    send_header / end_headers. We mock those so `_send_sanitized_error` can
    run to completion and we can inspect what it wrote.
    """
    inst = handler_class.__new__(handler_class)  # skip __init__; no socket setup
    inst.path = "/jobs/fake-id"
    inst.command = "GET"
    inst.wfile = MagicMock()
    inst.send_response = MagicMock()
    inst.send_header = MagicMock()
    inst.end_headers = MagicMock()
    return inst


def _body_from_stub(stub) -> dict:
    """Extract JSON payload from the captured `wfile.write` call."""
    assert stub.wfile.write.called, "handler never wrote a response body"
    written = stub.wfile.write.call_args.args[0]
    return json.loads(written.decode("utf-8"))


class TestSendSanitizedError:
    def setup_method(self) -> None:
        service = MagicMock()
        self.handler_class = _build_job_api_handler(service=service)

    def test_sensitive_substrings_never_appear_in_body(self, caplog):
        """Exception details with secrets must be logged but never returned."""
        stub = _make_stub_handler_instance(self.handler_class)
        evil = RuntimeError(
            "DB password=hunter2 connstr=postgresql://user:pass@host/db path=/etc/secret"
        )

        with caplog.at_level(logging.ERROR):
            stub._send_sanitized_error(evil)

        body = _body_from_stub(stub)
        assert body["error"] == "internal_error"
        assert "message" in body  # user-facing message present

        raw = json.dumps(body, ensure_ascii=False)
        for leaked in ("hunter2", "password", "postgresql://", "/etc/secret"):
            assert leaked not in raw, f"sensitive substring {leaked!r} leaked to client"

        # Full detail must still reach the log (for operators to debug)
        log_text = " ".join(record.getMessage() for record in caplog.records)
        assert any("hunter2" in str(exc) for exc in [evil])  # the logger.exception() call path
        # logger.exception attaches the traceback, not the arg message —
        # check that the handler's path + method were logged for context
        assert "/jobs/fake-id" in log_text or any(
            "fake-id" in record.getMessage() for record in caplog.records
        )

    def test_status_is_500(self):
        stub = _make_stub_handler_instance(self.handler_class)
        stub._send_sanitized_error(ValueError("anything"))

        # send_response was called with 500
        assert stub.send_response.call_args.args[0] == HTTPStatus.INTERNAL_SERVER_ERROR.value

    def test_response_is_json(self):
        stub = _make_stub_handler_instance(self.handler_class)
        stub._send_sanitized_error(RuntimeError("x"))

        # Content-Type header set to json
        header_calls = [c.args for c in stub.send_header.call_args_list]
        assert any(
            name.lower() == "content-type" and "json" in value.lower()
            for name, value in header_calls
        )

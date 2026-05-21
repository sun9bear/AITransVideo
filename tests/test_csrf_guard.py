from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from config import settings  # noqa: E402
from csrf import require_same_origin_state_change  # noqa: E402


class _Request:
    def __init__(
        self,
        *,
        method: str = "POST",
        scheme: str = "https",
        host: str = "aitrans.video",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.method = method
        self.url = SimpleNamespace(scheme=scheme)
        self.headers = {"host": host}
        if headers:
            self.headers.update(headers)


@pytest.fixture(autouse=True)
def _reset_origin_config(monkeypatch):
    monkeypatch.delenv("SITE_URL", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_SITE_URL", raising=False)
    monkeypatch.setattr(settings, "cors_origins", "https://aivideotrans.site")


def test_allows_origin_matching_request_host():
    req = _Request(headers={"origin": "https://aitrans.video"})

    require_same_origin_state_change(req)


def test_allows_origin_matching_forwarded_public_host():
    req = _Request(
        scheme="http",
        host="127.0.0.1:8880",
        headers={
            "origin": "https://aitrans.video",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "aitrans.video",
        },
    )

    require_same_origin_state_change(req)


def test_allows_referer_when_origin_is_missing():
    req = _Request(headers={"referer": "https://aitrans.video/admin/settings"})

    require_same_origin_state_change(req)


def test_allows_configured_cors_origin():
    settings.cors_origins = "https://admin.aitrans.video"
    req = _Request(
        host="gateway.aitrans.video",
        headers={"origin": "https://admin.aitrans.video"},
    )

    require_same_origin_state_change(req)


def test_rejects_cross_origin_state_change():
    req = _Request(headers={"origin": "https://evil.example"})

    with pytest.raises(HTTPException) as exc:
        require_same_origin_state_change(req)

    assert exc.value.status_code == 403
    assert exc.value.detail == "csrf_origin_rejected"


def test_rejects_present_invalid_origin_without_referer_fallback():
    req = _Request(
        headers={
            "origin": "null",
            "referer": "https://aitrans.video/admin/settings",
        },
    )

    with pytest.raises(HTTPException) as exc:
        require_same_origin_state_change(req)

    assert exc.value.status_code == 403
    assert exc.value.detail == "csrf_origin_rejected"


def test_rejects_missing_origin_and_referer_for_state_change():
    req = _Request()

    with pytest.raises(HTTPException) as exc:
        require_same_origin_state_change(req)

    assert exc.value.status_code == 403
    assert exc.value.detail == "csrf_origin_rejected"


def test_safe_method_does_not_require_origin_or_referer():
    req = _Request(method="GET")

    require_same_origin_state_change(req)

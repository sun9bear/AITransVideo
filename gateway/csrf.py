"""Small CSRF guard helpers for session-authenticated state changes."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from config import settings

_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SITE_ORIGIN_ENV_KEYS = ("SITE_URL", "NEXT_PUBLIC_SITE_URL")
_PRODUCTION_ENVS = {"prod", "production"}
_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _first_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    value = headers.get(name.lower())
    if value is not None:
        return value
    return headers.get(name.title())


def _origin_netloc(host: str, port: int | None, scheme: str) -> str:
    default_port = 443 if scheme == "https" else 80
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and port != default_port:
        return f"{host}:{port}"
    return host


def _normalize_origin(value: str | None) -> str | None:
    """Return a canonical ``scheme://host[:port]`` origin or ``None``."""
    raw = (value or "").strip()
    if not raw or raw.lower() == "null":
        return None

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        return None

    host = parts.hostname.lower()
    return f"{scheme}://{_origin_netloc(host, port, scheme)}"


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSEY:
        return False
    return default


def _is_production_env() -> bool:
    return (os.getenv("AVT_ENV") or settings.env or "dev").strip().lower() in _PRODUCTION_ENVS


def _trust_forwarded_host() -> bool:
    """Return whether proxy-supplied public host headers may define same-origin.

    In production, canonical origins should come from SITE_URL /
    NEXT_PUBLIC_SITE_URL or AVT_CORS_ORIGINS. Trusting request-supplied
    X-Forwarded-Host is only safe when the Gateway is reachable exclusively
    through a reverse proxy that overwrites those headers.
    """
    return _env_flag(
        "AVT_CSRF_TRUST_FORWARDED_HOST",
        default=not _is_production_env(),
    )


def _iter_configured_origins() -> Iterable[str]:
    for key in _SITE_ORIGIN_ENV_KEYS:
        origin = _normalize_origin(os.getenv(key))
        if origin:
            yield origin

    for item in settings.cors_origins.split(","):
        origin = _normalize_origin(item)
        if origin:
            yield origin


def _request_public_origin(request: Request) -> str | None:
    headers = request.headers
    trust_forwarded = _trust_forwarded_host()
    proto = None
    host = None
    if trust_forwarded:
        proto = _first_header_value(_header(headers, "x-forwarded-proto"))
        host = _first_header_value(_header(headers, "x-forwarded-host"))

    if not proto:
        proto = getattr(request.url, "scheme", None)
    if not host:
        host = _header(headers, "host")

    if proto and host:
        return _normalize_origin(f"{proto}://{host}")

    base_url = getattr(request, "base_url", None)
    if base_url is not None:
        return _normalize_origin(str(base_url))
    return None


def _allowed_origins(request: Request) -> set[str]:
    origins = set(_iter_configured_origins())
    request_origin = _request_public_origin(request)
    if request_origin:
        origins.add(request_origin)
    return origins


def _request_origin(request: Request) -> str | None:
    headers = request.headers
    raw_origin = _header(headers, "origin")
    if raw_origin and raw_origin.strip():
        return _normalize_origin(raw_origin)
    return _normalize_origin(_header(headers, "referer"))


def _has_explicit_origin_or_referer(request: Request) -> bool:
    headers = request.headers
    origin = _header(headers, "origin")
    referer = _header(headers, "referer")
    return bool(origin and origin.strip()) or bool(referer and referer.strip())


def _fetch_metadata_same_origin(request: Request) -> bool:
    value = _header(request.headers, "sec-fetch-site")
    return bool(value and value.strip().lower() == "same-origin")


def require_same_origin_state_change(request: Request) -> None:
    """Reject cross-site state-changing requests that rely on ambient cookies.

    This is intentionally an Origin/Referer guard, not a full token framework.
    It is meant for authenticated admin/user write endpoints where the frontend
    is same-origin or explicitly listed in ``AVT_CORS_ORIGINS``.
    """
    if request.method.upper() not in _STATE_CHANGING_METHODS:
        return

    origin = _request_origin(request)
    if origin and origin in _allowed_origins(request):
        return

    if not _has_explicit_origin_or_referer(request) and _fetch_metadata_same_origin(request):
        return

    raise HTTPException(status_code=403, detail="csrf_origin_rejected")

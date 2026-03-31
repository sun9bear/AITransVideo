"""Transparent reverse proxy to upstream services.

Forwards requests to web_ui (8876) and job_api (8877) without modification.
This is the core of Step 1 — pure passthrough, zero business logic change.
"""

from __future__ import annotations

import httpx
from fastapi import Request, Response

# Shared client — initialised/closed via app lifespan in main.py
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the shared httpx client. Raises if not initialised."""
    if _client is None:
        raise RuntimeError("httpx client not initialised — call init_client() first")
    return _client


def init_client() -> httpx.AsyncClient:
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def proxy_request(
    request: Request,
    upstream_base: str,
    strip_prefix: str = "",
    override_body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """Forward a request to an upstream service and return its response.

    Args:
        override_body: If provided, use this as the request body instead of
                       the original request body.
        extra_headers: If provided, merge these headers into the forwarded
                       request (e.g. internal ``X-User-Id``).
    """

    # Build upstream URL
    path = request.url.path
    if strip_prefix and path.startswith(strip_prefix):
        path = path[len(strip_prefix):] or "/"
    query = str(request.url.query)
    upstream_url = f"{upstream_base.rstrip('/')}{path}"
    if query:
        upstream_url = f"{upstream_url}?{query}"

    # Read request body (use override if provided)
    body = override_body if override_body is not None else await request.body()

    # Forward headers (skip hop-by-hop)
    skip_headers = {"host", "connection", "transfer-encoding", "keep-alive"}
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in skip_headers
    }
    # Update content-length if body was overridden
    if override_body is not None:
        headers["content-length"] = str(len(body))
    # Merge internal headers (e.g. X-User-Id for upstream identity)
    if extra_headers:
        headers.update(extra_headers)

    client = get_client()
    upstream_response = await client.request(
        method=request.method,
        url=upstream_url,
        headers=headers,
        content=body,
    )

    # Build response, preserving upstream status and headers
    response_headers = dict(upstream_response.headers)
    # Remove hop-by-hop headers from response
    for h in ("transfer-encoding", "connection", "keep-alive"):
        response_headers.pop(h, None)

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )

"""Transparent reverse proxy to upstream services.

Forwards requests to web_ui (8876) and job_api (8877) without modification.
This is the core of Step 1 — pure passthrough, zero business logic change.
"""

from __future__ import annotations

import httpx
from fastapi import Request, Response


async def proxy_request(
    request: Request,
    upstream_base: str,
    strip_prefix: str = "",
) -> Response:
    """Forward a request to an upstream service and return its response."""

    # Build upstream URL
    path = request.url.path
    if strip_prefix and path.startswith(strip_prefix):
        path = path[len(strip_prefix):] or "/"
    query = str(request.url.query)
    upstream_url = f"{upstream_base.rstrip('/')}{path}"
    if query:
        upstream_url = f"{upstream_url}?{query}"

    # Read request body
    body = await request.body()

    # Forward headers (skip hop-by-hop)
    skip_headers = {"host", "connection", "transfer-encoding", "keep-alive"}
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in skip_headers
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
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

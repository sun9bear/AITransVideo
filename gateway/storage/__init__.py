"""Gateway storage backends (plan 2026-04-23).

Phase 2: the only concrete backend shipped here is Cloudflare R2 for the
``publish.dubbed_video`` artifact, routed via ``backend_router``. The default
``local`` backend keeps the historic gateway -> Job API byte-passthrough and
requires no code in this package (the request falls through the intercept
layer untouched).

Nothing is re-exported on purpose — callers should import the concrete
module they need (``from storage.backend_router import resolve_download_target``)
so the dependency graph stays explicit.
"""

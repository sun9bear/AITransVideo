"""Shared library for R2 artifact publish + parity logic.

This package is intentionally **flat and dependency-light** so the Gateway
container (which does NOT install pydub / ffmpeg / Job-API-only packages)
can import from it without triggering ``services.jobs.__init__.py``.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §2.1

Allowed imports inside this package:
- stdlib (always)
- ``services.manifest_reader`` (single-file module, no jobs deps)
- ``boto3`` / ``botocore`` (Gateway-only deps, available in both containers
  because Gateway pulls them; never imported at top level — only inside
  function bodies that the publisher path actually exercises)

Forbidden inside this package:
- ``services.jobs.*`` — pulls pydub / ffmpeg via models / pipeline glue
- ``fastapi`` / ``sqlalchemy.ext.asyncio`` — caller passes session in if
  needed; we keep this layer DB-agnostic so unit tests stay fast

Regression guard: ``tests/test_legacy_cleanup_guards.py`` AST-scans this
directory and ``gateway/`` for any ``from services.jobs`` import.
"""

"""Audit P2-18E / F-HIGH-3 regression: every mutation fetch in the
Next.js frontend that targets a same-origin API path must explicitly
pass ``credentials: 'include'`` so the session cookie reaches the
gateway.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        F-HIGH-3 — TranslationForm.upload-video used POST without
                   ``credentials: 'include'``. On browsers with
                   tracking-prevention modes (Safari ITP, some
                   Chromium privacy builds, Edge strict mode) the
                   session cookie is dropped on the cross-site
                   layer and the gateway sees an anonymous request
                   → 401 → silent upload failure.

Sweep on 2026-05-08 found ONE actual occurrence (TranslationForm)
plus 10 false-positive matches that were caused by a too-naive regex
ignoring nested ``{}`` (headers/body objects). This guard uses a
brace-balanced parser so the false-positive class is closed.

Adding a new mutation fetch that hits a gateway path without
``credentials`` will fail this test in CI. The only acceptable
remediation is to pass ``credentials`` (typically ``'include'``;
``'omit'`` is allowed when intentional).
"""
from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_FRONTEND_SRC = _REPO_ROOT / "frontend-next" / "src"

# Path prefixes that map to the gateway / Job API. Mutations against
# these MUST send credentials; the audit's concern was specifically
# session-cookie loss on same-origin API calls.
_API_PREFIXES = ("/gateway/", "/api/", "/auth/", "/job-api/")

# HTTP methods that mutate. GET / HEAD / OPTIONS are read-only and
# don't need the credentials guard (server-side auth on those is a
# separate concern; rate-limit / CSRF aren't relevant for safe verbs).
_MUTATING = {"POST", "PUT", "DELETE", "PATCH"}


def _find_fetch_calls(src: str):
    """Yield ``(start_offset, full_call_text)`` for every ``fetch(...)``
    invocation in ``src``. Uses brace/quote depth tracking so nested
    options objects (headers, body) don't confuse the parser the way
    a naive regex does."""
    i = 0
    while True:
        idx = src.find("fetch(", i)
        if idx == -1:
            return
        # Walk forward to the matching paren, respecting strings and
        # nested brackets.
        depth = 1
        in_str: str | None = None
        j = idx + len("fetch(")
        while j < len(src) and depth > 0:
            c = src[j]
            if in_str is not None:
                if c == "\\" and j + 1 < len(src):
                    j += 2
                    continue
                if c == in_str:
                    in_str = None
            elif c in ("'", '"', "`"):
                in_str = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            j += 1
        if depth == 0:
            yield idx, src[idx:j]
        i = j


def test_no_mutation_fetch_missing_credentials():
    """Walk every .ts/.tsx under frontend-next/src and assert no
    mutation fetch against a gateway / API path is missing
    ``credentials: ...``."""
    if not _FRONTEND_SRC.is_dir():
        # Repo without the Next.js frontend (some sub-checkouts) —
        # nothing to scan.
        return

    issues: list[str] = []
    scanned_calls = 0
    for f in _FRONTEND_SRC.rglob("*.ts*"):
        if not f.is_file():
            continue
        # Skip generated test/snapshot files if any.
        rel = f.relative_to(_REPO_ROOT)
        src = f.read_text(encoding="utf-8")
        for start, call in _find_fetch_calls(src):
            url_match = re.search(
                r"^fetch\(\s*['\"`]([^'\"`]+)['\"`]", call
            )
            if url_match is None:
                continue
            url = url_match.group(1)
            if not any(p in url for p in _API_PREFIXES):
                continue
            method_m = re.search(
                r"method\s*:\s*['\"](\w+)['\"]", call
            )
            method = (
                method_m.group(1).upper() if method_m else "GET"
            )
            if method not in _MUTATING:
                continue
            scanned_calls += 1
            if "credentials" not in call:
                line = src[:start].count("\n") + 1
                issues.append(
                    f"  {rel}:{line}  {method:<6} {url}"
                )

    assert scanned_calls > 0, (
        "P2-18E regression scaffold: brace-aware scan found 0 "
        "mutation fetches. Either the frontend was restructured "
        "and the scan can't find calls, or the suite is running "
        "against a non-frontend checkout. Investigate before "
        "treating this as 'all clean'."
    )
    assert issues == [], (
        "P2-18E regression: the following mutation fetch(es) target "
        "a gateway / API path WITHOUT ``credentials: '...'``. On "
        "browsers with tracking-prevention modes (Safari ITP, "
        "Chromium strict privacy, Edge strict tracking prevention) "
        "the session cookie is dropped and the gateway sees an "
        "anonymous request → 401. Add ``credentials: 'include'`` "
        "to the fetch options.\n\n"
        "Hits:\n" + "\n".join(issues)
    )

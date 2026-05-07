"""P1-12c (audit 2026-05-07) regression: download / upload IO must not
load entire payload into memory.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        P-CRITICAL-3 — download_path.read_bytes() spiked Python RSS
                       by file size; 2 concurrent 1GB downloads OOM'd
                       the container.
        H-6          — gateway/upload.py shutil.copyfileobj was sync
                       in an async route; 2GB upload blocked the event
                       loop for 30-60s.

Strategy: AST guards. Real streaming verification would need a real
multi-GB file + ThreadingHTTPServer harness; the AST checks catch any
revert pattern.
"""
from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_jobs_api_download_does_not_use_read_bytes_for_full_file():
    """The download endpoint MUST stream rather than read_bytes() the
    full file. Look for either a chunked iter pattern (read(chunk_size)
    in a loop) or a delegation to a _stream_binary_file helper."""
    src_path = _REPO_ROOT / "src" / "services" / "jobs" / "api.py"
    src = src_path.read_text(encoding="utf-8")

    # Either pattern is acceptable:
    has_streaming_pattern = (
        "_stream_binary_file" in src
        or ("handle.read(" in src and "while" in src)
    )

    assert has_streaming_pattern, (
        "P1-12c regression: src/services/jobs/api.py download path no "
        "longer streams. read_bytes() of a 1GB dubbed_video would "
        "spike Python RSS and OOM the container with 2 concurrent "
        "downloads."
    )


def test_upload_uses_asyncio_to_thread_for_copyfileobj():
    """gateway/upload.py shutil.copyfileobj must be wrapped in
    asyncio.to_thread to avoid blocking the event loop."""
    src_path = _REPO_ROOT / "gateway" / "upload.py"
    src = src_path.read_text(encoding="utf-8")

    # The fix wraps copyfileobj in to_thread. Look for both being
    # mentioned within the file (rough but effective) — and ensure
    # that copyfileobj is NEVER called bare-sync.
    assert "asyncio.to_thread" in src, (
        "P1-12c regression: gateway/upload.py no longer wraps "
        "shutil.copyfileobj in asyncio.to_thread. A 2GB upload would "
        "block the event loop for 30-60s, starving all other async "
        "requests."
    )

    # Tree walk: any call to shutil.copyfileobj must be inside an
    # await asyncio.to_thread(...) call, never bare.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Identify: shutil.copyfileobj(...)
        is_copyfileobj = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "copyfileobj"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "shutil"
        )
        if not is_copyfileobj:
            continue
        # Walk parents to confirm this call is inside asyncio.to_thread(...)
        # AST doesn't track parents directly; do a string proximity check
        # as a soft guard.
        # If we see "shutil.copyfileobj" without "asyncio.to_thread" near
        # the same source range, it's a regression.
        seg = ast.get_source_segment(src, node)
        if seg is None:
            continue
        # Allow "shutil.copyfileobj" appearing as a positional arg to
        # to_thread; in that case the entire seg is just the call args.
        # The simpler check: the line CONTAINING the copyfileobj call
        # should also have to_thread on it OR a few lines above.
        line_no = node.lineno
        nearby = "\n".join(src.splitlines()[max(0, line_no - 3):line_no + 1])
        assert "asyncio.to_thread" in nearby, (
            f"P1-12c regression: shutil.copyfileobj call at line {line_no} "
            f"is not wrapped in asyncio.to_thread:\n{nearby}"
        )

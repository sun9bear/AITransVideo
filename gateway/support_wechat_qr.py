"""WeChat customer-service QR image storage helpers.

Plan 2026-05-08 (L1 follow-up):

- Admin uploads a PNG/JPG QR image via /api/admin/support/wechat-qr.
- Image is stored on disk under ``${AIVIDEOTRANS_CONFIG_DIR}/`` so it
  survives container recreates (config dir is bind-mounted on US).
- Public endpoint /api/support/wechat-qr serves it back. The path is
  deterministic so cache headers can be aggressive.

Constraints (defense against abuse):
- Allowed types: image/png, image/jpeg only.
- Max size: 1 MB. Anything bigger is rejected at the upload boundary.
- Single QR per deployment (overwrite on re-upload). No multi-QR / per
  region routing in this iteration.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


CONFIG_DIR_ENV = "AIVIDEOTRANS_CONFIG_DIR"
_PNG_FILENAME = "support_wechat_qr.png"
_JPG_FILENAME = "support_wechat_qr.jpg"
MAX_BYTES = 1 * 1024 * 1024  # 1 MB

# Maps Content-Type → on-disk filename for that variant.
_CONTENT_TYPE_TO_FILENAME = {
    "image/png": _PNG_FILENAME,
    "image/jpeg": _JPG_FILENAME,
    "image/jpg": _JPG_FILENAME,  # some clients send this non-standard type
}


def _config_dir() -> Path:
    raw = os.environ.get(CONFIG_DIR_ENV, "/opt/aivideotrans/config")
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _candidate_paths() -> list[Path]:
    base = _config_dir()
    return [base / _PNG_FILENAME, base / _JPG_FILENAME]


def existing_qr_path() -> Path | None:
    """Return the on-disk path of the currently uploaded QR, or None.

    If both PNG and JPG exist (e.g. re-upload changed format mid-stream),
    return the most recently modified one — the older variant is dead
    weight that ``save_qr`` should have cleaned up.
    """
    candidates = [p for p in _candidate_paths() if p.exists() and p.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def get_qr_metadata() -> dict | None:
    """Return ``{"path": Path, "size_bytes": int, "uploaded_at": datetime, "filename": str}``
    for the currently uploaded QR, or None if there isn't one."""
    p = existing_qr_path()
    if p is None:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    return {
        "path": p,
        "size_bytes": int(st.st_size),
        "uploaded_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        "filename": p.name,
    }


def save_qr(*, content_type: str, body: bytes) -> dict:
    """Validate + persist the QR image.

    Raises ``ValueError`` for content-type / size violations. On success
    returns the same dict shape as ``get_qr_metadata``.
    """
    ct = (content_type or "").lower().strip()
    target_name = _CONTENT_TYPE_TO_FILENAME.get(ct)
    if target_name is None:
        raise ValueError(
            f"unsupported content-type: {content_type!r}. only PNG / JPEG allowed."
        )
    if not body:
        raise ValueError("empty file")
    if len(body) > MAX_BYTES:
        raise ValueError(f"file too large: {len(body)} bytes > {MAX_BYTES}")

    base = _config_dir()
    target = base / target_name
    tmp = base / (target_name + ".tmp")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, target)
    except OSError as exc:
        # cleanup tmp; do NOT touch existing target
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"failed to write QR file: {exc}") from exc

    # If the upload changed format (e.g. PNG → JPEG), unlink the old variant.
    for other in _candidate_paths():
        if other != target and other.exists():
            try:
                other.unlink()
            except OSError:
                logger.warning("failed to unlink stale QR variant %s", other)

    meta = get_qr_metadata()
    assert meta is not None
    return meta


def delete_qr() -> bool:
    removed = False
    for p in _candidate_paths():
        if p.exists():
            try:
                p.unlink()
                removed = True
            except OSError as exc:
                logger.warning("failed to remove QR file %s: %s", p, exc)
    return removed


def public_url() -> str:
    """Frontend / widget use this URL to embed the QR. Cache-busted by
    the upload mtime so a re-upload immediately invalidates browser
    caches without manual user action."""
    meta = get_qr_metadata()
    if meta is None:
        return "/api/support/wechat-qr"
    return f"/api/support/wechat-qr?v={int(meta['uploaded_at'].timestamp())}"

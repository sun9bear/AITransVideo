"""Minimal local helper for guarded ext4 resize operations.

This process is intentionally separate from the main Gateway container so the
web API does not need long-lived access to the raw block device.  It exposes
only status and fixed-device resize2fs operations, bound to loopback and gated
by a bearer token shared with Gateway through production env.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="AIVideoTrans disk resize helper")
_resize_lock = threading.Lock()


class ResizeRequest(BaseModel):
    dry_run: bool = False
    confirm: bool = False


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _device() -> str:
    return os.environ.get("AVT_ADMIN_DISK_RESIZE_DEVICE", "/dev/sdb").strip() or "/dev/sdb"


def _token() -> str:
    return os.environ.get("AVT_ADMIN_DISK_RESIZE_HELPER_TOKEN", "").strip()


def _timeout() -> int:
    try:
        return max(30, int(os.environ.get("AVT_ADMIN_DISK_RESIZE_TIMEOUT_SECONDS", "300")))
    except ValueError:
        return 300


def _min_delta() -> int:
    try:
        return max(1, int(os.environ.get("AVT_ADMIN_DISK_RESIZE_MIN_DELTA_BYTES", 16 * 1024 * 1024)))
    except ValueError:
        return 16 * 1024 * 1024


def _require_token(authorization: str | None) -> None:
    expected = _token()
    if not expected:
        raise HTTPException(status_code=503, detail="resize helper token is not configured")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid resize helper token")


def _run_optional(args: list[str], timeout: int = 5) -> str:
    try:
        return subprocess.check_output(
            args,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).strip()
    except Exception:
        return ""


def _block_device_size_bytes(device: str) -> int | None:
    out = _run_optional(["blockdev", "--getsize64", device])
    if not out:
        return None
    try:
        return int(out.splitlines()[-1].strip())
    except ValueError:
        return None


def _ext4_filesystem_size_bytes(device: str) -> int | None:
    out = _run_optional(["tune2fs", "-l", device])
    if not out:
        return None
    block_count: int | None = None
    block_size: int | None = None
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        try:
            if key.strip().lower() == "block count":
                block_count = int(value.strip())
            elif key.strip().lower() == "block size":
                block_size = int(value.strip())
        except ValueError:
            continue
    if block_count is None or block_size is None:
        return None
    return block_count * block_size


def _status() -> dict[str, Any]:
    device = _device()
    device_visible = os.path.exists(device)
    resize2fs_available = bool(shutil.which("resize2fs"))
    tune2fs_available = bool(shutil.which("tune2fs"))
    device_bytes = _block_device_size_bytes(device) if device_visible else None
    filesystem_bytes = _ext4_filesystem_size_bytes(device) if device_visible else None
    needs_resize = bool(
        device_bytes
        and filesystem_bytes
        and device_bytes > filesystem_bytes + _min_delta()
    )
    return {
        "device": device,
        "device_visible": device_visible,
        "resize2fs_available": resize2fs_available,
        "tune2fs_available": tune2fs_available,
        "device_bytes": device_bytes,
        "filesystem_bytes": filesystem_bytes,
        "needs_resize": needs_resize,
        "can_resize": bool(device_visible and resize2fs_available and tune2fs_available and needs_resize),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
def status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_token(authorization)
    return _status()


@app.post("/resize")
def resize(
    body: ResizeRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_token(authorization)
    before = _status()
    if not before["can_resize"]:
        raise HTTPException(
            status_code=409,
            detail={"message": "filesystem does not currently need resize", "before": before},
        )
    if body.dry_run:
        return {"dry_run": True, "ran": False, "before": before, "after": before, "output": ""}
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required")
    if not _resize_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="resize already running")
    try:
        completed = subprocess.run(
            ["resize2fs", before["device"]],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        raise HTTPException(
            status_code=504,
            detail={"message": "resize2fs timed out", "output": output[-4000:]},
        ) from exc
    finally:
        _resize_lock.release()

    output = (completed.stdout or "")[-8000:]
    after = _status()
    if completed.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "resize2fs failed",
                "output": output,
                "before": before,
                "after": after,
            },
        )
    return {
        "dry_run": False,
        "ran": True,
        "device": before["device"],
        "output": output,
        "before": before,
        "after": after,
    }


if __name__ == "__main__":
    host = os.environ.get("AVT_ADMIN_DISK_RESIZE_HELPER_HOST", "127.0.0.1")
    port = int(os.environ.get("AVT_ADMIN_DISK_RESIZE_HELPER_PORT", "8891"))
    uvicorn.run(app, host=host, port=port)

"""Admin disk management API.

Surfaces the operational checks we have been doing by hand:

- filesystem capacity for the mounted project data disk
- disk job directories that no longer have a Gateway DB row
- expired terminal jobs that existing project cleanup may purge
- protected/admin jobs whose deadline elapsed but must not be swept

Mutating endpoints deliberately accept job ids, not paths.  Paths are
re-derived from the configured project root and checked against the same
whitelist used by ``project_cleanup.py`` before any rmtree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import Job, User
from project_cleanup import (
    DEFAULT_SAFE_PROJECT_ROOTS,
    PURGEABLE_STATUSES,
    RETENTION_DAYS,
    _is_expired,
    _is_safe_project_dir,
    cleanup_expired_projects,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/disk", tags=["admin-disk"])
_resize_lock = asyncio.Lock()

_JOB_ID_RE = re.compile(r"^job_[A-Za-z0-9_-]{8,128}$")
_DEFAULT_SCAN_ROOTS = (
    Path("/opt/aivideotrans/app/projects"),
    Path("/opt/aivideotrans/data/projects"),
)


class OrphanCleanupRequest(BaseModel):
    job_ids: list[str] = Field(default_factory=list, max_length=100)
    dry_run: bool = False

    @field_validator("job_ids")
    @classmethod
    def validate_job_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            job_id = (raw or "").strip()
            if not _JOB_ID_RE.match(job_id):
                raise ValueError(f"非法 job_id: {raw!r}")
            if job_id not in seen:
                cleaned.append(job_id)
                seen.add(job_id)
        return cleaned


class ExpiredCleanupRequest(BaseModel):
    dry_run: bool = True


class ResizeFilesystemRequest(BaseModel):
    dry_run: bool = False
    confirm: bool = False


def _is_admin(user: User) -> bool:
    return (getattr(user, "role", None) or "user") == "admin"


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _bytes_to_gib(value: int | float | None) -> float:
    return round(float(value or 0) / 1024 / 1024 / 1024, 2)


def _configured_project_roots() -> tuple[Path, ...]:
    raw = os.environ.get("AVT_ADMIN_DISK_PROJECT_ROOTS", "").strip()
    if raw:
        roots = [Path(part.strip()) for part in raw.split(os.pathsep) if part.strip()]
        if roots:
            return tuple(roots)
    return _DEFAULT_SCAN_ROOTS


def _resolve_scan_root() -> Path:
    for root in _configured_project_roots():
        if root.is_dir():
            return root
    return _configured_project_roots()[0]


def _effective_safe_roots(scan_root: Path) -> tuple[Path, ...]:
    roots = [scan_root]
    for root in DEFAULT_SAFE_PROJECT_ROOTS:
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _directory_size_bytes(path: Path) -> int:
    """Return best-effort directory size.

    GNU ``du`` is much faster on production Linux hosts; the os.walk fallback
    keeps tests and local Windows development portable.
    """
    try:
        out = subprocess.check_output(
            ["du", "-sB1", "--", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return int(out.split()[0])
    except Exception:
        total = 0
        for base, dirnames, filenames in os.walk(path):
            dirnames[:] = [
                name
                for name in dirnames
                if not (Path(base) / name).is_symlink()
            ]
            for filename in filenames:
                candidate = Path(base) / filename
                try:
                    total += candidate.stat().st_size
                except OSError:
                    continue
        return total


def _disk_usage(path: Path) -> dict:
    probe = path if path.exists() else path.parent
    usage = shutil.disk_usage(probe)
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "total_gib": _bytes_to_gib(usage.total),
        "used_gib": _bytes_to_gib(usage.used),
        "free_gib": _bytes_to_gib(usage.free),
        "use_percent": round((usage.used / usage.total) * 100, 1)
        if usage.total
        else 0.0,
    }


def _run_optional(args: list[str], timeout: int = 3) -> str:
    try:
        return subprocess.check_output(
            args,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).strip()
    except Exception:
        return ""


def _mount_info(path: Path) -> dict:
    out = _run_optional(
        ["findmnt", "-no", "SOURCE,FSTYPE,SIZE,USED,AVAIL,TARGET", str(path)]
    )
    if not out:
        return {"available": False}
    parts = out.split()
    return {
        "available": True,
        "source": parts[0] if len(parts) > 0 else "",
        "fstype": parts[1] if len(parts) > 1 else "",
        "size": parts[2] if len(parts) > 2 else "",
        "used": parts[3] if len(parts) > 3 else "",
        "avail": parts[4] if len(parts) > 4 else "",
        "target": parts[5] if len(parts) > 5 else str(path),
    }


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _configured_resize_device() -> str:
    return os.environ.get("AVT_ADMIN_DISK_RESIZE_DEVICE", "/dev/sdb").strip() or "/dev/sdb"


def _configured_resize_helper_url() -> str:
    return os.environ.get(
        "AVT_ADMIN_DISK_RESIZE_HELPER_URL",
        "http://127.0.0.1:8891",
    ).strip().rstrip("/")


def _configured_resize_helper_token() -> str:
    return os.environ.get("AVT_ADMIN_DISK_RESIZE_HELPER_TOKEN", "").strip()


def _configured_resize_timeout() -> int:
    try:
        return max(30, int(os.environ.get("AVT_ADMIN_DISK_RESIZE_TIMEOUT_SECONDS", "300")))
    except ValueError:
        return 300


def _normalize_mount_source(source: str | None) -> str:
    if not source:
        return ""
    return source.split("[", 1)[0]


def _json_http_error_reason(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read().decode("utf-8", errors="replace"))
    except Exception:
        return f"helper HTTP {exc.code}"
    detail = data.get("detail")
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str):
            return message
    if isinstance(detail, str):
        return detail
    message = data.get("message")
    return message if isinstance(message, str) else f"helper HTTP {exc.code}"


def _get_resize_helper_status() -> dict:
    token = _configured_resize_helper_token()
    if not token:
        return {"available": False, "reason": "未配置磁盘扩容 helper token。"}
    request = urllib.request.Request(
        f"{_configured_resize_helper_url()}/status",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"available": False, "reason": _json_http_error_reason(exc)}
    except Exception as exc:
        return {"available": False, "reason": f"无法连接磁盘扩容 helper: {exc}"}
    if not isinstance(payload, dict):
        return {"available": False, "reason": "磁盘扩容 helper 返回格式无效。"}
    payload["available"] = True
    return payload


def _build_resize_hint(root: Path, mount: dict | None = None) -> dict:
    mount = mount or _mount_info(root)
    device = _configured_resize_device()
    mount_source = _normalize_mount_source(mount.get("source"))
    feature_enabled = _env_flag("AVT_ADMIN_DISK_RESIZE_ENABLED", default=False)
    helper = _get_resize_helper_status() if feature_enabled else {}
    helper_device = helper.get("device") if isinstance(helper.get("device"), str) else device
    device_visible = bool(helper.get("device_visible"))
    resize2fs_available = bool(helper.get("resize2fs_available"))
    tune2fs_available = bool(helper.get("tune2fs_available"))
    device_bytes = helper.get("device_bytes") if isinstance(helper.get("device_bytes"), int) else None
    filesystem_bytes = (
        helper.get("filesystem_bytes")
        if isinstance(helper.get("filesystem_bytes"), int)
        else None
    )
    needs_resize = False
    can_resize = False

    commands = [
        f"lsblk -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL {helper_device}",
        f"df -hT {root}",
        f"resize2fs {helper_device}",
    ]

    reason = "后台未启用一键扩展文件系统。"
    if not feature_enabled:
        pass
    elif not helper.get("available"):
        reason = str(helper.get("reason") or "磁盘扩容 helper 不可用。")
    elif not mount.get("available"):
        reason = "无法识别项目数据盘挂载信息。"
    elif (mount.get("fstype") or "").lower() != "ext4":
        reason = f"当前文件系统是 {mount.get('fstype') or 'unknown'}，仅允许 ext4。"
    elif mount_source and mount_source != helper_device:
        reason = f"挂载源是 {mount_source}，与配置设备 {helper_device} 不一致。"
    elif not device_visible:
        reason = f"磁盘扩容 helper 看不到 {helper_device}。"
    elif not resize2fs_available:
        reason = "磁盘扩容 helper 缺少 resize2fs。"
    elif not tune2fs_available:
        reason = "磁盘扩容 helper 缺少 tune2fs，无法安全判断 ext4 当前大小。"
    else:
        if not device_bytes:
            reason = f"无法读取 {helper_device} 的块设备大小。"
        elif not filesystem_bytes:
            reason = f"无法读取 {helper_device} 的 ext4 文件系统大小。"
        else:
            # resize2fs grows ext4 to the block device size.  Use tune2fs block
            # counts instead of df totals because df excludes ext4 metadata and
            # reserved blocks, which would make a fully resized filesystem look
            # smaller than the device.
            min_delta = int(os.environ.get("AVT_ADMIN_DISK_RESIZE_MIN_DELTA_BYTES", 16 * 1024 * 1024))
            needs_resize = device_bytes > filesystem_bytes + min_delta
            can_resize = needs_resize
            reason = (
                "块设备容量大于 ext4 文件系统容量，可以执行 resize2fs。"
                if needs_resize
                else "文件系统已使用当前块设备容量。"
            )

    return {
        "enabled": bool(feature_enabled and can_resize),
        "feature_enabled": feature_enabled,
        "can_resize": can_resize,
        "needs_resize": needs_resize,
        "reason": reason,
        "commands": commands,
        "device": helper_device,
        "helper_available": bool(helper.get("available")),
        "mount_source": mount_source,
        "device_visible": device_visible,
        "resize2fs_available": resize2fs_available,
        "tune2fs_available": tune2fs_available,
        "device_bytes": device_bytes,
        "device_gib": _bytes_to_gib(device_bytes),
        "filesystem_bytes": filesystem_bytes,
        "filesystem_gib": _bytes_to_gib(filesystem_bytes),
    }


def _is_allowed_scan_root(root: Path) -> bool:
    try:
        resolved = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    for safe_root in DEFAULT_SAFE_PROJECT_ROOTS:
        try:
            resolved_safe = safe_root.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if resolved == resolved_safe:
            return True
        try:
            resolved.relative_to(resolved_safe)
        except ValueError:
            continue
        return True
    return False


def _as_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _retention_deadline(job: Job) -> datetime | None:
    explicit = _as_aware_utc(getattr(job, "expires_at", None))
    if explicit is not None:
        return explicit
    created = _as_aware_utc(getattr(job, "created_at", None))
    if created is None:
        return None
    return created + timedelta(days=RETENTION_DAYS)


def _title_from_obj(obj, depth: int = 0) -> str:
    if depth > 6:
        return ""
    keys = ("display_name", "title", "source_title", "video_title", "name")
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip() and not value.startswith("job_"):
                return value.strip()
        for value in obj.values():
            found = _title_from_obj(value, depth + 1)
            if found:
                return found
    if isinstance(obj, list):
        for value in obj[:40]:
            found = _title_from_obj(value, depth + 1)
            if found:
                return found
    return ""


def _infer_title_from_disk(job_dir: Path, job_id: str) -> str:
    candidates: list[Path] = []
    for name in (
        "manifest.json",
        "download_metadata.json",
        "job.json",
        "metadata.json",
        "source_info.json",
        "input.json",
        "result.json",
        "state.json",
    ):
        path = job_dir / name
        if path.exists():
            candidates.append(path)
    try:
        for path in sorted(job_dir.glob("*.json"))[:20]:
            if path not in candidates:
                candidates.append(path)
    except OSError:
        pass

    for path in candidates[:40]:
        try:
            if path.stat().st_size > 5_000_000:
                continue
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        found = _title_from_obj(data)
        if found:
            return found
    return job_id


def _job_title(job: Job) -> str:
    return (
        (getattr(job, "display_name", None) or "")
        or (getattr(job, "title", None) or "")
        or getattr(job, "job_id", "")
    )


def _iter_disk_job_dirs(root: Path) -> Iterable[tuple[str, Path]]:
    if not root.is_dir():
        return []
    rows: list[tuple[str, Path]] = []
    for user_dir in sorted(root.iterdir()):
        if not user_dir.is_dir():
            continue
        for job_dir in sorted(user_dir.iterdir()):
            if job_dir.is_dir() and job_dir.name.startswith("job_"):
                rows.append((user_dir.name, job_dir))
    return rows


def _candidate_row(
    *,
    job_dir: Path,
    user_id: str,
    size_bytes: int,
    title: str,
    job: Job | None = None,
    category: str,
    now: datetime,
) -> dict:
    deadline = _retention_deadline(job) if job is not None else None
    return {
        "category": category,
        "job_id": job_dir.name,
        "user_id": user_id,
        "path": str(job_dir),
        "size_bytes": size_bytes,
        "size_gib": _bytes_to_gib(size_bytes),
        "mtime": datetime.fromtimestamp(
            job_dir.stat().st_mtime, timezone.utc
        ).isoformat(),
        "title": title,
        "status": getattr(job, "status", "") if job is not None else "",
        "current_stage": getattr(job, "current_stage", "") if job is not None else "",
        "role_snapshot": getattr(job, "role_snapshot", "") if job is not None else "",
        "expires_at": deadline.isoformat() if deadline else "",
        "expired": bool(deadline and deadline < now),
    }


def _find_largest_files(root: Path, *, limit: int = 25) -> list[dict]:
    if not root.is_dir():
        return []
    files: list[tuple[int, str]] = []
    min_bytes = int(os.environ.get("AVT_ADMIN_DISK_LARGE_FILE_MIN_BYTES", 300 * 1024 * 1024))
    for base, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if not (Path(base) / name).is_symlink()
        ]
        for filename in filenames:
            path = Path(base) / filename
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size >= min_bytes:
                files.append((size, str(path)))
    return [
        {"size_bytes": size, "size_gib": _bytes_to_gib(size), "path": path}
        for size, path in sorted(files, reverse=True)[:limit]
    ]


async def build_disk_overview(
    db: AsyncSession,
    *,
    project_root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    root = project_root or _resolve_scan_root()

    result = await db.execute(select(Job))
    jobs = {job.job_id: job for job in result.scalars().all()}

    buckets: dict[str, list[dict]] = {
        "orphan_dirs": [],
        "expired_dirs": [],
        "protected_expired_dirs": [],
        "failed_dirs": [],
        "active_largest_dirs": [],
    }
    all_rows: list[dict] = []

    for user_id, job_dir in _iter_disk_job_dirs(root):
        job_id = job_dir.name
        job = jobs.get(job_id)
        size = _directory_size_bytes(job_dir)
        if job is None:
            row = _candidate_row(
                job_dir=job_dir,
                user_id=user_id,
                size_bytes=size,
                title=_infer_title_from_disk(job_dir, job_id),
                category="orphan",
                now=now,
            )
            buckets["orphan_dirs"].append(row)
            all_rows.append(row)
            continue

        status = (getattr(job, "status", "") or "").lower()
        role = (getattr(job, "role_snapshot", "") or "").lower()
        deadline = _retention_deadline(job)
        deadline_expired = bool(deadline and deadline < now)
        common = dict(
            job_dir=job_dir,
            user_id=user_id,
            size_bytes=size,
            title=_job_title(job),
            job=job,
            now=now,
        )
        if (
            status in PURGEABLE_STATUSES
            and role == "admin"
            and deadline_expired
        ):
            row = _candidate_row(category="protected_expired", **common)
            buckets["protected_expired_dirs"].append(row)
        elif status in PURGEABLE_STATUSES and _is_expired(job, now):
            row = _candidate_row(category="expired", **common)
            buckets["expired_dirs"].append(row)
        elif status in {"failed", "cancelled", "canceled", "error"}:
            row = _candidate_row(category="failed", **common)
            buckets["failed_dirs"].append(row)
        else:
            row = _candidate_row(category="active", **common)
            buckets["active_largest_dirs"].append(row)
        all_rows.append(row)

    for key in buckets:
        buckets[key].sort(key=lambda item: item["size_bytes"], reverse=True)
    buckets["active_largest_dirs"] = buckets["active_largest_dirs"][:20]

    summary = {
        "disk_job_dir_count": len(all_rows),
        "disk_job_bytes": sum(item["size_bytes"] for item in all_rows),
        "disk_job_gib": _bytes_to_gib(sum(item["size_bytes"] for item in all_rows)),
        "db_job_count": len(jobs),
    }
    for key in (
        "orphan_dirs",
        "expired_dirs",
        "protected_expired_dirs",
        "failed_dirs",
    ):
        total = sum(item["size_bytes"] for item in buckets[key])
        summary[f"{key}_count"] = len(buckets[key])
        summary[f"{key}_bytes"] = total
        summary[f"{key}_gib"] = _bytes_to_gib(total)

    mount = _mount_info(root)
    return {
        "scanned_at": now.isoformat(),
        "project_root": str(root),
        "filesystem": _disk_usage(root),
        "mount": mount,
        "summary": summary,
        "categories": buckets,
        "largest_files": _find_largest_files(root),
        "resize_hint": _build_resize_hint(root, mount),
    }


def _find_job_dirs_for_ids(root: Path, job_ids: set[str]) -> dict[str, list[Path]]:
    found = {job_id: [] for job_id in job_ids}
    for _user_id, job_dir in _iter_disk_job_dirs(root):
        if job_dir.name in found:
            found[job_dir.name].append(job_dir)
    return found


async def cleanup_orphan_dirs(
    db: AsyncSession,
    *,
    job_ids: list[str],
    dry_run: bool,
    project_root: Path | None = None,
    safe_roots: tuple[Path, ...] | None = None,
    enforce_safe_root: bool = True,
) -> dict:
    if not job_ids:
        raise HTTPException(status_code=400, detail="至少选择一个 job_id")
    root = project_root or _resolve_scan_root()
    if enforce_safe_root and not _is_allowed_scan_root(root):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "扫描根目录不在允许的项目数据目录内，已禁止删除操作",
                "project_root": str(root),
            },
        )
    unique_ids = list(dict.fromkeys(job_ids))

    result = await db.execute(select(Job.job_id).where(Job.job_id.in_(unique_ids)))
    existing = set(result.scalars().all())
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "部分 job_id 仍存在数据库记录，已中止清理",
                "job_ids": sorted(existing),
            },
        )

    safe_roots = safe_roots or DEFAULT_SAFE_PROJECT_ROOTS
    paths_by_job = _find_job_dirs_for_ids(root, set(unique_ids))
    items: list[dict] = []
    freed = 0
    for job_id in unique_ids:
        paths = paths_by_job.get(job_id) or []
        if not paths:
            items.append({"job_id": job_id, "status": "missing", "freed_bytes": 0})
            continue
        for path in paths:
            if not _is_safe_project_dir(path, safe_roots=safe_roots):
                raise HTTPException(
                    status_code=400,
                    detail={"message": "拒绝清理不安全路径", "path": str(path)},
                )
            size = _directory_size_bytes(path)
            if not dry_run:
                shutil.rmtree(path)
                freed += size
            items.append(
                {
                    "job_id": job_id,
                    "path": str(path),
                    "status": "would_delete" if dry_run else "deleted",
                    "freed_bytes": 0 if dry_run else size,
                    "size_bytes": size,
                    "size_gib": _bytes_to_gib(size),
                }
            )
    logger.info(
        "admin disk orphan cleanup dry_run=%s jobs=%s freed_bytes=%s",
        dry_run,
        unique_ids,
        freed,
    )
    return {
        "dry_run": dry_run,
        "freed_bytes": freed,
        "freed_gib": _bytes_to_gib(freed),
        "items": items,
    }


async def _post_resize_helper(*, dry_run: bool, confirm: bool, timeout: int) -> dict:
    token = _configured_resize_helper_token()
    if not token:
        raise HTTPException(status_code=403, detail="未配置磁盘扩容 helper token")
    try:
        async with httpx.AsyncClient(timeout=timeout + 5) as client:
            response = await client.post(
                f"{_configured_resize_helper_url()}/resize",
                headers={"Authorization": f"Bearer {token}"},
                json={"dry_run": dry_run, "confirm": confirm},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"无法连接磁盘扩容 helper: {exc}",
        ) from exc

    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(status_code=502, detail=detail or payload or "磁盘扩容 helper 执行失败")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="磁盘扩容 helper 返回格式无效")
    return payload


async def resize_filesystem(
    *,
    dry_run: bool,
    confirm: bool,
    project_root: Path | None = None,
) -> dict:
    async with _resize_lock:
        root = project_root or _resolve_scan_root()
        before = _build_resize_hint(root)
        if not before.get("feature_enabled"):
            raise HTTPException(status_code=403, detail="后台未启用一键扩展文件系统")
        if not before.get("can_resize"):
            raise HTTPException(
                status_code=409,
                detail={"message": before.get("reason") or "当前不能扩展文件系统", "resize_hint": before},
            )
        if dry_run:
            return {"dry_run": True, "ran": False, "before": before, "after": before, "output": ""}
        if not confirm:
            raise HTTPException(status_code=400, detail="需要 confirm=true")

        timeout = _configured_resize_timeout()
        helper_result = await _post_resize_helper(
            dry_run=False,
            confirm=True,
            timeout=timeout,
        )
        after = _build_resize_hint(root)
        logger.info(
            "admin disk resize helper ran device=%s before_device=%s before_fs=%s after_device=%s after_fs=%s",
            before.get("device"),
            before.get("device_bytes"),
            before.get("filesystem_bytes"),
            after.get("device_bytes"),
            after.get("filesystem_bytes"),
        )
        return {
            **helper_result,
            "before": before,
            "after": after,
        }


@router.get("/overview")
async def get_disk_overview(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(user)
    return await build_disk_overview(db)


@router.post("/cleanup-orphans")
async def post_cleanup_orphans(
    body: OrphanCleanupRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(user)
    return await cleanup_orphan_dirs(
        db,
        job_ids=body.job_ids,
        dry_run=body.dry_run,
    )


@router.post("/cleanup-expired")
async def post_cleanup_expired(
    body: ExpiredCleanupRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(user)
    purged = await cleanup_expired_projects(db, dry_run=body.dry_run)
    return {"dry_run": body.dry_run, "purged_count": purged}


@router.post("/resize-filesystem")
async def post_resize_filesystem(
    body: ResizeFilesystemRequest,
    user: User | None = Depends(get_current_user),
) -> dict:
    _require_admin(user)
    return await resize_filesystem(dry_run=body.dry_run, confirm=body.confirm)

"""Fail-safe rotating file log handler attachment.

Usage (job-api entry point, or any Python process that wants on-disk logs):

    from utils.rotating_log import attach_rotating_file_log
    attach_rotating_file_log("jobapi.app.log")

The helper reads AIVIDEOTRANS_RUNTIME_LOGS_DIR from the environment (default
``/opt/aivideotrans/data/runtime_logs``) and attaches a RotatingFileHandler to
the root logger.  Every failure is handled gracefully — the caller's process
must never crash because the log directory is missing or read-only (common on
Windows local dev).
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR_ENV = "AIVIDEOTRANS_RUNTIME_LOGS_DIR"
_LOG_DIR_DEFAULT = "/opt/aivideotrans/data/runtime_logs"
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def attach_rotating_file_log(filename: str) -> None:
    """Attach a RotatingFileHandler for *filename* to the root logger.

    Completely fail-safe: any OS / permission error is printed to stderr and
    swallowed so the calling process continues normally.

    Args:
        filename: Bare filename (no path) for the log file, e.g. ``jobapi.app.log``.
    """
    log_dir_str = os.environ.get(_LOG_DIR_ENV, _LOG_DIR_DEFAULT)
    try:
        log_dir = Path(log_dir_str)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / filename
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        logging.getLogger().addHandler(handler)
    except Exception as exc:  # noqa: BLE001
        # Never crash the process — just warn on stderr.
        print(
            f"[rotating_log] WARNING: could not attach rotating file handler "
            f"(dir={log_dir_str!r}, file={filename!r}): {exc}",
            flush=True,
        )

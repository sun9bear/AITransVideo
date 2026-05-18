"""PanProvider Protocol — structural typing for future multi-provider support.

MVP 只有 BaiduPanClient 实现这个,但写 Protocol 让 backup_executor 不 hard-bind
百度。未来加 OneDrive / 阿里云盘按这个协议补。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class PanProvider(Protocol):
    """Protocol all pan provider clients must satisfy."""

    def upload(self, local_path: Path, remote_path: str, *, access_token: str) -> dict:
        """Upload local file. Returns dict with at minimum:
            { 'size': int, 'md5': str, 'fs_id': str }
        Raises on failure.
        """
        ...

    def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
        """Download remote file to local_path. Returns:
            { 'size': int, 'md5': str (server-reported), 'sha256': str (locally computed) }
        Raises on failure.
        """
        ...

    def list(self, prefix: str, *, access_token: str) -> list[dict]:
        """List files under prefix. Each entry has 'path', 'size', 'fs_id' at minimum."""
        ...

    def delete(self, remote_path: str, *, access_token: str) -> None:
        """Delete remote file. Idempotent: deleting non-existent file = no-op."""
        ...

    def get_quota(self, *, access_token: str) -> dict:
        """Return { 'total': int, 'used': int, 'free': int } in bytes."""
        ...

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange OAuth authorization code for tokens.
        Returns: { 'access_token': str, 'refresh_token': str, 'expires_in': int, 'scope': str }
        """
        ...

    def refresh(self, refresh_token: str) -> dict:
        """Use refresh_token to get new tokens. Returns same shape as exchange_code.
        ⚠️ Baidu rotates refresh_token on every call — caller MUST persist the new
        refresh_token from the response.
        """
        ...

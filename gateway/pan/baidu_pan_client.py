"""Baidu Pan OpenAPI client.

Plan 2026-05-13 §3.1 + §7 + §9. 使用 requests library (sync) — backup
executor 本身在 background_task 里跑,不阻塞 event loop。

API base: https://openapi.baidu.com/oauth/2.0/
Pan API base: https://pan.baidu.com/rest/2.0/xpan/

Reference: https://pan.baidu.com/union/document
"""
from __future__ import annotations

import hashlib
import json as _json
from pathlib import Path
from typing import Iterator

import requests


class BaiduPanClient:
    """Implements PanProvider protocol for Baidu Pan."""

    OAUTH_BASE = "https://openapi.baidu.com/oauth/2.0"
    XPAN_BASE = "https://pan.baidu.com/rest/2.0/xpan"
    PCS_BASE = "https://d.pcs.baidu.com/rest/2.0/pcs"

    def __init__(self, appkey: str, appsecret: str):
        if not appkey or not appsecret:
            raise ValueError("Baidu Pan client requires appkey + appsecret")
        self.appkey = appkey
        self.appsecret = appsecret

    # --- T3.6: chunked upload internals ---
    def _chunk_file(self, path: Path, chunk_bytes: int) -> Iterator[tuple[int, bytes]]:
        """Yield (index, chunk_bytes_blob) pairs."""
        with path.open('rb') as f:
            idx = 0
            while True:
                chunk = f.read(chunk_bytes)
                if not chunk:
                    break
                yield idx, chunk
                idx += 1

    def _compute_chunk_md5s(self, path: Path, chunk_bytes: int) -> tuple[list[str], str]:
        """Returns (per-chunk md5s, file-level md5). Walks file twice for clarity."""
        chunk_md5s = []
        for _, chunk in self._chunk_file(path, chunk_bytes):
            chunk_md5s.append(hashlib.md5(chunk).hexdigest())

        file_md5 = hashlib.md5()
        with path.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                file_md5.update(chunk)
        return chunk_md5s, file_md5.hexdigest()

    def _precreate(self, remote_path: str, size: int, chunk_md5s: list[str], access_token: str) -> str:
        """Declare upload intent. Returns uploadid."""
        resp = requests.post(
            f"{self.XPAN_BASE}/file",
            params={'method': 'precreate', 'access_token': access_token},
            data={
                'path': remote_path,
                'size': size,
                'isdir': 0,
                'autoinit': 1,
                'block_list': _json.dumps(chunk_md5s),
                'rtype': 3,  # 3 = 覆盖同名
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get('errno', 0) != 0:
            raise RuntimeError(f"Baidu precreate failed: {body}")
        return body['uploadid']

    def _upload_chunk(self, path: Path, chunk_idx: int, chunk_data: bytes,
                      remote_path: str, uploadid: str, access_token: str) -> None:
        """PUT one 4MB chunk via superfile2.

        `path` retained in signature for future use (e.g. retry-by-offset reads);
        currently unused — chunk bytes are passed directly to avoid double-reading.
        """
        del path  # silence linters
        resp = requests.post(
            f"{self.PCS_BASE}/superfile2",
            params={
                'method': 'upload',
                'access_token': access_token,
                'type': 'tmpfile',
                'path': remote_path,
                'uploadid': uploadid,
                'partseq': chunk_idx,
            },
            files={'file': chunk_data},
            timeout=300,  # 大 chunk 跨境慢
        )
        resp.raise_for_status()
        body = resp.json()
        if 'md5' not in body:
            raise RuntimeError(f"Baidu chunk PUT failed (no md5 returned): {body}")

    def _create_finalize(self, remote_path: str, size: int, chunk_md5s: list[str],
                         uploadid: str, access_token: str) -> dict:
        """Finalize the multipart upload, returns server-final {fs_id, size, md5}."""
        resp = requests.post(
            f"{self.XPAN_BASE}/file",
            params={'method': 'create', 'access_token': access_token},
            data={
                'path': remote_path,
                'size': size,
                'isdir': 0,
                'uploadid': uploadid,
                'block_list': _json.dumps(chunk_md5s),
                'rtype': 3,
            },
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get('errno', 0) != 0:
            raise RuntimeError(f"Baidu finalize failed: {body}")
        return {'fs_id': body['fs_id'], 'size': body['size'], 'md5': body['md5']}

    def upload(self, local_path: Path, remote_path: str, *, access_token: str) -> dict:
        """Full upload flow: precreate → chunked PUT → finalize.

        Plan §7 steps g-h. Returns server-confirmed dict for caller to compare.
        """
        from config import settings
        chunk_bytes = settings.pan_upload_chunk_bytes

        size = local_path.stat().st_size
        chunk_md5s, _file_md5 = self._compute_chunk_md5s(local_path, chunk_bytes)

        uploadid = self._precreate(remote_path, size, chunk_md5s, access_token)
        for idx, chunk in self._chunk_file(local_path, chunk_bytes):
            self._upload_chunk(local_path, idx, chunk, remote_path, uploadid, access_token)

        return self._create_finalize(remote_path, size, chunk_md5s, uploadid, access_token)

    def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
        raise NotImplementedError("T3.8")

    def list(self, prefix: str, *, access_token: str) -> list[dict]:
        """List files under prefix. Pagination not supported in MVP — assume single page."""
        resp = requests.get(
            f"{self.XPAN_BASE}/file",
            params={
                'method': 'list',
                'access_token': access_token,
                'dir': prefix,
                'limit': 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get('errno', 0) != 0:
            raise RuntimeError(f"Baidu list failed: {body}")
        return [
            {'path': item['path'], 'size': item['size'], 'fs_id': item['fs_id']}
            for item in body.get('list', [])
            if not item.get('isdir')
        ]

    def delete(self, remote_path: str, *, access_token: str) -> None:
        """Delete a single file. Idempotent: 404-like errno -9 = no-op success."""
        resp = requests.post(
            f"{self.XPAN_BASE}/file",
            params={'method': 'filemanager', 'access_token': access_token, 'opera': 'delete'},
            data={'async': 0, 'filelist': _json.dumps([remote_path])},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get('errno', 0) not in (0, -9):
            raise RuntimeError(f"Baidu delete failed: {body}")

    def get_quota(self, *, access_token: str) -> dict:
        resp = requests.get(
            'https://pan.baidu.com/api/quota',
            params={'access_token': access_token, 'checkfree': 1, 'checkexpire': 1},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        total = body.get('total', 0)
        used = body.get('used', 0)
        return {'total': total, 'used': used, 'free': total - used}

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange OAuth code for tokens (one-shot, code expires fast).

        Plan §9.3. Baidu doc: pan.baidu.com/union/doc/Fl1d4dx7t
        """
        resp = requests.post(
            f"{self.OAUTH_BASE}/token",
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': self.appkey,
                'client_secret': self.appsecret,
                'redirect_uri': redirect_uri,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        if 'error' in body:
            raise RuntimeError(f"Baidu OAuth code exchange failed: {body}")
        # Baidu returns scope as space-separated string
        return {
            'access_token': body['access_token'],
            'refresh_token': body['refresh_token'],
            'expires_in': body['expires_in'],
            'scope': body.get('scope', ''),
        }

    def refresh(self, refresh_token: str) -> dict:
        """Refresh access_token. Baidu **rotates refresh_token on every call**;
        caller MUST persist the new refresh_token from response.

        Plan §9 step 3-4.
        """
        resp = requests.post(
            f"{self.OAUTH_BASE}/token",
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': self.appkey,
                'client_secret': self.appsecret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        if 'error' in body:
            raise RuntimeError(f"Baidu OAuth refresh failed: {body}")
        return {
            'access_token': body['access_token'],
            'refresh_token': body['refresh_token'],
            'expires_in': body['expires_in'],
            'scope': body.get('scope', ''),
        }

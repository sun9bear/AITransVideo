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
import logging
import time
from pathlib import Path
from typing import Iterator

import requests

logger = logging.getLogger(__name__)

# Production 2026-05-20: cross-border (US gateway → Baidu PCS) 4MB chunk
# uploads occasionally hit urllib3's default 30s read-timeout when the
# Baidu CDN edge is congested or briefly throttles. Single attempt fails
# the whole backup. Retry on transient network failures (Timeout +
# ConnectionError + 5xx) with exponential backoff. HTTP 4xx is auth /
# permission and NOT retried.
_CHUNK_UPLOAD_MAX_ATTEMPTS = 3
_CHUNK_UPLOAD_BACKOFF_BASE_S = 1.0  # → 1s, 2s, 4s


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
        """PUT one 4MB chunk via superfile2 with retry on transient failures.

        `path` retained in signature for future use (e.g. retry-by-offset reads);
        currently unused — chunk bytes are passed directly to avoid double-reading.

        Production 2026-05-20: cross-border to Baidu PCS occasionally fails
        with ``read timeout=30`` (urllib3 default) when the CDN edge is
        congested. Each chunk now retries up to 3 times with exponential
        backoff (1s, 2s, 4s). Idempotency note: per Baidu Pan docs,
        re-uploading the same (uploadid, partseq) with identical content
        is a no-op on the server side — the partseq slot just keeps the
        last successfully received bytes.

        Retried exceptions:
            requests.Timeout       — read timeout / connect timeout
            requests.ConnectionError — TCP reset, DNS hiccup, etc.
            5xx HTTPError          — server-side transient
        NOT retried:
            4xx HTTPError          — auth (401/403) / bad request / quota (429)
            RuntimeError           — Baidu errno!=0 (business-level reject)
        """
        del path  # silence linters
        last_exc: Exception | None = None
        for attempt in range(_CHUNK_UPLOAD_MAX_ATTEMPTS):
            try:
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
                    # Tuple form is explicit: (connect, read). 30s to handshake,
                    # 300s for the whole-chunk read (large chunks on slow links
                    # can take a couple of minutes). Same total as before, but
                    # we now retry instead of giving up on the first hiccup.
                    timeout=(30, 300),
                )
                # 5xx → retry; 4xx → raise (auth / bad request, no point retrying)
                if 500 <= resp.status_code < 600:
                    raise requests.HTTPError(
                        f"PCS chunk upload returned 5xx {resp.status_code}",
                        response=resp,
                    )
                resp.raise_for_status()
                body = resp.json()
                if 'md5' not in body:
                    raise RuntimeError(
                        f"Baidu chunk PUT failed (no md5 returned): {body}"
                    )
                if attempt > 0:
                    logger.info(
                        "PCS chunk upload succeeded on attempt %d/%d "
                        "(partseq=%d uploadid=%s)",
                        attempt + 1, _CHUNK_UPLOAD_MAX_ATTEMPTS,
                        chunk_idx, uploadid,
                    )
                return
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                if attempt + 1 >= _CHUNK_UPLOAD_MAX_ATTEMPTS:
                    break
                sleep_s = _CHUNK_UPLOAD_BACKOFF_BASE_S * (2 ** attempt)
                logger.warning(
                    "PCS chunk upload transient failure (partseq=%d "
                    "attempt=%d/%d): %s — retrying in %.1fs",
                    chunk_idx, attempt + 1, _CHUNK_UPLOAD_MAX_ATTEMPTS,
                    exc, sleep_s,
                )
                time.sleep(sleep_s)
            except requests.HTTPError as exc:
                # 5xx caught above; 4xx fall here → raise immediately.
                if (exc.response is not None
                        and 500 <= exc.response.status_code < 600
                        and attempt + 1 < _CHUNK_UPLOAD_MAX_ATTEMPTS):
                    last_exc = exc
                    sleep_s = _CHUNK_UPLOAD_BACKOFF_BASE_S * (2 ** attempt)
                    logger.warning(
                        "PCS chunk upload 5xx (partseq=%d attempt=%d/%d): "
                        "%s — retrying in %.1fs",
                        chunk_idx, attempt + 1, _CHUNK_UPLOAD_MAX_ATTEMPTS,
                        exc, sleep_s,
                    )
                    time.sleep(sleep_s)
                    continue
                raise
        # Exhausted retries — raise the last network exception so backup_executor
        # can mark the BackupRecord failed with the actual cause.
        assert last_exc is not None  # we only break out of loop after setting it
        raise last_exc

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

    # --- T3.7: read-back probe (3rd gate per design §7) ---
    def _get_dlink(self, remote_path: str, access_token: str) -> str:
        """Get a time-limited download link for a single remote file.

        Chains: list(parent_dir) → match path → fs_id → filemetas(fsids=[fs_id])
        → dlink. Baidu's /multimedia?method=filemetas API requires fs_id (path
        is NOT a valid lookup key on that endpoint), so we discover fs_id
        from a list() call against the file's parent directory.

        Raises RuntimeError when the file is missing from the parent listing
        or when filemetas returns no items.
        """
        # Compute parent dir. "/apps/X/job.tar.gz" -> "/apps/X/"
        # Root-level file "/job.tar.gz" -> "/"
        head, _, _tail = remote_path.rpartition('/')
        parent = head if head else '/'
        if not parent.endswith('/'):
            parent = parent + '/'

        entries = self.list(parent, access_token=access_token)
        matched = next((e for e in entries if e.get('path') == remote_path), None)
        if matched is None:
            raise RuntimeError(
                f"Remote file not found in listing of {parent}: {remote_path}"
            )
        fs_id = matched['fs_id']

        resp = requests.get(
            f"{self.XPAN_BASE}/multimedia",
            params={
                'method': 'filemetas',
                'access_token': access_token,
                'fsids': _json.dumps([fs_id]),
                'dlink': 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        items = body.get('list', [])
        if not items:
            raise RuntimeError(
                f"No metadata returned for fs_id={fs_id} (path={remote_path})"
            )
        return items[0]['dlink'] + f'&access_token={access_token}'

    def verify_remote_tail(self, local_path: Path, remote_path: str, size: int, *,
                           access_token: str, probe_bytes: int = 64 * 1024) -> bool:
        """Read-back probe: pull last `probe_bytes` of remote file and compare
        with local file's tail. Used as 3rd gate in §7 step h.

        Returns True if matched, False otherwise. Caller decides whether to
        raise or fall back.
        """
        if size < probe_bytes:
            probe_bytes = size  # smaller files probe entirety

        # local tail
        with local_path.open('rb') as f:
            f.seek(-probe_bytes, 2)  # 2 = end
            local_tail = f.read(probe_bytes)

        # remote tail via Range
        range_header = {'Range': f'bytes={size - probe_bytes}-{size - 1}'}
        # download link for the remote file
        dlink = self._get_dlink(remote_path, access_token)
        resp = requests.get(dlink, headers=range_header, timeout=30)
        resp.raise_for_status()
        return resp.content == local_tail

    def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
        """Stream download to local_path. Computes sha256 + size locally.

        Returns {size, sha256, md5} — md5 left empty (server doesn't include
        it in the streamed body); caller verifies against the value stored
        in BackupRecord at upload time.
        """
        dlink = self._get_dlink(remote_path, access_token)

        sha = hashlib.sha256()
        size = 0
        with requests.get(dlink, stream=True, timeout=300) as r:
            r.raise_for_status()
            with local_path.open('wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)

        return {'size': size, 'sha256': sha.hexdigest(), 'md5': ''}  # md5 由 caller 验

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
        """Delete a single file. Idempotent: errno -9 (file not found) at
        either top level OR per-file `info[]` → no-op success.

        Baidu filemanager has two-layer error reporting:
          - top-level errno: API-level (auth, malformed request, etc.)
          - info[].errno: per-file (file not found, locked, permission, etc.)
        Both must be checked: top-level=0 with info[0].errno=-7 means the
        file was NOT deleted, but a top-level-only check would think it was.
        That would leave orphan files in user pan while DB marks "deleted".
        """
        resp = requests.post(
            f"{self.XPAN_BASE}/file",
            params={'method': 'filemanager', 'access_token': access_token, 'opera': 'delete'},
            data={'async': 0, 'filelist': _json.dumps([remote_path])},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        top_errno = body.get('errno', 0)
        if top_errno not in (0, -9):
            raise RuntimeError(f"Baidu delete failed (top errno={top_errno}): {body}")
        # Per-file info[] check — Baidu may return top=0 with per-file failure.
        for entry in (body.get('info') or []):
            per_errno = entry.get('errno', 0)
            if per_errno not in (0, -9):
                raise RuntimeError(
                    f"Baidu delete failed (per-file errno={per_errno}): {body}"
                )

    def get_quota(self, *, access_token: str) -> dict:
        """Return {'total', 'used', 'free'} in bytes.

        Body-level errno non-zero → raise (e.g. errno=2 = invalid token).
        Without this check, an auth failure would silently return 0/0/0,
        looking like an empty account instead of an auth error.
        """
        resp = requests.get(
            'https://pan.baidu.com/api/quota',
            params={'access_token': access_token, 'checkfree': 1, 'checkexpire': 1},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        errno = body.get('errno', 0)
        if errno != 0:
            raise RuntimeError(f"Baidu get_quota failed (errno={errno}): {body}")
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

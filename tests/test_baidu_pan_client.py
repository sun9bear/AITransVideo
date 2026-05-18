"""Tests for gateway.pan.baidu_pan_client.BaiduPanClient.

Plan 2026-05-14 Phase 3. All tests mock requests — no real Baidu calls.
"""
from __future__ import annotations

import json as _json

import pytest


# --- T3.1: skeleton + Protocol conformance ---


def test_client_instantiates_with_settings():
    from gateway.pan.baidu_pan_client import BaiduPanClient
    c = BaiduPanClient(appkey='test_appkey', appsecret='test_appsecret')
    assert c.appkey == 'test_appkey'
    assert c.appsecret == 'test_appsecret'


def test_client_conforms_to_pan_provider_protocol():
    from gateway.pan.provider_protocol import PanProvider
    from gateway.pan.baidu_pan_client import BaiduPanClient
    c = BaiduPanClient(appkey='x', appsecret='x')
    # Protocol structural typing
    assert isinstance(c, PanProvider)


def test_client_rejects_empty_credentials():
    from gateway.pan.baidu_pan_client import BaiduPanClient
    with pytest.raises(ValueError):
        BaiduPanClient(appkey='', appsecret='x')
    with pytest.raises(ValueError):
        BaiduPanClient(appkey='x', appsecret='')


# --- T3.2: exchange_code ---


def test_exchange_code_happy_path(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    calls = []

    def mock_post(url, data=None, **kw):
        calls.append((url, data))

        class R:
            def __init__(self, body):
                self._body = body
                self.status_code = 200

            def json(self):
                return self._body

            def raise_for_status(self):
                pass

        return R({
            'access_token': 'access_xyz',
            'refresh_token': 'refresh_xyz',
            'expires_in': 2592000,
            'scope': 'basic netdisk',
        })

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.exchange_code(code='abc123', redirect_uri='https://aitrans.video/cb')
    assert result['access_token'] == 'access_xyz'
    assert result['refresh_token'] == 'refresh_xyz'
    assert result['expires_in'] == 2592000

    # 验证请求参数
    url, data = calls[0]
    assert 'oauth/2.0/token' in url
    assert data['grant_type'] == 'authorization_code'
    assert data['code'] == 'abc123'
    assert data['client_id'] == 'ak'
    assert data['client_secret'] == 'as'
    assert data['redirect_uri'] == 'https://aitrans.video/cb'


def test_exchange_code_invalid_code_raises(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, data=None, **kw):
        class R:
            status_code = 400

            def json(self):
                return {'error': 'invalid_grant', 'error_description': 'bad code'}

            def raise_for_status(self):
                from requests import HTTPError
                raise HTTPError('400')

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(Exception, match='invalid_grant|bad code|400'):
        c.exchange_code(code='bad', redirect_uri='https://aitrans.video/cb')


def test_exchange_code_error_in_body_raises(monkeypatch):
    """If Baidu returns 200 but body has 'error' field, treat as failure."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, data=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'error': 'invalid_client', 'error_description': 'bad appkey'}

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='invalid_client|Baidu OAuth code exchange'):
        c.exchange_code(code='x', redirect_uri='https://x.example/cb')


# --- T3.3: refresh ---


def test_refresh_returns_new_tokens(monkeypatch):
    """Baidu rotates refresh_token; caller must persist the new one."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, data=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {
                    'access_token': 'NEW_access',
                    'refresh_token': 'NEW_refresh',  # 注意:跟旧的不同
                    'expires_in': 2592000,
                    'scope': 'basic netdisk',
                }

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.refresh(refresh_token='OLD_refresh')
    assert result['access_token'] == 'NEW_access'
    assert result['refresh_token'] == 'NEW_refresh'  # 新的,必须 persist


def test_refresh_sends_grant_type_refresh_token(monkeypatch):
    """Sanity: verify the POST body has correct grant_type + secret."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    calls = []

    def mock_post(url, data=None, **kw):
        calls.append((url, data))

        class R:
            status_code = 200

            def json(self):
                return {
                    'access_token': 'a',
                    'refresh_token': 'r',
                    'expires_in': 1,
                    'scope': '',
                }

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    c.refresh(refresh_token='RT_old')

    url, data = calls[0]
    assert 'oauth/2.0/token' in url
    assert data['grant_type'] == 'refresh_token'
    assert data['refresh_token'] == 'RT_old'
    assert data['client_id'] == 'ak'
    assert data['client_secret'] == 'as'


def test_refresh_error_in_body_raises(monkeypatch):
    """Body-level error → RuntimeError, even on HTTP 200."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, data=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'error': 'expired_token', 'error_description': 'refresh expired'}

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='expired_token|Baidu OAuth refresh'):
        c.refresh(refresh_token='RT')


# --- T3.4: list + get_quota ---


def test_list_files_under_prefix(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {
                    'errno': 0,
                    'list': [
                        {'path': '/apps/AIVideoTrans/backups/job_a.tar.gz', 'size': 1000, 'fs_id': 1, 'isdir': 0},
                        {'path': '/apps/AIVideoTrans/backups/job_b.tar.gz', 'size': 2000, 'fs_id': 2, 'isdir': 0},
                    ],
                }

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    files = c.list('/apps/AIVideoTrans/backups/', access_token='at_xyz')
    assert len(files) == 2
    assert files[0]['path'] == '/apps/AIVideoTrans/backups/job_a.tar.gz'
    assert files[0]['size'] == 1000


def test_list_filters_directories(monkeypatch):
    """list should skip isdir=1 entries."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {
                    'errno': 0,
                    'list': [
                        {'path': '/apps/AIVideoTrans/backups/file.tar.gz', 'size': 1, 'fs_id': 1, 'isdir': 0},
                        {'path': '/apps/AIVideoTrans/backups/subdir', 'size': 0, 'fs_id': 2, 'isdir': 1},
                    ],
                }

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    files = c.list('/apps/AIVideoTrans/backups/', access_token='at')
    assert len(files) == 1
    assert files[0]['path'].endswith('file.tar.gz')


def test_list_raises_on_errno(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'errno': -7, 'errmsg': 'invalid path'}

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='Baidu list failed|-7'):
        c.list('/bogus', access_token='at')


def test_get_quota(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'total': 2 * 10**12, 'used': 500 * 10**9}

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    q = c.get_quota(access_token='at_xyz')
    assert q['total'] == 2 * 10**12
    assert q['used'] == 500 * 10**9
    assert q['free'] == q['total'] - q['used']


def test_get_quota_default_zero_when_missing(monkeypatch):
    """If Baidu omits total/used, default to zero (free=0). Avoid KeyError crash."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {}

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    q = c.get_quota(access_token='at')
    assert q == {'total': 0, 'used': 0, 'free': 0}


# --- T3.5: delete (idempotent on errno -9) ---


def test_delete_calls_filemanager_delete(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    calls = []

    def mock_post(url, params=None, data=None, **kw):
        calls.append((url, params, data))

        class R:
            status_code = 200

            def json(self):
                return {'errno': 0}

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    c.delete('/apps/AIVideoTrans/backups/job_x.tar.gz', access_token='at')
    url, params, data = calls[0]
    assert 'filemanager' in str(params)
    assert params['opera'] == 'delete'
    assert '/apps/AIVideoTrans/backups/job_x.tar.gz' in data['filelist']


def test_delete_idempotent_on_404(monkeypatch):
    """Deleting already-gone file (errno -9) should not raise."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, params=None, data=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'errno': -9, 'info': [{'errno': -9}]}  # file not found

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    # 不抛
    c.delete('/apps/AIVideoTrans/backups/missing.tar.gz', access_token='at')


def test_delete_raises_on_other_errno(monkeypatch):
    """Non-zero non-(-9) errno → RuntimeError."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, params=None, data=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'errno': -7, 'errmsg': 'invalid filelist'}

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='Baidu delete failed|-7'):
        c.delete('/bad', access_token='at')


def test_delete_raises_on_per_file_errno(monkeypatch):
    """top errno=0 but info[0].errno=-7 must still raise — would otherwise
    leave orphan remote files while DB marks deleted (CodeX P1)."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, params=None, data=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {
                    'errno': 0,  # 顶层报"全成功"
                    'info': [{
                        'path': '/apps/AIVideoTrans/backups/locked.tar.gz',
                        'errno': -7,  # 单文件失败:权限/路径问题
                    }],
                }

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='per-file errno|=-7'):
        c.delete('/apps/AIVideoTrans/backups/locked.tar.gz', access_token='at')


def test_delete_idempotent_on_per_file_minus_9(monkeypatch):
    """top errno=0 + info[0].errno=-9 (file not found) is also idempotent success."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, params=None, data=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {
                    'errno': 0,
                    'info': [{'path': '/x.tar.gz', 'errno': -9}],
                }

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    # 不抛
    c.delete('/x.tar.gz', access_token='at')


# --- T3.6: chunked upload ---


def _make_upload_mocker(monkeypatch, expected_size: int):
    """Helper: install requests.post mock that handles precreate / chunk / finalize.

    Returns (requests_made list, restore-noop). Reuses mock pattern from plan T3.6.
    """
    import requests

    requests_made = []

    def mock_post(url, **kw):
        params = kw.get('params') or {}
        method = params.get('method', '')
        requests_made.append({
            'url': url,
            'params': params,
            'data': kw.get('data'),
            'files': kw.get('files'),
        })

        class R:
            status_code = 200

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

            def raise_for_status(self):
                pass

        if method == 'precreate':
            return R({'errno': 0, 'uploadid': 'upload_abc'})
        if 'pcs.baidu.com' in url:
            return R({'errno': 0, 'md5': f"chunk_md5_{params.get('partseq', 0)}"})
        if method == 'create':
            return R({
                'errno': 0,
                'fs_id': 12345,
                'size': expected_size,
                'md5': 'final_full_md5',
            })
        return R({'errno': 0})

    monkeypatch.setattr(requests, 'post', mock_post)
    return requests_made


def test_upload_full_flow_5mb_two_chunks(monkeypatch, tmp_path):
    """5MB file with 4MB chunk size → 2 chunks (4MB + 1MB) + precreate + finalize."""
    from gateway.pan.baidu_pan_client import BaiduPanClient

    test_file = tmp_path / 'test.tar.gz'
    test_file.write_bytes(b'A' * (5 * 1024 * 1024))

    requests_made = _make_upload_mocker(monkeypatch, expected_size=5 * 1024 * 1024)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.upload(test_file, '/apps/AIVideoTrans/backups/test.tar.gz', access_token='at')

    assert result['size'] == 5 * 1024 * 1024
    assert result['md5'] == 'final_full_md5'
    assert result['fs_id'] == 12345

    # Verify three-phase orchestration: precreate + N chunks + finalize.
    precreate_calls = [r for r in requests_made if (r['params'].get('method') == 'precreate')]
    chunk_calls = [r for r in requests_made if 'pcs.baidu.com' in r['url']]
    finalize_calls = [r for r in requests_made if (r['params'].get('method') == 'create')]

    assert len(precreate_calls) == 1
    assert len(chunk_calls) == 2  # 5MB / 4MB chunks = 2
    assert len(finalize_calls) == 1

    # block_list (per-chunk md5s) must be propagated to precreate AND finalize.
    pre_block = _json.loads(precreate_calls[0]['data']['block_list'])
    fin_block = _json.loads(finalize_calls[0]['data']['block_list'])
    assert pre_block == fin_block
    assert len(pre_block) == 2  # 2 md5s, one per chunk
    # Each md5 is a 32-hex-char string.
    for md5 in pre_block:
        assert len(md5) == 32

    # partseq must be sequential 0, 1.
    partseqs = sorted([r['params']['partseq'] for r in chunk_calls])
    assert partseqs == [0, 1]


def test_upload_single_chunk_for_small_file(monkeypatch, tmp_path):
    """File smaller than chunk size → 1 chunk."""
    from gateway.pan.baidu_pan_client import BaiduPanClient

    test_file = tmp_path / 'small.tar.gz'
    test_file.write_bytes(b'X' * (100 * 1024))  # 100 KB

    requests_made = _make_upload_mocker(monkeypatch, expected_size=100 * 1024)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.upload(test_file, '/apps/AIVideoTrans/small.tar.gz', access_token='at')
    assert result['size'] == 100 * 1024

    chunk_calls = [r for r in requests_made if 'pcs.baidu.com' in r['url']]
    assert len(chunk_calls) == 1


def test_upload_precreate_failure_propagates(monkeypatch, tmp_path):
    """If precreate returns errno != 0 → RuntimeError, no chunk uploads attempted."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    chunk_attempts = []

    def mock_post(url, **kw):
        params = kw.get('params') or {}
        if params.get('method') == 'precreate':
            class R:
                status_code = 200

                def json(self):
                    return {'errno': 31203, 'errmsg': 'quota exhausted'}

                def raise_for_status(self):
                    pass
            return R()
        if 'pcs.baidu.com' in url:
            chunk_attempts.append(params.get('partseq'))

        class Empty:
            status_code = 200

            def json(self):
                return {'errno': 0}

            def raise_for_status(self):
                pass
        return Empty()

    monkeypatch.setattr(requests, 'post', mock_post)
    test_file = tmp_path / 'f.tar.gz'
    test_file.write_bytes(b'X' * 1024)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='Baidu precreate|31203|quota'):
        c.upload(test_file, '/x.tar.gz', access_token='at')
    # 关键:precreate 失败后没尝试上传 chunk
    assert chunk_attempts == []


def test_upload_chunk_missing_md5_raises(monkeypatch, tmp_path):
    """If chunk PUT response missing 'md5', upload raises (data integrity).
    """
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, **kw):
        params = kw.get('params') or {}
        method = params.get('method', '')

        class R:
            status_code = 200

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

            def raise_for_status(self):
                pass

        if method == 'precreate':
            return R({'errno': 0, 'uploadid': 'u1'})
        if 'pcs.baidu.com' in url:
            return R({'errno': 0})  # missing md5
        return R({'errno': 0, 'fs_id': 1, 'size': 0, 'md5': ''})

    monkeypatch.setattr(requests, 'post', mock_post)
    test_file = tmp_path / 'f.tar.gz'
    test_file.write_bytes(b'X' * 1024)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='chunk PUT failed|no md5'):
        c.upload(test_file, '/x.tar.gz', access_token='at')


def test_upload_finalize_failure_raises(monkeypatch, tmp_path):
    """Finalize errno != 0 → RuntimeError."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, **kw):
        params = kw.get('params') or {}
        method = params.get('method', '')

        class R:
            status_code = 200

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

            def raise_for_status(self):
                pass

        if method == 'precreate':
            return R({'errno': 0, 'uploadid': 'u1'})
        if 'pcs.baidu.com' in url:
            return R({'errno': 0, 'md5': 'cm'})
        if method == 'create':
            return R({'errno': 31363, 'errmsg': 'block list inconsistent'})
        return R({'errno': 0})

    monkeypatch.setattr(requests, 'post', mock_post)
    test_file = tmp_path / 'f.tar.gz'
    test_file.write_bytes(b'X' * 1024)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='Baidu finalize|31363'):
        c.upload(test_file, '/x.tar.gz', access_token='at')


def test_compute_chunk_md5s_deterministic(tmp_path):
    """Sanity: md5 list + file md5 are computed correctly for known input."""
    from gateway.pan.baidu_pan_client import BaiduPanClient

    test_file = tmp_path / 'known.bin'
    payload = b'A' * (4 * 1024 * 1024) + b'B' * 1024
    test_file.write_bytes(payload)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    chunk_md5s, file_md5 = c._compute_chunk_md5s(test_file, 4 * 1024 * 1024)

    import hashlib as _h
    expected_chunk0 = _h.md5(b'A' * (4 * 1024 * 1024)).hexdigest()
    expected_chunk1 = _h.md5(b'B' * 1024).hexdigest()
    expected_file = _h.md5(payload).hexdigest()

    assert chunk_md5s == [expected_chunk0, expected_chunk1]
    assert file_md5 == expected_file


# --- T3.7: read-back probe ---


def _make_dlink_mocker(monkeypatch, *, tail_bytes: bytes, status: int = 206):
    """Helper: mock both /multimedia (returns dlink) and the dlink GET (returns tail bytes)."""
    import requests

    requests_made = []

    def mock_get(url, params=None, headers=None, **kw):
        requests_made.append({'url': url, 'params': params, 'headers': headers})

        # /xpan/multimedia returns JSON with dlink
        if 'multimedia' in url:
            class JR:
                status_code = 200

                def json(self):
                    return {'list': [{'dlink': 'https://example.com/dl?token=fake'}]}

                def raise_for_status(self):
                    pass
            return JR()

        # actual dlink fetch returns tail bytes
        class BR:
            def __init__(self):
                self.status_code = status
                self.content = tail_bytes

            def raise_for_status(self):
                pass
        return BR()

    monkeypatch.setattr(requests, 'get', mock_get)
    return requests_made


def test_read_back_probe_matches_tail(monkeypatch, tmp_path):
    from gateway.pan.baidu_pan_client import BaiduPanClient

    test_file = tmp_path / 'probe.tar.gz'
    payload = b'X' * 200_000  # 200KB
    test_file.write_bytes(payload)

    # Mock returns the same tail as local (last 64KB = b'X' * 65536)
    _make_dlink_mocker(monkeypatch, tail_bytes=b'X' * 65_536)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    ok = c.verify_remote_tail(test_file, '/apps/AIVideoTrans/probe.tar.gz',
                              size=200_000, access_token='at')
    assert ok is True


def test_read_back_probe_detects_tampering(monkeypatch, tmp_path):
    """If remote tail differs from local → return False (no exception)."""
    from gateway.pan.baidu_pan_client import BaiduPanClient

    test_file = tmp_path / 'probe.tar.gz'
    test_file.write_bytes(b'X' * 200_000)

    # Mock returns 'Y' bytes — won't match local 'X' tail.
    _make_dlink_mocker(monkeypatch, tail_bytes=b'Y' * 65_536)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    ok = c.verify_remote_tail(test_file, '/apps/AIVideoTrans/probe.tar.gz',
                              size=200_000, access_token='at')
    assert ok is False


def test_read_back_probe_small_file_probes_entirety(monkeypatch, tmp_path):
    """File smaller than default probe_bytes (64KB) → probe == size."""
    from gateway.pan.baidu_pan_client import BaiduPanClient

    test_file = tmp_path / 'tiny.tar.gz'
    test_file.write_bytes(b'Z' * 1000)  # only 1KB

    requests_made = _make_dlink_mocker(monkeypatch, tail_bytes=b'Z' * 1000)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    ok = c.verify_remote_tail(test_file, '/apps/AIVideoTrans/tiny.tar.gz',
                              size=1000, access_token='at')
    assert ok is True

    # Range header should be bytes=0-999 (full file).
    dlink_calls = [r for r in requests_made if 'multimedia' not in r['url']]
    assert dlink_calls, 'expected at least one dlink GET call'
    range_header = dlink_calls[0]['headers']['Range']
    assert range_header == 'bytes=0-999'


def test_read_back_probe_uses_correct_range_for_large_file(monkeypatch, tmp_path):
    """Range header must be bytes=(size-probe)-(size-1)."""
    from gateway.pan.baidu_pan_client import BaiduPanClient

    test_file = tmp_path / 'large.tar.gz'
    size = 5_000_000
    test_file.write_bytes(b'M' * size)

    requests_made = _make_dlink_mocker(monkeypatch, tail_bytes=b'M' * 65_536)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    c.verify_remote_tail(test_file, '/x', size=size, access_token='at')

    dlink_calls = [r for r in requests_made if 'multimedia' not in r['url']]
    range_header = dlink_calls[0]['headers']['Range']
    expected_start = size - 65_536
    expected_end = size - 1
    assert range_header == f'bytes={expected_start}-{expected_end}'


def test_get_dlink_raises_when_no_metadata(monkeypatch):
    """If /multimedia returns empty list → RuntimeError."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'list': []}

            def raise_for_status(self):
                pass
        return R()

    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='No metadata returned'):
        c._get_dlink('/nonexistent.tar.gz', access_token='at')


# --- T3.8: streaming download ---


def test_download_streams_to_local(monkeypatch, tmp_path):
    """Happy path: dlink → stream chunks → write file → return size+sha256."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    dst = tmp_path / 'downloaded.tar.gz'
    test_content = b'TARGZ_CONTENT' * 1000  # 13000 bytes

    class StreamResponse:
        status_code = 200
        headers: dict = {}

        def iter_content(self, chunk_size):
            # Yield in two chunks to exercise the loop.
            half = len(test_content) // 2
            yield test_content[:half]
            yield test_content[half:]

        def raise_for_status(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def mock_get(url, params=None, headers=None, stream=False, **kw):
        if 'multimedia' in url:
            class JR:
                status_code = 200

                def json(self):
                    return {'list': [{'dlink': 'https://example.com/file?token=x'}]}

                def raise_for_status(self):
                    pass
            return JR()
        # actual dlink GET — must be the context manager response.
        return StreamResponse()

    monkeypatch.setattr(requests, 'get', mock_get)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.download('/apps/AIVideoTrans/backups/test.tar.gz', dst, access_token='at')

    assert dst.read_bytes() == test_content
    assert result['size'] == len(test_content)

    import hashlib as _h
    expected_sha = _h.sha256(test_content).hexdigest()
    assert result['sha256'] == expected_sha
    assert result['md5'] == ''  # caller responsibility per docstring


def test_download_handles_empty_chunks_in_stream(monkeypatch, tmp_path):
    """If iter_content yields empty bytes between data chunks, ignore them."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    dst = tmp_path / 'out.bin'
    payload = b'HELLO_WORLD' * 50

    class StreamResponse:
        status_code = 200
        headers: dict = {}

        def iter_content(self, chunk_size):
            yield b''
            yield payload
            yield b''

        def raise_for_status(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def mock_get(url, params=None, headers=None, stream=False, **kw):
        if 'multimedia' in url:
            class JR:
                status_code = 200

                def json(self):
                    return {'list': [{'dlink': 'https://example.com/file'}]}

                def raise_for_status(self):
                    pass
            return JR()
        return StreamResponse()

    monkeypatch.setattr(requests, 'get', mock_get)

    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.download('/x.bin', dst, access_token='at')
    assert dst.read_bytes() == payload
    assert result['size'] == len(payload)


def test_download_propagates_dlink_failure(monkeypatch, tmp_path):
    """If _get_dlink raises (empty metadata), download propagates."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200

            def json(self):
                return {'list': []}  # no items

            def raise_for_status(self):
                pass
        return R()

    monkeypatch.setattr(requests, 'get', mock_get)

    dst = tmp_path / 'never.bin'
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(RuntimeError, match='No metadata returned'):
        c.download('/gone.tar.gz', dst, access_token='at')
    assert not dst.exists()

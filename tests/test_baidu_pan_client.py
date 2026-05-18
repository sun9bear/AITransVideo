"""Tests for gateway.pan.baidu_pan_client.BaiduPanClient.

Plan 2026-05-14 Phase 3. All tests mock requests — no real Baidu calls.
"""
from __future__ import annotations

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

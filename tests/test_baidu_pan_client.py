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

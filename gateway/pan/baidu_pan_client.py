"""Baidu Pan OpenAPI client.

Plan 2026-05-13 §3.1 + §7 + §9. 使用 requests library (sync) — backup
executor 本身在 background_task 里跑,不阻塞 event loop。

API base: https://openapi.baidu.com/oauth/2.0/
Pan API base: https://pan.baidu.com/rest/2.0/xpan/

Reference: https://pan.baidu.com/union/document
"""
from __future__ import annotations

from pathlib import Path

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

    # --- placeholder methods, filled in by 后续 task ---
    def upload(self, local_path: Path, remote_path: str, *, access_token: str) -> dict:
        raise NotImplementedError("T3.6")

    def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
        raise NotImplementedError("T3.8")

    def list(self, prefix: str, *, access_token: str) -> list[dict]:
        raise NotImplementedError("T3.4")

    def delete(self, remote_path: str, *, access_token: str) -> None:
        raise NotImplementedError("T3.5")

    def get_quota(self, *, access_token: str) -> dict:
        raise NotImplementedError("T3.4")

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

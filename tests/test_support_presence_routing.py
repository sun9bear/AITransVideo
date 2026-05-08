"""Tests for the L1 in-product chat routing + WeChat QR fallback.

Plan 2026-05-08 follow-up §"管理员/运营/客服只要登录，就显示在线".

Three layers tested here without spinning up Postgres:

1. **WeChat QR file IO** — real disk via tmp_path. Validates upload
   constraints (PNG/JPEG only, ≤ 1 MB) and the delete/replace flow.
2. **Handoff routing source** — AST scan of ``support_handoff.py``
   confirms the three-branch routing (in_product / wechat_qr / email)
   landed and the function calls ``is_anyone_online`` from
   ``support_presence``.
3. **Presence service signatures** — module exposes the four functions
   the rest of the system relies on, with the right async/sync shape.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# WeChat QR file IO
# ---------------------------------------------------------------------------


@pytest.fixture
def qr_module(tmp_path, monkeypatch):
    """Import support_wechat_qr against a tmp config dir."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    import importlib
    import sys

    # Force fresh import so the module reads our tmp env.
    for mod in list(sys.modules):
        if mod.endswith("support_wechat_qr"):
            sys.modules.pop(mod, None)
    return importlib.import_module("gateway.support_wechat_qr")


# Smallest valid PNG: 8-byte signature + IHDR chunk for 1x1 image.
_TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A"
    "0000000D49484452"
    "00000001000000010806000000"
    "1F15C4890000000A49444154789C6300010000000500010D0A2DB4"
    "0000000049454E44AE426082"
)


def test_save_and_get_qr_metadata(qr_module):
    out = qr_module.save_qr(content_type="image/png", body=_TINY_PNG)
    assert out["filename"] == "support_wechat_qr.png"
    assert out["size_bytes"] == len(_TINY_PNG)
    meta = qr_module.get_qr_metadata()
    assert meta is not None
    assert meta["path"].name == "support_wechat_qr.png"
    assert qr_module.existing_qr_path() is not None


def test_save_qr_rejects_unsupported_type(qr_module):
    with pytest.raises(ValueError):
        qr_module.save_qr(content_type="image/gif", body=b"GIF89a...")
    with pytest.raises(ValueError):
        qr_module.save_qr(content_type="text/html", body=b"<html>")
    assert qr_module.get_qr_metadata() is None


def test_save_qr_rejects_oversized(qr_module):
    too_big = b"\x89PNG" + b"\x00" * (qr_module.MAX_BYTES + 1)
    with pytest.raises(ValueError):
        qr_module.save_qr(content_type="image/png", body=too_big)
    assert qr_module.get_qr_metadata() is None


def test_save_qr_rejects_empty(qr_module):
    with pytest.raises(ValueError):
        qr_module.save_qr(content_type="image/png", body=b"")


def test_save_jpg_then_png_unlinks_old_variant(qr_module):
    # First upload a JPEG.
    qr_module.save_qr(content_type="image/jpeg", body=b"\xff\xd8\xff" + b"x" * 100)
    assert qr_module.existing_qr_path().name == "support_wechat_qr.jpg"
    # Now upload a PNG — old JPEG should be unlinked.
    qr_module.save_qr(content_type="image/png", body=_TINY_PNG)
    assert qr_module.existing_qr_path().name == "support_wechat_qr.png"
    base = Path(os.environ["AIVIDEOTRANS_CONFIG_DIR"])
    assert not (base / "support_wechat_qr.jpg").exists()


def test_delete_qr(qr_module):
    qr_module.save_qr(content_type="image/png", body=_TINY_PNG)
    assert qr_module.delete_qr() is True
    assert qr_module.get_qr_metadata() is None
    # Deleting a nonexistent QR is a no-op.
    assert qr_module.delete_qr() is False


def test_public_url_carries_cache_busting_when_uploaded(qr_module):
    base = qr_module.public_url()
    assert base == "/api/support/wechat-qr"  # no QR yet
    qr_module.save_qr(content_type="image/png", body=_TINY_PNG)
    busted = qr_module.public_url()
    assert busted.startswith("/api/support/wechat-qr?v=")
    # Numeric version suffix.
    suffix = busted.split("=", 1)[1]
    assert suffix.isdigit()


# ---------------------------------------------------------------------------
# Handoff routing source — AST guard
# ---------------------------------------------------------------------------


def test_handoff_routes_to_three_branches():
    """``create_handoff`` must contain the three online-aware branches:
    ``in_product``, ``wechat_qr``, ``email``. Removing any of these is
    a regression of the L1 routing contract."""
    src = (REPO / "gateway" / "support_handoff.py").read_text(encoding="utf-8")
    # Each branch is a literal in the body — search for the
    # provider-name string inside an ``if/elif provider == "X"`` shape.
    for branch in ('"in_product"', '"wechat_qr"', '"email"'):
        assert branch in src, f"handoff missing branch {branch}"
    assert "is_anyone_online" in src, (
        "handoff must consult presence service before routing — "
        "got source without ``is_anyone_online`` call"
    )
    assert "get_qr_metadata" in src, (
        "handoff must check QR availability before falling back to email"
    )


def test_handoff_payload_includes_wechat_qr_url():
    """When the routing chooses wechat_qr, the payload returned to the
    caller MUST include ``wechat_qr_url`` so the widget can render it."""
    src = (REPO / "gateway" / "support_handoff.py").read_text(encoding="utf-8")
    assert "wechat_qr_url" in src
    assert "offline_message" in src


# ---------------------------------------------------------------------------
# Presence service signatures
# ---------------------------------------------------------------------------


def test_presence_module_exports_expected_functions():
    """Sanity: the four functions support_handoff and the API layer
    rely on are present and async. AST-level so we don't need a live
    DB to run."""
    src_path = REPO / "gateway" / "support_presence.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    expected = {
        "record_heartbeat": True,  # async
        "set_status": True,
        "count_online": True,
        "is_anyone_online": True,
        "get_my_presence": True,
        "list_recent": True,
    }
    seen: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in expected:
            seen[node.name] = True
    for name in expected:
        assert seen.get(name) is True, (
            f"support_presence missing async function {name!r}"
        )


def test_presence_status_validation():
    """``set_status`` must reject invalid status strings — the API
    layer trusts this for its 4xx error responses."""
    # We can't easily await without a DB; instead, verify the source
    # contains the validator literal.
    src = (REPO / "gateway" / "support_presence.py").read_text(encoding="utf-8")
    assert '"online"' in src and '"paused"' in src and '"offline"' in src
    assert "_VALID_STATUSES" in src


# ---------------------------------------------------------------------------
# Settings projection
# ---------------------------------------------------------------------------


def test_admin_settings_pydantic_includes_l1_fields():
    """Pydantic schema must expose the four new fields so admin save
    can round-trip them."""
    from gateway.support_models import SupportAdminSettings

    fields = SupportAdminSettings.model_fields
    for name in (
        "support_admin_heartbeat_interval_seconds",
        "support_admin_online_threshold_seconds",
        "support_handoff_offline_fallback_minutes",
        "support_offline_message",
    ):
        assert name in fields, f"SupportAdminSettings missing {name!r}"


def test_admin_settings_defaults_match_plan():
    from gateway.support_models import SupportAdminSettings

    s = SupportAdminSettings()
    assert s.support_admin_heartbeat_interval_seconds == 30
    assert s.support_admin_online_threshold_seconds == 60
    assert s.support_handoff_offline_fallback_minutes == 5
    assert "微信" in s.support_offline_message

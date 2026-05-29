"""Phase 2a free-tier feature-flag scaffolding guards (plan 2026-05-29 Task 0).

Task 0 only wires the flag plumbing: a gateway Settings field, default
False, read from the ``AVT_``-prefixed env var (mirrors ``enable_post_edit``
/ ``enable_smart_mode``, gateway/config.py model_config env_prefix="AVT_").

The actual ``service_mode="free"`` rejection gate (fail-closed when the flag
is off, no silent express downgrade) lands in Task 1 and will add its
assertions to this file.
"""
from config import GatewaySettings


def test_enable_free_tier_defaults_false(monkeypatch):
    monkeypatch.delenv("AVT_ENABLE_FREE_TIER", raising=False)
    s = GatewaySettings(database_url="", pg_password="")
    assert s.enable_free_tier is False


def test_enable_free_tier_reads_avt_env(monkeypatch):
    monkeypatch.setenv("AVT_ENABLE_FREE_TIER", "true")
    s = GatewaySettings(database_url="", pg_password="")
    assert s.enable_free_tier is True

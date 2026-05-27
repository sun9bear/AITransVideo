"""Phase 4.2 E.1 PR #15 P2 fix (Codex 2026-05-27 review on commit 9a621859).

Static guard on ``frontend-next/next.config.ts``:

- **NO blanket** ``/api/:path*`` dev rewrite — that would proxy
  EVERYTHING (including paid CosyVoice clone calls + clone-gate session
  reads) to ``https://aitrans.video`` during ``next dev``.
- **NO cosyvoice / clone** path in the dev rewrite destination — the
  CosyVoice clone endpoint and clone-gate must never be silently
  bridged to production from a dev machine (CLAUDE.md paid-API
  hard constraint).
- Allowed sources are an explicit allowlist: ``/api/plans`` (pricing
  SSR for PricingGrid + TrialBanner). Any future addition needs to
  classify the endpoint as "safe to read with prod data" — paid /
  authenticated / user-mutating endpoints are forbidden.

Why these tests aren't simple text scans only: this is the kind of
config drift that **silently** breaks the paid-API isolation. The
project already has multiple paid-API guards (CLAUDE.md, F.6/F.8,
no-auto-retry guards) — this rounds out the dev-config layer so a
developer can never accidentally fire a real clone against prod when
running ``next dev``.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NEXT_CONFIG = REPO_ROOT / "frontend-next" / "next.config.ts"


def _strip_ts_comments(src: str) -> str:
    """Strip ``//`` line comments + ``/* ... */`` block comments. We want
    to inspect actual route declarations, not explanatory comments that
    may mention forbidden patterns as counterexamples."""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def test_next_config_file_exists():
    """Sanity: the config file we're guarding actually exists."""
    assert NEXT_CONFIG.is_file(), f"next.config.ts missing at {NEXT_CONFIG}"


def test_no_blanket_api_path_rewrite_source():
    """**Primary guard**: no rewrite rule with ``source: "/api/:path*"``.

    A blanket source would proxy EVERY ``/api/*`` request — including
    paid-API endpoints — to the destination. Allowlist sources only.
    """
    src = _strip_ts_comments(NEXT_CONFIG.read_text(encoding="utf-8"))
    # Match any string literal `/api/:path*` (or `/api/:something*` variants)
    # used as a rewrite source. The :path* / :*params* syntax is what
    # creates the wildcard behavior.
    forbidden = re.compile(r'source:\s*[\'"`]/api/:[a-zA-Z_]+\*[\'"`]')
    matches = forbidden.findall(src)
    assert not matches, (
        f"next.config.ts has a blanket /api/:path* rewrite source: "
        f"{matches!r}\n"
        f"This proxies ALL /api/* requests to the destination, including "
        f"paid-API call sites like /api/voice/cosyvoice/clone. Use an "
        f"explicit allowlist of safe-to-proxy endpoints instead."
    )


def test_no_blanket_api_destination():
    """**Defense in depth**: also forbid ``destination: ".../api/:path*"``.

    Even if the source is narrow, a destination wildcard with the same
    parameter name plus an over-broad source could still bridge calls
    we don't want. Catches both halves of the pattern.
    """
    src = _strip_ts_comments(NEXT_CONFIG.read_text(encoding="utf-8"))
    forbidden = re.compile(
        r'destination:\s*[\'"`]https?://[^/]+/api/:[a-zA-Z_]+\*[\'"`]'
    )
    matches = forbidden.findall(src)
    assert not matches, (
        f"next.config.ts has a wildcard destination forwarding ALL "
        f"/api/* to a remote host: {matches!r}\n"
        f"Use exact paths for each allowlisted endpoint."
    )


def test_no_cosyvoice_in_dev_rewrites():
    """**Paid API protection**: no rewrite rule whose source matches
    ``/api/voice/cosyvoice/`` (clone-gate, clone) nor destination
    pointing to a remote cosyvoice path.

    These endpoints are paid (DashScope CosyVoice via gateway → mainland
    worker) and per-user authenticated. They must NEVER be silently
    proxied to production from a local dev machine — that's how an
    accidental ``next dev`` session could burn real DashScope quota.
    """
    src = _strip_ts_comments(NEXT_CONFIG.read_text(encoding="utf-8"))
    # Source side
    src_pattern = re.compile(r'source:\s*[\'"`][^\'"`]*cosyvoice[^\'"`]*[\'"`]')
    src_matches = src_pattern.findall(src)
    assert not src_matches, (
        f"next.config.ts rewrites a CosyVoice path: {src_matches!r}\n"
        f"CosyVoice endpoints (clone, clone-gate) are paid + "
        f"user-authenticated. Never rewrite to remote in dev."
    )
    # Destination side
    dst_pattern = re.compile(
        r'destination:\s*[\'"`][^\'"`]*cosyvoice[^\'"`]*[\'"`]'
    )
    dst_matches = dst_pattern.findall(src)
    assert not dst_matches, (
        f"next.config.ts destination points at a CosyVoice path: "
        f"{dst_matches!r}\n"
        f"Same paid-API hard constraint applies to destinations."
    )


def test_no_voice_clone_endpoints_in_dev_rewrites():
    """**Defense**: also explicitly forbid ``voice-clone`` (MiniMax legacy
    clone) + ``/clone`` standalone in dev rewrites. Same paid-API
    reasoning — MiniMax clone has its own billing.
    """
    src = _strip_ts_comments(NEXT_CONFIG.read_text(encoding="utf-8"))
    # Look for "clone" appearing in any rewrite source/destination
    rewrite_blocks = re.findall(
        r"\{\s*source:\s*[\'\"`][^\'\"`]+[\'\"`]\s*,\s*destination:\s*[\'\"`][^\'\"`]+[\'\"`]\s*,?\s*\}",
        src,
    )
    for block in rewrite_blocks:
        # The 'clone-gate' literal also contains "clone", as does
        # 'voice-clone'. Both must be absent.
        assert "clone" not in block, (
            f"next.config.ts rewrite block mentions 'clone' "
            f"(paid API): {block!r}"
        )


def test_dev_rewrite_allowlist_is_pricing_only():
    """**Allowlist verification**: the only dev rewrite sources we
    expect are pricing / public-catalog endpoints. Today that's just
    ``/api/plans``. Any addition must update both this test and the
    config — forcing review of the "is this safe to read with prod
    data?" question.

    Allowed prefixes (small, explicit):
      - ``/api/plans`` — pricing tier SSR
      - ``/api/marketing/*`` — public marketing data (future)
      - ``/api/catalog/*`` — public voice catalog (future)
    """
    src = _strip_ts_comments(NEXT_CONFIG.read_text(encoding="utf-8"))
    # Extract all source: "..." values from rewrite blocks
    sources = re.findall(r'source:\s*[\'"`]([^\'"`]+)[\'"`]', src)
    allowed_prefixes = (
        "/api/plans",
        "/api/marketing/",
        "/api/catalog/",
    )
    for source in sources:
        ok = any(
            source == p or source.startswith(p) for p in allowed_prefixes
        )
        assert ok, (
            f"next.config.ts dev rewrite has source {source!r} which is "
            f"not in the safe-to-proxy allowlist {allowed_prefixes!r}.\n"
            f"If this endpoint legitimately needs prod data in dev, add "
            f"it to this allowlist AND update the test — that forces "
            f"review of the paid-API / auth-safety question."
        )


def test_dev_rewrite_destinations_only_point_at_known_host():
    """**Defense**: destinations point only at the production marketing
    domain. Defense against typos like ``https://staging.example.com``
    leaking PII or any other host accidentally."""
    src = _strip_ts_comments(NEXT_CONFIG.read_text(encoding="utf-8"))
    destinations = re.findall(r'destination:\s*[\'"`]([^\'"`]+)[\'"`]', src)
    allowed_hosts = (
        "https://aitrans.video/",
    )
    for dst in destinations:
        ok = any(dst.startswith(h) for h in allowed_hosts)
        assert ok, (
            f"next.config.ts rewrite destination {dst!r} doesn't match "
            f"the allowed host list {allowed_hosts!r}."
        )

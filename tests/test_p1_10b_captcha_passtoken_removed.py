"""Audit P1-10b / S-HIGH-2 regression guard: captcha pre-verify pass-token
flow stays deleted.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        S-HIGH-2 — the pre-verify endpoint signed an in-memory
                   ``pass_token``, but ``send_code_endpoint`` never
                   called ``consume_captcha_pass``; instead it ran
                   ``risk_control.verify_captcha`` a second time on
                   the original captcha_token. With single-use
                   provider tokens (Cloudflare Turnstile, Aliyun)
                   this re-verification fails — so the captcha chain
                   was broken in production unless
                   AVT_CAPTCHA_PROVIDER=fake. The pre-verify pass
                   token was dead code; the dict only got cleaned at
                   issuance, so a hostile caller could spam
                   /pre-verify to grow memory unbounded.

Per audit option (b), the entire pass-token flow is deleted:
``_captcha_passes`` dict, ``issue_captcha_pass`` /
``consume_captcha_pass`` helpers, ``PreVerifyRequest`` model,
``captcha_pre_verify`` route, and the ``captcha_router`` that mounted
it. ``send_code_endpoint`` keeps a single ``risk_control.verify_captcha``
call.

These guards keep that decision sticky. If a future change reintroduces
the dead code (e.g. someone restores option (a) without wiring the
frontend), CI immediately catches the drift.
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


def test_auth_phone_no_passtoken_dict_or_lock():
    """The in-memory ``_captcha_passes`` dict + lock are gone, and the
    issuance / consumption helpers are not exposed. A re-introduction
    would re-open the unbounded-memory + dead-code surfaces."""
    import auth_phone

    forbidden = (
        "_captcha_passes",
        "_captcha_lock",
        "_cleanup_expired_passes",
        "issue_captcha_pass",
        "consume_captcha_pass",
        "captcha_pre_verify",
        "PreVerifyRequest",
        "captcha_router",
    )
    leaked = [name for name in forbidden if hasattr(auth_phone, name)]
    assert not leaked, (
        "P1-10b regression: pre-verify pass-token symbols reappeared "
        f"on auth_phone module: {leaked}. The audit removed this flow "
        "as dead code (S-HIGH-2). If a real use case has surfaced, "
        "wire the frontend to /pre-verify and update consume_captcha_pass "
        "into send_code_endpoint — but DON'T silently restore symbols."
    )


def test_auth_phone_source_does_not_reference_dead_symbols():
    """Belt-and-suspenders source-level scan in case the symbols above
    are introduced as nested locals (which getattr wouldn't catch)."""
    src = (_REPO_ROOT / "gateway" / "auth_phone.py").read_text(encoding="utf-8")
    forbidden = (
        "_captcha_passes",
        "issue_captcha_pass",
        "consume_captcha_pass",
        "PreVerifyRequest",
    )
    # Allow the explanation block in the module docstring / inline
    # comment — but only as text following a leading "#" or inside a
    # triple-quoted string. We do this by stripping comments and
    # docstrings, then scanning the remainder.
    import io
    import tokenize

    source_only_chars: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenizeError:
        # Fall back to raw source if tokenization fails (shouldn't happen).
        source_only_chars.append(src)
    else:
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            source_only_chars.append(tok.string)
    code_only = "\n".join(source_only_chars)

    leaked = [name for name in forbidden if name in code_only]
    assert not leaked, (
        f"P1-10b regression: forbidden symbol(s) {leaked} appear in "
        f"executable code of auth_phone.py (excluding comments and "
        f"docstrings). The pass-token flow must stay deleted."
    )


def test_main_no_captcha_router_registration():
    """``main.py`` no longer imports or mounts ``captcha_router``."""
    src = (_REPO_ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
    # The import line that pulled captcha_router from auth_phone is gone.
    assert "captcha_router" not in src or all(
        line.lstrip().startswith("#")
        for line in src.splitlines()
        if "captcha_router" in line
    ), (
        "P1-10b regression: gateway/main.py still imports or mounts "
        "captcha_router. The pre-verify endpoint must stay unmounted "
        "to keep the dead code from re-attaching itself to a route."
    )


def test_send_code_endpoint_still_verifies_captcha():
    """No-regression: removing the pass-token flow must NOT also remove
    the captcha check from ``send_code_endpoint``. The captcha widget
    is the user's only proof-of-human gate before SMS issuance."""
    import inspect

    import auth_phone

    src = inspect.getsource(auth_phone.send_code_endpoint)
    # The single source-of-truth captcha verification.
    assert "risk_control.verify_captcha" in src, (
        "P1-10b regression: send_code_endpoint no longer calls "
        "risk_control.verify_captcha. Removing the pass-token flow "
        "should NOT also remove the captcha gate — the human-check "
        "must run on every send-code attempt."
    )

"""APF3a anonymous Free/Express preview contract tests.

Exercises :mod:`src.services.anonymous_preview_admission` against pure
inputs only. No real backend, gateway, frontend, upload, probe,
compliance, preview media, clone provider, pricing, payment, migration
or deployment code is touched.

Design source of truth:
``docs/plans/2026-06-04-apf3a-preview-contract.md``.
Pure contract module under test:
``src/services/anonymous_preview_admission.py``.
"""

from __future__ import annotations

import ast
import math
from dataclasses import fields, replace
from pathlib import Path

import pytest

from src.services.anonymous_preview_admission import (
    ADMITTED_REASON,
    DEFAULT_EXPRESS_PREVIEW_QUOTA_PER_ANONYMOUS,
    DEFAULT_FREE_PREVIEW_QUOTA_PER_ANONYMOUS,
    DEFAULT_MAX_PREVIEW_DURATION_SECONDS,
    FORBIDDEN_ADMISSION_FIELDS,
    LOGIN_REQUIRED_REASON,
    NOT_ANONYMOUS_FUNNEL_REASON,
    UNKNOWN_MODE_REASON,
    AdmissionDecision,
    AdmissionRejected,
    AnonymousPreviewAdmission,
    AnonymousPreviewAdmissionConfig,
    AnonymousPreviewArtifactPolicy,
    AnonymousPreviewMode,
    VoiceStrategy,
    evaluate_anonymous_preview_admission,
    raise_clone_provider_boundary,
)


_ADMISSION_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "services"
    / "anonymous_preview_admission.py"
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _default_config(**overrides) -> AnonymousPreviewAdmissionConfig:
    base = AnonymousPreviewAdmissionConfig()
    if overrides:
        return replace(base, **overrides)
    return base


# ---------------------------------------------------------------------------
# (1) Pinned defaults.
# ---------------------------------------------------------------------------


def test_default_preview_duration_cap_is_180s():
    assert DEFAULT_MAX_PREVIEW_DURATION_SECONDS == 180.0


def test_default_anonymous_quotas_are_one():
    assert DEFAULT_FREE_PREVIEW_QUOTA_PER_ANONYMOUS == 1
    assert DEFAULT_EXPRESS_PREVIEW_QUOTA_PER_ANONYMOUS == 1


def test_default_config_disables_anonymous_express_clone():
    cfg = AnonymousPreviewAdmissionConfig()
    assert cfg.anonymous_express_cosyvoice_clone_enabled is False


def test_default_artifact_policy_is_locked_down():
    policy = AnonymousPreviewArtifactPolicy()
    assert policy.watermark_required is True
    assert policy.allow_download_url is False
    assert policy.allow_subtitle_export is False
    assert policy.allow_jianying_draft_export is False
    assert policy.allow_payment_fields is False
    assert policy.allow_provider_voice_id is False
    assert policy.allow_clone_artifact is False
    # APF3a contract markers — stream-only / no editable assets /
    # TTL required / low queue priority. These are required markers
    # only; concrete TTL seconds and queue priority numbers are pinned
    # in a later phase, not in this contract shell.
    assert policy.stream_only_required is True
    assert policy.allow_editable_assets is False
    assert policy.artifact_ttl_required is True
    assert policy.low_priority_required is True


def test_artifact_policy_dataclass_has_no_forbidden_url_or_provider_fields():
    """Even with the new APF3a markers, the policy dataclass MUST NOT
    grow fields that *carry* a real URL / provider / payment / credit /
    voice-id / token value — those belong to later phases that wire real
    storage / queue / pricing.

    The naive bare-substring check used in the initial scaffold flagged
    legitimate negative deny markers like ``allow_download_url=False`` /
    ``allow_provider_voice_id=False`` / ``allow_payment_fields=False``,
    which are explicitly part of the locked-down contract. This test
    therefore guards the dataclass with:

    1. an explicit allowlist of known good policy marker names (any new
       field must be vetted and added here before the suite goes green);
    2. a precision denylist of real value-carrying field names (defense
       in depth against accidentally landing one through the allowlist).
    """

    field_names = {f.name for f in fields(AnonymousPreviewArtifactPolicy)}

    # (1) Explicit allowlist — only boolean policy markers are permitted
    # on the APF3a dataclass. Adding a new field requires updating this
    # allowlist deliberately, which is exactly the gate we want.
    allowed_field_names = {
        "watermark_required",
        "allow_download_url",
        "allow_subtitle_export",
        "allow_jianying_draft_export",
        "allow_payment_fields",
        "allow_provider_voice_id",
        "allow_clone_artifact",
        "stream_only_required",
        "allow_editable_assets",
        "artifact_ttl_required",
        "low_priority_required",
    }
    unexpected = sorted(field_names - allowed_field_names)
    assert unexpected == [], (
        "AnonymousPreviewArtifactPolicy gained fields outside the APF3a "
        f"allowlist: {unexpected}. Only boolean deny markers "
        "(allow_*=False) and required-flag markers (*_required=True) are "
        "permitted in this phase; real value-carrying fields like "
        "preview_url / download_url_value / provider_voice_id / "
        "payment_token / pricing_quote / credit_reservation_id / "
        "provider_voice_id_value / token belong to a later phase that "
        "wires real storage / queue / pricing."
    )

    # (2) Precision denylist of known value-carrying field names. This
    # is a defense-in-depth check distinct from (1): even if the
    # allowlist were widened by accident in a future refactor, any of
    # these exact field names would still red the suite.
    forbidden_value_field_names = {
        "preview_url",
        "preview_url_value",
        "download_url",
        "download_url_value",
        "provider_voice_id",
        "provider_voice_id_value",
        "payment_token",
        "pricing_quote",
        "credit_reservation_id",
        "clone_reservation_id",
        "token",
        "endpoint",
        "preview_artifact_key",
    }
    leaks = sorted(field_names & forbidden_value_field_names)
    assert leaks == [], (
        f"AnonymousPreviewArtifactPolicy leaks value-carrying fields: {leaks}"
    )


# ---------------------------------------------------------------------------
# (2) Free / Express happy paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [AnonymousPreviewMode.FREE, AnonymousPreviewMode.EXPRESS, "free", "express"],
)
def test_free_and_express_admitted_for_short_source(mode):
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=mode,
        source_duration_seconds=42.0,
    )

    assert admission.decision is AdmissionDecision.ADMITTED
    assert admission.preview_duration_seconds == pytest.approx(42.0)
    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY
    assert admission.artifact_policy.watermark_required is True
    assert admission.artifact_policy.allow_download_url is False
    assert admission.reason == ADMITTED_REASON
    assert admission.next_step_hint is None


def test_free_caps_long_source_to_180s():
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=600.0,
    )

    assert admission.decision is AdmissionDecision.ADMITTED
    assert admission.preview_duration_seconds == pytest.approx(180.0)


def test_express_caps_at_exact_boundary():
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.EXPRESS,
        source_duration_seconds=180.0,
    )

    assert admission.decision is AdmissionDecision.ADMITTED
    assert admission.preview_duration_seconds == pytest.approx(180.0)


def test_zero_duration_admitted_returns_zero():
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=0.0,
    )

    assert admission.decision is AdmissionDecision.ADMITTED
    assert admission.preview_duration_seconds == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# (3) Smart escalates to login; Studio leaves the funnel entirely.
# ---------------------------------------------------------------------------


def test_smart_anonymous_requires_login():
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.SMART,
        source_duration_seconds=120.0,
    )

    assert admission.decision is AdmissionDecision.LOGIN_REQUIRED
    assert admission.preview_duration_seconds == 0.0
    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY
    assert admission.reason == LOGIN_REQUIRED_REASON
    assert admission.next_step_hint == "login_required"
    assert admission.artifact_policy.watermark_required is True


def test_studio_not_in_anonymous_funnel():
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.STUDIO,
        source_duration_seconds=120.0,
    )

    assert admission.decision is AdmissionDecision.NOT_ANONYMOUS_FUNNEL
    assert admission.preview_duration_seconds == 0.0
    assert admission.reason == NOT_ANONYMOUS_FUNNEL_REASON
    assert admission.next_step_hint == "studio_requires_login_and_entitlement"


# ---------------------------------------------------------------------------
# (4) Artifact policy is enforced regardless of decision.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [
        AnonymousPreviewMode.FREE,
        AnonymousPreviewMode.EXPRESS,
        AnonymousPreviewMode.SMART,
        AnonymousPreviewMode.STUDIO,
    ],
)
def test_artifact_policy_locked_for_every_mode(mode):
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=mode,
        source_duration_seconds=60.0,
    )

    policy = admission.artifact_policy
    assert policy.watermark_required is True
    assert policy.allow_download_url is False
    assert policy.allow_subtitle_export is False
    assert policy.allow_jianying_draft_export is False
    assert policy.allow_payment_fields is False
    assert policy.allow_provider_voice_id is False
    assert policy.allow_clone_artifact is False
    assert policy.stream_only_required is True
    assert policy.allow_editable_assets is False
    assert policy.artifact_ttl_required is True
    assert policy.low_priority_required is True


def test_admission_dataclass_has_no_forbidden_fields():
    admission_field_names = {f.name for f in fields(AnonymousPreviewAdmission)}
    leak = admission_field_names & FORBIDDEN_ADMISSION_FIELDS
    assert leak == set(), f"admission dataclass leaks forbidden fields: {leak}"


def test_admission_is_frozen_and_immutable():
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=10.0,
    )
    with pytest.raises(Exception):
        admission.preview_duration_seconds = 9999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# (5) Express clone gate — default closed, explicit-true stays at boundary.
# ---------------------------------------------------------------------------


def test_express_admission_defaults_to_preset_only():
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.EXPRESS,
        source_duration_seconds=90.0,
    )

    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY


def test_express_admission_with_flag_truthy_string_stays_preset_only():
    # Strings / truthy non-True values must not flip the gate. Only an
    # explicit Python ``True`` is accepted.
    cfg = _default_config()
    cfg = replace(
        cfg,
        anonymous_express_cosyvoice_clone_enabled="true",  # type: ignore[arg-type]
    )

    admission = evaluate_anonymous_preview_admission(
        config=cfg,
        mode=AnonymousPreviewMode.EXPRESS,
        source_duration_seconds=90.0,
    )

    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY


def test_express_admission_with_flag_true_exposes_boundary_strategy():
    cfg = _default_config(anonymous_express_cosyvoice_clone_enabled=True)

    admission = evaluate_anonymous_preview_admission(
        config=cfg,
        mode=AnonymousPreviewMode.EXPRESS,
        source_duration_seconds=90.0,
    )

    assert admission.decision is AdmissionDecision.ADMITTED
    assert admission.voice_strategy is VoiceStrategy.EXPRESS_TEMPORARY_CLONE_GATE


def test_clone_provider_boundary_always_raises_not_implemented():
    with pytest.raises(NotImplementedError) as excinfo:
        raise_clone_provider_boundary(AnonymousPreviewMode.EXPRESS)
    assert "no provider wiring" in str(excinfo.value)


def test_free_admission_never_emits_clone_strategy_even_when_flag_on():
    cfg = _default_config(anonymous_express_cosyvoice_clone_enabled=True)

    admission = evaluate_anonymous_preview_admission(
        config=cfg,
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=90.0,
    )

    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY


# ---------------------------------------------------------------------------
# (6) Fail-closed branches.
# ---------------------------------------------------------------------------


def test_missing_config_fails_closed():
    admission = evaluate_anonymous_preview_admission(
        config=None,
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=60.0,
    )

    assert admission.decision is AdmissionDecision.FAILED
    assert "AnonymousPreviewAdmissionConfig is missing" in admission.reason
    assert admission.preview_duration_seconds == 0.0
    assert admission.next_step_hint == "retry_or_contact_support"


@pytest.mark.parametrize(
    "bad_max",
    [0, 0.0, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_invalid_max_duration_in_config_fails_closed(bad_max):
    cfg = AnonymousPreviewAdmissionConfig(max_preview_duration_seconds=bad_max)

    admission = evaluate_anonymous_preview_admission(
        config=cfg,
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=10.0,
    )

    assert admission.decision is AdmissionDecision.FAILED
    assert "max_preview_duration_seconds" in admission.reason


@pytest.mark.parametrize("bad_max", [True, False])
def test_boolean_max_preview_duration_in_config_fails_closed(bad_max):
    """P2 regression — ``bool`` is a subclass of ``int``, so without an
    explicit guard ``max_preview_duration_seconds=True`` / ``False`` would
    silently coerce to ``1.0`` / ``0.0``. ``True`` would cap every preview
    to 1 second and ``False`` would trip the ``<= 0`` branch with a
    confusing reason instead of a deterministic boolean rejection. APF3a
    admission must reject boolean configuration values before any numeric
    conversion, fail closed, leave ``preview_duration_seconds`` at
    ``0.0``, and must not silently admit at the bogus 1.0 / 0.0 cap nor
    leak the raw boolean / cap value in the rejection reason or hint."""

    cfg = AnonymousPreviewAdmissionConfig(max_preview_duration_seconds=bad_max)

    admission = evaluate_anonymous_preview_admission(
        config=cfg,
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=42.0,
    )

    assert admission.decision is AdmissionDecision.FAILED
    assert admission.preview_duration_seconds == 0.0
    # Must NOT silently cap to True->1.0 or False->0.0 as if the request
    # was admitted at a degenerate boundary.
    assert admission.preview_duration_seconds != 1.0 or (
        admission.decision is AdmissionDecision.FAILED
    )
    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY
    assert "max_preview_duration_seconds" in admission.reason
    # Locked-down policy still enforced on the failure record.
    assert admission.artifact_policy.watermark_required is True
    assert admission.artifact_policy.allow_download_url is False
    assert admission.artifact_policy.allow_provider_voice_id is False
    assert admission.artifact_policy.allow_clone_artifact is False
    assert admission.artifact_policy.allow_payment_fields is False
    # No echo of the raw boolean / silent-cap value, no provider / token
    # / path / payment / clone identifier surfaced via reason or hint.
    rendered = f"{admission.reason}|{admission.next_step_hint or ''}"
    for fragment in (
        "True",
        "False",
        "1.0",
        "0.0",
        "bool",
        "Bearer",
        "Token",
        "sk_live",
        "/tmp/",
        "path=",
        "minimax",
        "cosyvoice",
        "volcengine",
        "voice_clone",
        "clone_voice",
        "clone_provider_voice_id",
        "clone_reservation_id",
        "voice_clone_voice_id",
        "payment_token",
        "pricing_quote",
        "credit_reservation_id",
        "preview_url",
        "download_url",
    ):
        assert fragment not in rendered, (
            "boolean max_preview_duration rejection reason/hint leaks "
            f"{fragment!r}: {rendered!r}"
        )


@pytest.mark.parametrize("bad_mode", ["bogus", "Free ", "", "premium", 0, None])
def test_unknown_mode_is_rejected(bad_mode):
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=bad_mode,
        source_duration_seconds=10.0,
    )

    assert admission.decision is AdmissionDecision.REJECTED
    assert "unknown anonymous preview mode" in admission.reason
    assert admission.preview_duration_seconds == 0.0
    assert admission.next_step_hint == "fix_input_and_retry"


@pytest.mark.parametrize(
    "bad_duration",
    [-1, -0.5, float("nan"), float("inf"), float("-inf")],
)
def test_invalid_duration_fails_closed(bad_duration):
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=bad_duration,
    )

    assert admission.decision is AdmissionDecision.FAILED
    assert "source_duration_seconds" in admission.reason
    assert admission.preview_duration_seconds == 0.0


@pytest.mark.parametrize("bad_duration", ["120", None, object()])
def test_non_numeric_duration_fails_closed(bad_duration):
    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=bad_duration,
    )

    assert admission.decision is AdmissionDecision.FAILED
    assert "source_duration_seconds" in admission.reason


@pytest.mark.parametrize("bad_duration", [True, False])
def test_boolean_duration_fails_closed(bad_duration):
    """P2 regression — ``bool`` is a subclass of ``int``, so without an
    explicit guard ``True`` / ``False`` would silently coerce to ``1.0``
    / ``0.0`` and pass numeric validation. APF3a admission must reject
    boolean inputs before float conversion and must not leak the raw
    value or any provider / token / path / payment / clone identifier in
    the rejection reason or hint."""

    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=AnonymousPreviewMode.FREE,
        source_duration_seconds=bad_duration,
    )

    assert admission.decision is AdmissionDecision.FAILED
    assert admission.preview_duration_seconds == 0.0
    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY
    assert "source_duration_seconds" in admission.reason
    # Locked-down policy is still enforced on the failure record.
    assert admission.artifact_policy.watermark_required is True
    assert admission.artifact_policy.allow_download_url is False
    assert admission.artifact_policy.allow_provider_voice_id is False
    assert admission.artifact_policy.allow_clone_artifact is False
    assert admission.artifact_policy.allow_payment_fields is False
    # No echo of the raw boolean, no provider / token / path / payment /
    # clone identifier surfaced via reason or hint.
    rendered = f"{admission.reason}|{admission.next_step_hint or ''}"
    for fragment in (
        "True",
        "False",
        "1.0",
        "0.0",
        "bool",
        "Bearer",
        "Token",
        "sk_live",
        "/tmp/",
        "path=",
        "minimax",
        "cosyvoice",
        "volcengine",
        "voice_clone",
        "clone_voice",
        "clone_provider_voice_id",
        "clone_reservation_id",
        "voice_clone_voice_id",
        "payment_token",
        "pricing_quote",
        "credit_reservation_id",
        "preview_url",
        "download_url",
    ):
        assert fragment not in rendered, (
            f"boolean rejection reason/hint leaks {fragment!r}: {rendered!r}"
        )


# ---------------------------------------------------------------------------
# (7) Module-level import / surface guards.
# ---------------------------------------------------------------------------


def test_admission_module_imports_only_stdlib():
    """The admission module must only import stdlib symbols.

    Allowed roots: stdlib. Forbidden: any ``src`` import (this module is
    a leaf in the contract layer), any gateway/frontend/provider import,
    and any network/process import.
    """

    source = _ADMISSION_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_top_level = {
        "gateway",
        "frontend",
        "frontend_next",
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "httpx",
        "boto3",
        "aiohttp",
        "subprocess",
        "src",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in forbidden_top_level:
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in forbidden_top_level:
                offenders.append(f"from {module} import ...")

    assert offenders == [], (
        f"anonymous_preview_admission imports forbidden modules: {offenders}"
    )


def test_admission_module_has_no_forbidden_call_sites():
    """The admission module must not call provider / network / IO APIs."""

    source = _ADMISSION_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_call_names = {
        "open",
        "compile",
        "exec",
        "eval",
        "system",
        "Popen",
        "run",
        "urlopen",
        "urlretrieve",
        "request",
        "post",
        "get",
        "send",
        "synthesize",
        "clone_voice",
        "voice_clone",
        "record_voice_clone",
        "charge",
        "pricing_quote",
        "create_payment",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in forbidden_call_names:
                offenders.append(f"call {name}")
    assert offenders == [], (
        f"anonymous_preview_admission performs forbidden calls: {offenders}"
    )


def test_admission_module_exports_no_clone_or_pricing_callables():
    """Public surface scan — function names must not contain provider /
    pricing / payment substrings."""

    source = _ADMISSION_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_substrings = (
        "preview_url",
        "download_url",
        "preview_artifact",
        "clone_voice",
        "voice_clone",
        "synthesize",
        "pricing",
        "payment",
        "credit_reservation",
    )
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for fragment in forbidden_substrings:
                if fragment in node.name:
                    offenders.append(f"function {node.name}")
    assert offenders == [], (
        f"anonymous_preview_admission exposes forbidden surface: {offenders}"
    )


def test_scaffold_does_not_use_skip_or_xfail():
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    forbidden = {"skip", "skipif", "xfail"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            if isinstance(node.value, ast.Attribute) and node.value.attr == "mark":
                offenders.append(f"pytest.mark.{node.attr}")
            elif isinstance(node.value, ast.Name) and node.value.id == "pytest":
                offenders.append(f"pytest.{node.attr}")
    assert offenders == [], (
        f"scaffold uses forbidden skip/xfail markers: {offenders}"
    )


# ---------------------------------------------------------------------------
# (8) Sanity — AdmissionRejected carries decision + reason.
# ---------------------------------------------------------------------------


def test_admission_rejected_exception_carries_decision():
    exc = AdmissionRejected(AdmissionDecision.LOGIN_REQUIRED, "smart")
    assert exc.decision is AdmissionDecision.LOGIN_REQUIRED
    assert exc.reason == "smart"
    assert "login_required" in str(exc)


# ---------------------------------------------------------------------------
# (9) Unknown-mode redaction — ``reason`` MUST NOT echo raw input.
# ---------------------------------------------------------------------------


_SENSITIVE_MODE_STRINGS = [
    "Bearer abc.def.ghi",
    "sk_live_51HxYzABCDEF",
    "path=/tmp/raw.mp4",
    "Authorization: Token deadbeef",
    "../../etc/passwd",
    "<script>alert(1)</script>",
]


@pytest.mark.parametrize("malicious_mode", _SENSITIVE_MODE_STRINGS)
def test_unknown_string_mode_reason_does_not_echo_raw_input(malicious_mode):
    """P1 regression — `_coerce_mode()` must not surface raw user input
    in `admission.reason`, otherwise tokens / paths / payloads could
    leak via logs or status APIs."""

    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=malicious_mode,
        source_duration_seconds=10.0,
    )

    assert admission.decision is AdmissionDecision.REJECTED
    assert admission.reason == UNKNOWN_MODE_REASON
    # Belt-and-suspenders: no fragment of the raw input must appear,
    # neither as plain text nor as a Python ``repr``.
    assert malicious_mode not in admission.reason
    assert repr(malicious_mode) not in admission.reason
    for fragment in ("Bearer", "sk_live", "path=", "Authorization", "script", "etc"):
        if fragment in malicious_mode:
            assert fragment not in admission.reason


class _ModeWithSecretRepr:
    """Non-string mode object whose `repr` carries sensitive data."""

    def __repr__(self) -> str:  # pragma: no cover - asserted via admission
        return "<ModeWithSecretRepr token=sk_live_SECRET path=/var/secret>"


def test_unknown_non_string_mode_reason_does_not_echo_repr():
    """P1 regression — non-string mode objects must not have their
    ``repr`` rendered into ``admission.reason``."""

    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=_ModeWithSecretRepr(),
        source_duration_seconds=10.0,
    )

    assert admission.decision is AdmissionDecision.REJECTED
    assert admission.reason == UNKNOWN_MODE_REASON
    for fragment in ("sk_live", "SECRET", "/var/secret", "ModeWithSecretRepr"):
        assert fragment not in admission.reason


def test_unknown_mode_reason_constant_is_stable_and_input_free():
    """P1 regression — the constant itself is plain text, has no
    placeholders, and stays distinct from the other reason constants."""

    assert UNKNOWN_MODE_REASON == "unknown anonymous preview mode (fail closed)"
    assert "{" not in UNKNOWN_MODE_REASON
    assert "%" not in UNKNOWN_MODE_REASON
    assert UNKNOWN_MODE_REASON != ADMITTED_REASON
    assert UNKNOWN_MODE_REASON != LOGIN_REQUIRED_REASON
    assert UNKNOWN_MODE_REASON != NOT_ANONYMOUS_FUNNEL_REASON


# ---------------------------------------------------------------------------
# (10) PR #23 P2 regression — unsafe ``mode`` object must not be
#      re-coerced inside the fail-closed fallback path.
# ---------------------------------------------------------------------------


class _HostileMode:
    """Non-string mode object whose ``__eq__`` / ``__hash__`` / ``__repr__``
    are all hostile.

    Simulates the PR #23 external P2 attack: an attacker-controlled
    value where the Enum value-comparison done inside
    ``AnonymousPreviewMode(value)`` would invoke ``__eq__`` /
    ``__hash__`` on the value and raise out of the fail-closed branch.
    """

    def __eq__(self, other):  # pragma: no cover - asserted via admission
        raise RuntimeError(
            "hostile mode __eq__ — must never be invoked by APF3a fallback"
        )

    def __ne__(self, other):  # pragma: no cover - same as __eq__
        raise RuntimeError(
            "hostile mode __ne__ — must never be invoked by APF3a fallback"
        )

    def __hash__(self):  # pragma: no cover - asserted via admission
        raise RuntimeError(
            "hostile mode __hash__ — must never be invoked by APF3a fallback"
        )

    def __repr__(self) -> str:  # pragma: no cover - asserted via admission
        return "<HostileMode token=sk_live_LEAK path=/var/secret>"


_FAIL_CLOSED_FORBIDDEN_FRAGMENTS = (
    "HostileMode",
    "sk_live",
    "LEAK",
    "/var/secret",
    "token=",
    "path=",
    "Bearer",
    "Token",
    "Authorization",
    "minimax",
    "cosyvoice",
    "volcengine",
    "voice_clone",
    "clone_voice",
    "clone_provider_voice_id",
    "clone_reservation_id",
    "voice_clone_voice_id",
    "payment_token",
    "pricing_quote",
    "credit_reservation_id",
    "preview_url",
    "download_url",
    "http://",
    "https://",
)


def _assert_unsafe_mode_fail_closed(admission, *, expected_decision):
    assert admission.decision is expected_decision
    # Conservative fallback mode required by the task — must not echo
    # any attribute of the hostile object.
    assert admission.mode is AnonymousPreviewMode.FREE
    assert admission.preview_duration_seconds == 0.0
    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY
    policy = admission.artifact_policy
    assert policy.watermark_required is True
    assert policy.allow_download_url is False
    assert policy.allow_subtitle_export is False
    assert policy.allow_jianying_draft_export is False
    assert policy.allow_payment_fields is False
    assert policy.allow_provider_voice_id is False
    assert policy.allow_clone_artifact is False
    assert policy.stream_only_required is True
    assert policy.allow_editable_assets is False
    assert policy.artifact_ttl_required is True
    assert policy.low_priority_required is True
    rendered = f"{admission.reason}|{admission.next_step_hint or ''}"
    for fragment in _FAIL_CLOSED_FORBIDDEN_FRAGMENTS:
        assert fragment not in rendered, (
            f"fail-closed admission leaks {fragment!r}: {rendered!r}"
        )


def test_unsafe_mode_with_missing_config_fails_closed_without_recoercion():
    """PR #23 external P2 regression — when ``_validate_config`` raises
    before ``_coerce_mode`` runs, the ``except AdmissionRejected``
    fallback must not re-invoke ``AnonymousPreviewMode(mode)`` on a
    low-trust non-string object. Doing so would trigger Enum value
    comparison via the hostile ``__eq__`` / ``__hash__`` and re-raise
    out of the fail-closed branch."""

    admission = evaluate_anonymous_preview_admission(
        config=None,
        mode=_HostileMode(),
        source_duration_seconds=10.0,
    )

    _assert_unsafe_mode_fail_closed(
        admission, expected_decision=AdmissionDecision.FAILED
    )
    # Preserve the upstream failure semantics — the reason is the
    # ``_validate_config`` reason, not the generic unknown-mode reason.
    assert "AnonymousPreviewAdmissionConfig is missing" in admission.reason
    assert admission.next_step_hint == "retry_or_contact_support"


@pytest.mark.parametrize(
    "bad_max",
    [0, -1.0, float("nan"), float("inf"), True, False],
)
def test_unsafe_mode_with_invalid_max_duration_fails_closed_without_recoercion(
    bad_max,
):
    """Same PR #23 P2 regression but the upstream failure originates in
    the ``max_preview_duration_seconds`` validation branch (including
    the r3/r4 boolean fail-closed guard). The fallback must still not
    re-coerce the hostile object."""

    cfg = AnonymousPreviewAdmissionConfig(max_preview_duration_seconds=bad_max)

    admission = evaluate_anonymous_preview_admission(
        config=cfg,
        mode=_HostileMode(),
        source_duration_seconds=10.0,
    )

    _assert_unsafe_mode_fail_closed(
        admission, expected_decision=AdmissionDecision.FAILED
    )
    assert "max_preview_duration_seconds" in admission.reason
    assert admission.next_step_hint == "retry_or_contact_support"


def test_unsafe_mode_with_valid_config_is_rejected_without_recoercion():
    """When ``_validate_config`` succeeds and the hostile object reaches
    ``_coerce_mode``, the coercion helper rejects on the explicit
    non-string branch (which never invokes ``AnonymousPreviewMode(value)``).
    The fallback path must still avoid re-coercion and fall back to
    the conservative ``FREE`` mode."""

    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=_HostileMode(),
        source_duration_seconds=10.0,
    )

    _assert_unsafe_mode_fail_closed(
        admission, expected_decision=AdmissionDecision.REJECTED
    )
    assert admission.reason == UNKNOWN_MODE_REASON
    assert admission.next_step_hint == "fix_input_and_retry"


@pytest.mark.parametrize(
    "bad_duration",
    [-1, float("nan"), float("inf"), "120", True, False],
)
def test_unsafe_mode_with_invalid_duration_fails_closed_without_recoercion(
    bad_duration,
):
    """The duration validator only runs after ``_coerce_mode``, so this
    code path actually exercises a successful coercion attempt for a
    hostile object — which still rejects at the non-string branch
    *before* duration is even inspected. Asserts the contract holds
    end-to-end across the fail-closed branches r3/r4 already pin."""

    admission = evaluate_anonymous_preview_admission(
        config=_default_config(),
        mode=_HostileMode(),
        source_duration_seconds=bad_duration,
    )

    # The hostile mode rejects at ``_coerce_mode`` first, so the
    # decision is REJECTED (unknown mode), not FAILED (bad duration).
    _assert_unsafe_mode_fail_closed(
        admission, expected_decision=AdmissionDecision.REJECTED
    )
    assert admission.reason == UNKNOWN_MODE_REASON


def test_unsafe_mode_with_invalid_config_preserves_legitimate_mode_fallback():
    """A legitimate ``AnonymousPreviewMode`` caller must keep its mode
    in the fallback record even when ``_validate_config`` fails first
    — i.e. the new ``resolved_mode`` hoist must not regress the
    behaviour pinned by r3/r4."""

    admission = evaluate_anonymous_preview_admission(
        config=None,
        mode=AnonymousPreviewMode.EXPRESS,
        source_duration_seconds=10.0,
    )

    assert admission.decision is AdmissionDecision.FAILED
    assert admission.mode is AnonymousPreviewMode.EXPRESS
    assert admission.preview_duration_seconds == 0.0
    assert admission.voice_strategy is VoiceStrategy.PRESET_ONLY
    assert "AnonymousPreviewAdmissionConfig is missing" in admission.reason


def test_admission_except_block_does_not_recoerce_mode():
    """AST guard — the ``except AdmissionRejected`` block inside
    ``evaluate_anonymous_preview_admission`` must not contain a
    ``AnonymousPreviewMode(mode)`` call. That call triggers Enum
    value-comparison via ``__eq__`` / ``__hash__`` on a low-trust
    value and re-raises out of the fail-closed branch (PR #23 P2)."""

    source = _ADMISSION_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "evaluate_anonymous_preview_admission":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Try):
                continue
            for handler in child.handlers:
                handler_type = handler.type
                if not (
                    isinstance(handler_type, ast.Name)
                    and handler_type.id == "AdmissionRejected"
                ):
                    continue
                for sub in ast.walk(handler):
                    if not isinstance(sub, ast.Call):
                        continue
                    func = sub.func
                    name = None
                    if isinstance(func, ast.Name):
                        name = func.id
                    elif isinstance(func, ast.Attribute):
                        name = func.attr
                    if name == "AnonymousPreviewMode" and sub.args:
                        arg = sub.args[0]
                        if isinstance(arg, ast.Name) and arg.id == "mode":
                            offenders.append(
                                f"AnonymousPreviewMode({arg.id}) at "
                                f"line {sub.lineno}"
                            )

    assert offenders == [], (
        "evaluate_anonymous_preview_admission re-coerces a low-trust "
        f"value in its fail-closed branch: {offenders}"
    )


def test_admission_source_has_no_mode_repr_format_strings():
    """P1 regression — AST-scan the admission module to ensure no
    `f"...{mode!r}..."` / `repr(mode)` / `str(mode)` reaches the
    `reason` field. Catches future regressions that try to "helpfully"
    re-add the raw value."""

    source = _ADMISSION_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        # f"...{mode!r}..." or f"...{mode!s}..."
        if isinstance(node, ast.FormattedValue):
            if isinstance(node.value, ast.Name) and node.value.id == "mode":
                offenders.append(
                    f"f-string formats `mode` at line {node.lineno}"
                )
        # repr(mode) / str(mode)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"repr", "str"} and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id == "mode":
                    offenders.append(
                        f"{node.func.id}(mode) at line {node.lineno}"
                    )

    assert offenders == [], (
        f"admission module leaks raw `mode` into reason: {offenders}"
    )

"""Tests for the two new jianying gating fields on OutputRequest.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.4 (J5)
"""
from __future__ import annotations

import pytest

from core.enums import OutputTarget
from modules.output.output_models import OutputRequest


# ---------------------------------------------------------------------------
# 1. Default values
# ---------------------------------------------------------------------------

def test_include_jianying_draft_defaults_to_false() -> None:
    req = OutputRequest(targets=[OutputTarget.EDITOR])
    assert req.include_jianying_draft is False


def test_service_mode_defaults_to_none() -> None:
    req = OutputRequest(targets=[OutputTarget.EDITOR])
    assert req.service_mode is None


def test_both_new_fields_default_together() -> None:
    """Instantiating without the new fields gives safe defaults."""
    req = OutputRequest()
    assert req.include_jianying_draft is False
    assert req.service_mode is None


# ---------------------------------------------------------------------------
# 2. Explicit values are preserved
# ---------------------------------------------------------------------------

def test_include_jianying_draft_explicit_true() -> None:
    req = OutputRequest(targets=[OutputTarget.EDITOR], include_jianying_draft=True)
    assert req.include_jianying_draft is True


def test_service_mode_explicit_studio() -> None:
    req = OutputRequest(targets=[OutputTarget.EDITOR], service_mode="studio")
    assert req.service_mode == "studio"


def test_service_mode_explicit_express() -> None:
    req = OutputRequest(targets=[OutputTarget.PUBLISH], service_mode="express")
    assert req.service_mode == "express"


def test_both_new_fields_explicit() -> None:
    req = OutputRequest(
        targets=[OutputTarget.PUBLISH],
        output_dir="/tmp/out",
        include_jianying_draft=True,
        service_mode="studio",
    )
    assert req.include_jianying_draft is True
    assert req.service_mode == "studio"


# ---------------------------------------------------------------------------
# 3. No regression on existing call sites
# ---------------------------------------------------------------------------

def test_existing_call_site_targets_only() -> None:
    """Existing code: OutputRequest(targets=[OutputTarget.BOTH])"""
    req = OutputRequest(targets=[OutputTarget.BOTH])
    assert req.burn_subtitles is False
    assert req.mix_original_audio is False
    assert req.output_dir is None
    assert req.include_jianying_draft is False
    assert req.service_mode is None


def test_existing_call_site_targets_and_output_dir(tmp_path) -> None:
    """Existing code: OutputRequest(targets=[OutputTarget.PUBLISH], output_dir=str(...))"""
    req = OutputRequest(
        targets=[OutputTarget.PUBLISH],
        output_dir=str(tmp_path),
    )
    assert req.targets == [OutputTarget.PUBLISH]
    assert req.output_dir is not None  # normalised by __post_init__
    assert req.include_jianying_draft is False
    assert req.service_mode is None


def test_existing_call_site_editor_with_output_dir(tmp_path) -> None:
    """Existing code: OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(...))"""
    req = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
    )
    assert req.targets == [OutputTarget.EDITOR]
    assert req.include_jianying_draft is False
    assert req.service_mode is None


# ---------------------------------------------------------------------------
# 4. service_mode accepts None explicitly
# ---------------------------------------------------------------------------

def test_service_mode_explicit_none_matches_default() -> None:
    req_default = OutputRequest(targets=[OutputTarget.EDITOR])
    req_explicit = OutputRequest(targets=[OutputTarget.EDITOR], service_mode=None)
    assert req_default.service_mode == req_explicit.service_mode is None


def test_include_jianying_draft_explicit_false_matches_default() -> None:
    req_default = OutputRequest(targets=[OutputTarget.EDITOR])
    req_explicit = OutputRequest(targets=[OutputTarget.EDITOR], include_jianying_draft=False)
    assert req_default.include_jianying_draft == req_explicit.include_jianying_draft is False

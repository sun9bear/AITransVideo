"""Tests for the AVT_ENABLE_JIANYING_DRAFT env var wiring in process.py.

Verifies that _dispatch_process_output_bundle constructs OutputRequest
with the correct include_jianying_draft and service_mode values based
on env var + config.job_record.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.4 (J5)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.enums import OutputTarget
from modules.output.output_models import OutputBundleResult, OutputRequest
from pipeline.process import ProcessConfig, ProcessPipeline


# ---------------------------------------------------------------------------
# Helpers — minimal stubs so _dispatch_process_output_bundle can run
# ---------------------------------------------------------------------------

def _make_fake_build_result() -> MagicMock:
    """Return a MagicMock that satisfies the OutputDispatcher.dispatch() signature."""
    br = MagicMock()
    br.localized_project = MagicMock()
    br.artifact_index = MagicMock()
    return br


def _make_fake_bundle_result() -> OutputBundleResult:
    editor = MagicMock()
    return OutputBundleResult(editor_result=editor)


def _call_dispatch(
    monkeypatch,
    env_value: str | None,
    job_record=None,
) -> OutputRequest:
    """
    Call _dispatch_process_output_bundle with the given env and job_record.
    Returns the OutputRequest that was passed into OutputDispatcher.dispatch().
    """
    captured: list[OutputRequest] = []

    def fake_dispatch(self_inner, localized_project, artifact_index, request: OutputRequest):
        captured.append(request)
        return _make_fake_bundle_result()

    # Patch the env var
    if env_value is None:
        monkeypatch.delenv("AVT_ENABLE_JIANYING_DRAFT", raising=False)
    else:
        monkeypatch.setenv("AVT_ENABLE_JIANYING_DRAFT", env_value)

    pipeline = ProcessPipeline()
    build_result = _make_fake_build_result()
    config = ProcessConfig(
        youtube_url="https://www.youtube.com/watch?v=dummytest",
        job_record=job_record,
    )

    with patch(
        "modules.output.output_dispatcher.OutputDispatcher.dispatch",
        fake_dispatch,
    ):
        pipeline._dispatch_process_output_bundle(
            project_dir=Path("/tmp/test_project"),
            build_result=build_result,
            config=config,
        )

    assert len(captured) == 1, "dispatch() should have been called exactly once"
    return captured[0]


# ---------------------------------------------------------------------------
# 1. Default env (not set) -> include_jianying_draft=False
# ---------------------------------------------------------------------------

def test_env_not_set_gives_false(monkeypatch) -> None:
    req = _call_dispatch(monkeypatch, env_value=None)
    assert req.include_jianying_draft is False


# ---------------------------------------------------------------------------
# 2. Env = "1" -> include_jianying_draft=True
# ---------------------------------------------------------------------------

def test_env_1_gives_true(monkeypatch) -> None:
    req = _call_dispatch(monkeypatch, env_value="1")
    assert req.include_jianying_draft is True


# ---------------------------------------------------------------------------
# 3. Env = "0" -> include_jianying_draft=False
# ---------------------------------------------------------------------------

def test_env_0_gives_false(monkeypatch) -> None:
    req = _call_dispatch(monkeypatch, env_value="0")
    assert req.include_jianying_draft is False


# ---------------------------------------------------------------------------
# 4. Non-"1" strings do NOT enable the flag (strict check)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("non_one_value", ["true", "True", "TRUE", "yes", "1 ", " 1", "on"])
def test_env_non_1_strings_give_false(monkeypatch, non_one_value: str) -> None:
    req = _call_dispatch(monkeypatch, env_value=non_one_value)
    assert req.include_jianying_draft is False, (
        f"Only exact '1' should enable; got True for value={non_one_value!r}"
    )


# ---------------------------------------------------------------------------
# 5. service_mode from job_record (dict form)
# ---------------------------------------------------------------------------

def test_service_mode_from_dict_job_record(monkeypatch) -> None:
    req = _call_dispatch(
        monkeypatch,
        env_value=None,
        job_record={"service_mode": "studio"},
    )
    assert req.service_mode == "studio"


def test_service_mode_express_from_dict_job_record(monkeypatch) -> None:
    req = _call_dispatch(
        monkeypatch,
        env_value=None,
        job_record={"service_mode": "express"},
    )
    assert req.service_mode == "express"


def test_service_mode_none_when_job_record_missing_key(monkeypatch) -> None:
    req = _call_dispatch(
        monkeypatch,
        env_value=None,
        job_record={"tts_provider": "minimax"},  # no service_mode key
    )
    assert req.service_mode is None


def test_service_mode_none_when_no_job_record(monkeypatch) -> None:
    req = _call_dispatch(
        monkeypatch,
        env_value=None,
        job_record=None,
    )
    assert req.service_mode is None


# ---------------------------------------------------------------------------
# 6. service_mode from object-style job_record (JobRecord-like)
# ---------------------------------------------------------------------------

@dataclass
class _FakeJobRecord:
    service_mode: str | None = None


def test_service_mode_from_object_job_record_studio(monkeypatch) -> None:
    jr = _FakeJobRecord(service_mode="studio")
    req = _call_dispatch(monkeypatch, env_value=None, job_record=jr)
    assert req.service_mode == "studio"


def test_service_mode_from_object_job_record_none(monkeypatch) -> None:
    jr = _FakeJobRecord(service_mode=None)
    req = _call_dispatch(monkeypatch, env_value=None, job_record=jr)
    assert req.service_mode is None


# ---------------------------------------------------------------------------
# 7. Combination: env=1 + service_mode="studio"
# ---------------------------------------------------------------------------

def test_combined_env1_and_studio(monkeypatch) -> None:
    req = _call_dispatch(
        monkeypatch,
        env_value="1",
        job_record={"service_mode": "studio"},
    )
    assert req.include_jianying_draft is True
    assert req.service_mode == "studio"


def test_combined_env0_and_studio(monkeypatch) -> None:
    """Even when service_mode=studio, if env is off the flag stays False."""
    req = _call_dispatch(
        monkeypatch,
        env_value="0",
        job_record={"service_mode": "studio"},
    )
    assert req.include_jianying_draft is False
    assert req.service_mode == "studio"


# ---------------------------------------------------------------------------
# 8. OutputRequest always has PUBLISH target and correct output_dir
# ---------------------------------------------------------------------------

def test_output_request_has_publish_target(monkeypatch) -> None:
    req = _call_dispatch(monkeypatch, env_value=None)
    assert OutputTarget.PUBLISH in req.targets


def test_output_request_output_dir_set(monkeypatch) -> None:
    req = _call_dispatch(monkeypatch, env_value=None)
    # __post_init__ normalises the path; just check it is not None
    assert req.output_dir is not None

"""APF P0 T8（第一部分）— 终态结算 bypass + 匿名 consent 验证器。

断言级验收（plan AD-7/G2）：
* 匿名 job 到终态：状态字段照常镜像（PG Job 行进终态）、settle 零调用；
* 非匿名 job 行为不变（回归）；
* bypass 写在 terminal 块内部而非函数入口（G2 红线守卫）；
* validate_anonymous_consent strict-bool 矩阵。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

REPO = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import job_terminal_mirror  # noqa: E402
from anonymous_consent import validate_anonymous_consent  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _job(*, anonymous: bool, status: str = "running") -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job_anon" if anonymous else "job_user",
        status=status,
        current_stage="s5",
        project_dir="/tmp/project",
        completed_at=None,
        edit_generation=0,
        smart_state=None,
        is_anonymous_preview=anonymous,
        quota_state="none",
        service_mode="free",
        user_id="u1",
    )


def _upstream(status: str = "succeeded") -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        current_stage="completed",
        project_dir="/tmp/project",
        completed_at="2026-06-10T12:00:00Z",
        edit_generation=0,
        smart_state=None,
    )


def _patch_settles(monkeypatch) -> tuple[AsyncMock, AsyncMock]:
    quota_settle = AsyncMock()
    credit_settle = AsyncMock()
    monkeypatch.setattr(job_terminal_mirror, "settle_job_quota", quota_settle)
    monkeypatch.setattr(
        job_terminal_mirror, "settle_job_credit_ledger", credit_settle
    )
    return quota_settle, credit_settle


# --- 零结算不变量 -----------------------------------------------------------


def test_anonymous_terminal_mirrors_status_but_skips_settlement(monkeypatch):
    quota_settle, credit_settle = _patch_settles(monkeypatch)
    job = _job(anonymous=True)

    changed = _run(
        job_terminal_mirror.mirror_job_terminal_state(
            AsyncMock(), job, _upstream("succeeded")
        )
    )

    assert changed is True
    assert job.status == "succeeded"          # 状态镜像照常（G2 红线）
    assert job.completed_at == "2026-06-10T12:00:00Z"
    quota_settle.assert_not_awaited()         # 零结算
    credit_settle.assert_not_awaited()


def test_anonymous_failed_terminal_also_skips_settlement(monkeypatch):
    quota_settle, credit_settle = _patch_settles(monkeypatch)
    job = _job(anonymous=True)

    _run(
        job_terminal_mirror.mirror_job_terminal_state(
            AsyncMock(), job, _upstream("failed")
        )
    )

    assert job.status == "failed"
    quota_settle.assert_not_awaited()
    credit_settle.assert_not_awaited()


def test_non_anonymous_terminal_still_settles(monkeypatch):
    quota_settle, credit_settle = _patch_settles(monkeypatch)
    job = _job(anonymous=False)

    _run(
        job_terminal_mirror.mirror_job_terminal_state(
            AsyncMock(), job, _upstream("succeeded")
        )
    )

    assert job.status == "succeeded"
    quota_settle.assert_awaited_once()
    credit_settle.assert_awaited_once()


@pytest.mark.parametrize("bad", [1, "true", "True", {}, None])
def test_anonymous_flag_coercion_does_not_bypass(monkeypatch, bad):
    quota_settle, credit_settle = _patch_settles(monkeypatch)
    job = _job(anonymous=False)
    job.is_anonymous_preview = bad  # 非严格 True 一律照常结算

    _run(
        job_terminal_mirror.mirror_job_terminal_state(
            AsyncMock(), job, _upstream("succeeded")
        )
    )

    quota_settle.assert_awaited_once()
    credit_settle.assert_awaited_once()


def test_bypass_lives_inside_terminal_block_not_function_entry():
    src = (GATEWAY_DIR / "job_terminal_mirror.py").read_text(encoding="utf-8")
    guard_pos = src.index('getattr(db_job, "is_anonymous_preview", False)')
    terminal_pos = src.index("if upstream_status in TERMINAL_STATUSES:")
    status_mirror_pos = src.index("db_job.status = upstream_status")
    # G2 红线：bypass 必须在终态块内、状态镜像之后。
    assert guard_pos > terminal_pos > status_mirror_pos != -1


# --- 匿名 consent strict-bool 矩阵 ------------------------------------------


def test_consent_valid_payload():
    payload, reason = validate_anonymous_consent(
        {"voice_rights_confirmed": True, "client_confirmed_at": "2026-06-10T00:00:00Z"}
    )
    assert reason is None
    assert payload == {
        "voice_rights_confirmed": True,
        "client_confirmed_at": "2026-06-10T00:00:00Z",
    }


@pytest.mark.parametrize(
    "raw, expected_reason",
    [
        (None, "anonymous_consent_missing_or_invalid_type"),
        ("yes", "anonymous_consent_missing_or_invalid_type"),
        ({}, "voice_rights_not_confirmed"),
        ({"voice_rights_confirmed": False}, "voice_rights_not_confirmed"),
        ({"voice_rights_confirmed": 1}, "voice_rights_confirmed_not_bool"),
        ({"voice_rights_confirmed": "true"}, "voice_rights_confirmed_not_bool"),
        (
            {"voice_rights_confirmed": True, "client_confirmed_at": 123},
            "client_confirmed_at_not_string",
        ),
    ],
)
def test_consent_rejection_matrix(raw, expected_reason):
    payload, reason = validate_anonymous_consent(raw)
    assert payload is None
    assert reason == expected_reason


def test_consent_blank_client_timestamp_normalized_to_none():
    payload, reason = validate_anonymous_consent(
        {"voice_rights_confirmed": True, "client_confirmed_at": "   "}
    )
    assert reason is None
    assert payload["client_confirmed_at"] is None

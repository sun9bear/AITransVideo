"""Tests for pipeline _check_duration_limit with snapshot-based params (Phase 2)."""
from __future__ import annotations

import pytest
from src.pipeline.process import _check_duration_limit


class TestCheckDurationLimit:
    def test_free_plan_within_limit(self):
        """9 minutes < 10 min free limit → no error."""
        _check_duration_limit(9 * 60_000, plan_code_snapshot="free", role_snapshot="user")

    def test_free_plan_exceeds_limit(self):
        """11 minutes > 10 min free limit → raises."""
        with pytest.raises(RuntimeError, match="超出套餐上限"):
            _check_duration_limit(11 * 60_000, plan_code_snapshot="free", role_snapshot="user")

    def test_plus_plan_allows_longer(self):
        """30 minutes < 60 min plus limit → no error."""
        _check_duration_limit(30 * 60_000, plan_code_snapshot="plus", role_snapshot="user")

    def test_plus_plan_exceeds_limit(self):
        """65 minutes > 60 min plus limit → raises."""
        with pytest.raises(RuntimeError, match="超出套餐上限"):
            _check_duration_limit(65 * 60_000, plan_code_snapshot="plus", role_snapshot="user")

    def test_pro_plan_allows_long(self):
        """120 minutes < 180 min pro limit → no error."""
        _check_duration_limit(120 * 60_000, plan_code_snapshot="pro", role_snapshot="user")

    def test_pro_plan_exceeds_limit(self):
        """200 minutes > 180 min pro limit → raises."""
        with pytest.raises(RuntimeError, match="超出套餐上限"):
            _check_duration_limit(200 * 60_000, plan_code_snapshot="pro", role_snapshot="user")

    def test_admin_bypasses_all_limits(self):
        """Admin can process any duration regardless of plan_code."""
        _check_duration_limit(999 * 60_000, plan_code_snapshot="free", role_snapshot="admin")

    def test_unknown_plan_defaults_to_free_limit(self):
        """Unknown plan_code falls back to 10 min limit."""
        with pytest.raises(RuntimeError, match="超出套餐上限"):
            _check_duration_limit(11 * 60_000, plan_code_snapshot="unknown", role_snapshot="user")

    def test_defaults_to_free(self):
        """No arguments defaults to free plan, user role."""
        _check_duration_limit(9 * 60_000)
        with pytest.raises(RuntimeError):
            _check_duration_limit(11 * 60_000)

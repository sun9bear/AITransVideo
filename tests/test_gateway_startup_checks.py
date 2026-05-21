"""Tests for gateway/startup_checks.py — pure startup validators.

Guards the T6 design contract:
  1. validate_production_safety is a pure function (no side effects).
  2. env=production + auth_required=False must raise at startup.
  3. env=production + auth_required=True is allowed.
  4. Non-production envs are allowed to disable auth (dev/staging convenience).

The function lives in gateway/startup_checks.py (not main.py) specifically so
tests can import it without stubbing `database`, `auth`, or constructing the
FastAPI app. If this test needs to mock anything, something has regressed.
"""
from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import pytest

from startup_checks import (
    is_startup_recovery_schema_missing_error,
    validate_environment_name,
    validate_production_safety,
)


@pytest.mark.parametrize("env", ["dev", "test", "staging", "prod", "production", "Production"])
def test_known_environment_names_are_accepted(env):
    assert validate_environment_name(env) == env.strip().lower()


@pytest.mark.parametrize("env", ["", "prd", "local", "productionn"])
def test_unknown_environment_names_raise(env):
    with pytest.raises(RuntimeError, match="AVT_ENV must be one of"):
        validate_environment_name(env)


def test_production_with_auth_disabled_raises():
    with pytest.raises(RuntimeError, match="production requires"):
        validate_production_safety(env="production", auth_required=False)


def test_prod_alias_with_auth_disabled_raises():
    with pytest.raises(RuntimeError, match="prod requires"):
        validate_production_safety(env="prod", auth_required=False)


def test_production_with_auth_enabled_ok():
    # Should not raise.
    validate_production_safety(env="production", auth_required=True)


def test_dev_with_auth_disabled_ok():
    # Non-production envs are allowed to disable auth.
    validate_production_safety(env="dev", auth_required=False)


def test_startup_recovery_schema_missing_errors_are_expected():
    assert is_startup_recovery_schema_missing_error(
        RuntimeError("sqlite3.OperationalError: no such table: label_tasks")
    )
    assert is_startup_recovery_schema_missing_error(
        RuntimeError('psycopg.errors.UndefinedTable: relation "background_tasks" does not exist')
    )


def test_startup_recovery_unrelated_errors_are_not_expected():
    assert not is_startup_recovery_schema_missing_error(
        RuntimeError("database connection timeout")
    )

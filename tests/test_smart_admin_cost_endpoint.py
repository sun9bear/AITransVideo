"""PR#3C-P3-b — Gateway admin endpoint that serves smart_cost_summary.json.

Per decision log §2: admin-only display of cost data via
``GET /api/admin/jobs/{job_id}/cost``. User-facing workspace MUST
NOT show this data; the admin route is the single authoritative
read path.

Tests focus on:
  - Endpoint exists + admin-only auth
  - 404 for jobs that don't have cost_summary.json (non-smart,
    pre-P3-b, or jobs that hit handoff before terminal)
  - Reads + serves the JSON file content unchanged
  - User isolation: regular users get 403
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))

if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database


# ===========================================================================
# Endpoint registration
# ===========================================================================


class TestAdminCostEndpointRegistration:
    def test_endpoint_registered_on_admin_router(self):
        """``GET /api/admin/jobs/{job_id}/cost`` must be wired into
        the admin router so the Caddyfile @admin_block protects it
        + the role check is enforced consistently."""
        import admin_cost_api

        routes = [
            (r.path, r.methods)
            for r in admin_cost_api.router.routes
        ]
        cost_routes = [
            (path, methods)
            for path, methods in routes
            if "/jobs/" in path and path.endswith("/cost")
        ]
        assert cost_routes, (
            "GET /api/admin/jobs/{job_id}/cost not registered. "
            "Decision log §2: admin-only cost endpoint is the user-"
            "facing surface for cost_summary.json data.\n"
            f"Available routes: {routes}"
        )
        path, methods = cost_routes[0]
        assert path == "/api/admin/jobs/{job_id}/cost"
        assert "GET" in methods


# ===========================================================================
# Handler behaviour
# ===========================================================================


class TestAdminCostEndpointHandler:
    @pytest.mark.asyncio
    async def test_returns_404_when_file_missing(self, monkeypatch, tmp_path):
        """Non-smart jobs OR pre-P3-b jobs OR jobs that handed off
        before terminal don't have cost_summary.json. Endpoint must
        return 404 with a clear ``cost_summary_not_found`` code so
        the frontend can show "无数据" gracefully."""
        import admin_cost_api

        # Mock admin gate to pass.
        monkeypatch.setattr(
            admin_cost_api, "_require_admin", lambda user: user,
        )

        # Mock job lookup to return a record with a project_dir that
        # exists but lacks the cost_summary file.
        fake_job = MagicMock()
        project_dir = tmp_path / "project_missing_cost"
        project_dir.mkdir()
        fake_job.project_dir = str(project_dir)

        fake_db = MagicMock()
        fake_result = MagicMock()
        fake_result.scalar_one_or_none.return_value = fake_job
        fake_db.execute = AsyncMock(return_value=fake_result)

        resp = await admin_cost_api.get_smart_cost_summary(
            job_id="job_missing",
            user=MagicMock(role="admin"),
            db=fake_db,
        )
        assert resp.status_code == 404
        body = json.loads(resp.body.decode("utf-8"))
        assert body["error"] == "cost_summary_not_found"

    @pytest.mark.asyncio
    async def test_returns_200_with_file_contents_when_present(
        self, monkeypatch, tmp_path,
    ):
        """Happy path: cost_summary.json exists on disk → endpoint
        returns 200 + the parsed JSON content verbatim."""
        import admin_cost_api

        monkeypatch.setattr(
            admin_cost_api, "_require_admin", lambda user: user,
        )

        # Build a project_dir with cost_summary.json
        project_dir = tmp_path / "project_with_cost"
        audit = project_dir / "audit"
        audit.mkdir(parents=True)
        cost_payload = {
            "schema_version": 1,
            "job_id": "job_x",
            "service_mode": "smart",
            "minutes_processed": 12.5,
            "credits_charged": None,
            "credits_policy": "capture_full",
            "cost_breakdown_internal_only": {
                "asr_seconds": 45.2,
                "tts_chars": 8120,
                "voice_clone_calls": 1,
            },
            "generated_at": "2026-05-15T...",
        }
        (audit / "smart_cost_summary.json").write_text(
            json.dumps(cost_payload), encoding="utf-8",
        )

        fake_job = MagicMock()
        fake_job.project_dir = str(project_dir)

        fake_db = MagicMock()
        fake_result = MagicMock()
        fake_result.scalar_one_or_none.return_value = fake_job
        fake_db.execute = AsyncMock(return_value=fake_result)

        resp = await admin_cost_api.get_smart_cost_summary(
            job_id="job_x",
            user=MagicMock(role="admin"),
            db=fake_db,
        )
        assert resp.status_code == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body == cost_payload  # verbatim passthrough

    @pytest.mark.asyncio
    async def test_returns_404_when_job_not_found(self, monkeypatch):
        """Bad job_id → 404 ``job_not_found``."""
        import admin_cost_api

        monkeypatch.setattr(
            admin_cost_api, "_require_admin", lambda user: user,
        )

        fake_db = MagicMock()
        fake_result = MagicMock()
        fake_result.scalar_one_or_none.return_value = None
        fake_db.execute = AsyncMock(return_value=fake_result)

        resp = await admin_cost_api.get_smart_cost_summary(
            job_id="job_no_exist",
            user=MagicMock(role="admin"),
            db=fake_db,
        )
        assert resp.status_code == 404
        body = json.loads(resp.body.decode("utf-8"))
        assert body["error"] == "job_not_found"

    @pytest.mark.asyncio
    async def test_admin_gate_enforced(self):
        """Non-admin user must get 403 via _require_admin raising
        HTTPException(403). The endpoint MUST NOT serve cost data to
        non-admin even if they know a valid job_id."""
        import admin_cost_api
        from fastapi import HTTPException

        # Don't mock _require_admin — let it run with a non-admin user.
        with pytest.raises(HTTPException) as exc:
            await admin_cost_api.get_smart_cost_summary(
                job_id="any",
                user=MagicMock(role="user"),  # non-admin
                db=MagicMock(),
            )
        assert exc.value.status_code == 403


# ===========================================================================
# Frontend leakage guard
# ===========================================================================


class TestUserFacingPageNoCostLeakage:
    """Decision log §2: user-facing workspace page MUST NEVER show
    cost data. Pin that no frontend file references
    cost_summary / cost_breakdown / minimax_quota_used_after etc.
    outside of admin/ subroutes."""

    def test_no_cost_data_leak_in_workspace_frontend(self):
        """Recursive scan: workspace + features + components dirs
        must not reference cost_summary or cost_breakdown fields.
        Admin/ subroutes are explicitly allowed."""
        frontend_dir = _REPO / "frontend-next" / "src"
        if not frontend_dir.exists():
            pytest.skip("frontend-next not present")

        forbidden_outside_admin = [
            "smart_cost_summary",
            "cost_breakdown_internal_only",
            "minimax_quota_used_after",
            "credits_charged",  # cost facts on user page would leak margin
        ]

        violations = []
        for ts_file in frontend_dir.rglob("*.ts*"):
            rel = ts_file.relative_to(frontend_dir).as_posix()
            # Admin subroute is the allowed home for cost data.
            if rel.startswith("app/(app)/admin/") or rel.startswith("app/admin/"):
                continue
            text = ts_file.read_text(encoding="utf-8", errors="replace")
            for token in forbidden_outside_admin:
                if token in text:
                    violations.append((rel, token))

        assert not violations, (
            "User-facing frontend files reference admin-only cost data. "
            "Decision log §2: cost_summary fields MUST stay inside "
            "/admin/ subroutes to avoid user-visible margin leak.\n"
            f"Violations: {violations}"
        )

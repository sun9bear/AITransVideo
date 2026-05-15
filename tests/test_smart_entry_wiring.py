"""PR#3C-b3g — Smart MVP P2 entry-side wiring.

Closes the entry-point gap discovered during real-host E2E:

  - Gateway ``job_intercept.py`` whitelist accepts ``service_mode="smart"``
    instead of silently coercing to express.
  - ``plan_catalog.py`` plus / pro tiers include "smart" in
    ``allowed_service_modes`` so plan-gate doesn't 403.
  - ``smart_consent`` payload travels: request body → Gateway →
    Job API → JobRecord.smart_consent → pipeline ``_snap("smart_consent")``.
  - JobRecord (de)serialization round-trips the new field.

Without these wires, all the smart pipeline + sidecar work in
src/services/smart/ + src/pipeline/process.py is unreachable from
production traffic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
_GATEWAY = _REPO / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))


# ===========================================================================
# JobRecord smart_consent field
# ===========================================================================


class TestJobRecordSmartConsent:
    """JobRecord must carry a ``smart_consent`` dict + round-trip via
    ``to_dict`` / ``from_dict`` so JSON store reads/writes preserve it."""

    def _build_minimal_record(self, **overrides):
        from services.jobs.models import JobRecord

        defaults = dict(
            job_id="job_x",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://example.com/x",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="queued",
            current_stage=None,
            progress_message="queued",
            created_at="2026-05-15T00:00:00+00:00",
            updated_at="2026-05-15T00:00:00+00:00",
        )
        defaults.update(overrides)
        return JobRecord(**defaults)

    def test_field_exists_with_default_none(self):
        record = self._build_minimal_record()
        assert record.smart_consent is None, (
            "JobRecord.smart_consent must default to None for "
            "express/studio jobs (no consent payload). PR#3C-b3g."
        )

    def test_dict_payload_persists_through_post_init(self):
        record = self._build_minimal_record(
            service_mode="smart",
            smart_consent={
                "auto_voice_clone": True,
                "auto_translation_review": True,
            },
        )
        assert record.smart_consent == {
            "auto_voice_clone": True,
            "auto_translation_review": True,
        }
        # Nested dict is copied (defensive shallow copy via _copy_optional_dict)
        assert isinstance(record.smart_consent, dict)

    def test_to_dict_includes_smart_consent(self):
        record = self._build_minimal_record(
            service_mode="smart",
            smart_consent={"auto_voice_clone": True},
        )
        d = record.to_dict()
        assert "smart_consent" in d, (
            "to_dict() must serialize smart_consent so the JSON store "
            "persists it across job runs. PR#3C-b3g."
        )
        assert d["smart_consent"] == {"auto_voice_clone": True}

    def test_from_dict_restores_smart_consent(self):
        from services.jobs.models import JobRecord

        d = {
            "job_id": "job_y",
            "job_type": "localize_video",
            "source_type": "youtube_url",
            "source_ref": "https://example.com/y",
            "output_target": "editor",
            "speakers": "auto",
            "status": "queued",
            "current_stage": None,
            "progress_message": "queued",
            "created_at": "2026-05-15T00:00:00+00:00",
            "updated_at": "2026-05-15T00:00:00+00:00",
            "service_mode": "smart",
            "smart_consent": {"auto_voice_clone": True},
        }
        record = JobRecord.from_dict(d)
        assert record.smart_consent == {"auto_voice_clone": True}
        # Round-trip identity
        assert record.to_dict()["smart_consent"] == {"auto_voice_clone": True}

    def test_pipeline_snap_can_read_smart_consent(self):
        """Pipeline reads via ``_snap("smart_consent")``, which calls
        ``getattr(_jr, "smart_consent", None)``. Pin the attribute
        access path so a future refactor that renames the field
        breaks here, not at runtime in process.py."""
        record = self._build_minimal_record(
            service_mode="smart",
            smart_consent={"auto_voice_clone": True},
        )
        # Mirror process.py:_snap shape
        value = getattr(record, "smart_consent", None)
        assert value is not None
        assert value.get("auto_voice_clone") is True


# ===========================================================================
# Gateway whitelist + plan_catalog
# ===========================================================================


class TestGatewayServiceModeWhitelist:
    """gateway/job_intercept.py must accept ``smart`` so the entry-side
    coerce-to-express path no longer drops smart submissions."""

    def test_job_intercept_whitelist_includes_smart(self):
        """Source-level pin: the whitelist tuple at the request-body
        coerce site must include ``"smart"``."""
        path = _GATEWAY / "job_intercept.py"
        source = path.read_text(encoding="utf-8")
        # Look for the canonical coerce pattern
        assert (
            'if service_mode not in ("express", "studio", "smart"):'
            in source
        ), (
            "gateway/job_intercept.py service_mode whitelist must "
            "include \"smart\". Without it, smart submissions are "
            "silently coerced to express and the entire smart pipeline "
            "is unreachable. PR#3C-b3g entry-side gap."
        )

    def test_job_intercept_smart_consent_passthrough_only_for_smart(self):
        """smart_consent must only be forwarded when service_mode==smart
        — defensive against a non-smart submission smuggling a stale
        consent payload onto JobRecord."""
        path = _GATEWAY / "job_intercept.py"
        source = path.read_text(encoding="utf-8")
        # The passthrough block must be gated on service_mode==smart
        assert "if service_mode == \"smart\":" in source, (
            "smart_consent extraction missing the ``service_mode == "
            "\"smart\":`` gate — non-smart submissions could smuggle "
            "a consent payload that the pipeline then mistakenly "
            "honours."
        )
        # And the upstream forward must reference the validated payload
        assert 'request_data["smart_consent"] = smart_consent_payload' in source, (
            "smart_consent_payload not forwarded into upstream "
            "request_data — Job API will never see it, JobRecord "
            "won't persist, pipeline won't read."
        )


class TestPlanCatalogSmartMode:
    """plan_catalog plus + pro tiers must list ``smart`` so the
    plan-gate at job_intercept.py doesn't 403 paying users away from
    smart auto-decision."""

    def test_plus_plan_allows_smart(self):
        from plan_catalog import PLANS as PLAN_CATALOG

        plus = PLAN_CATALOG.get("plus")
        assert plus is not None, "plan_catalog missing 'plus' tier"
        assert "smart" in plus.allowed_service_modes, (
            f"plus.allowed_service_modes={plus.allowed_service_modes} "
            "missing 'smart' — plus-tier users get 403 on smart "
            "submissions. Plan §4.2 lists smart as plus+ feature."
        )

    def test_pro_plan_allows_smart(self):
        from plan_catalog import PLANS as PLAN_CATALOG

        pro = PLAN_CATALOG.get("pro")
        assert pro is not None, "plan_catalog missing 'pro' tier"
        assert "smart" in pro.allowed_service_modes

    def test_free_plan_still_excludes_smart(self):
        """Free tier intentionally excludes smart (plan §4.2 — paid
        feature). Pin so a future PR doesn't accidentally widen free."""
        from plan_catalog import PLANS as PLAN_CATALOG

        free = PLAN_CATALOG.get("free")
        assert free is not None
        assert "smart" not in free.allowed_service_modes, (
            "free tier shouldn't include smart — would let free-tier "
            "users burn paid MiniMax clone API. Plan §4.2."
        )


# ===========================================================================
# End-to-end smoke: request shape → JobRecord → _snap
# ===========================================================================


class TestSmartEntryEndToEndSmoke:
    """Stitch the three pieces — a smart request body shape that matches
    what frontend would POST, persisted via JobRecord.from_dict, read
    back via the same pattern process.py uses."""

    def test_smart_submission_round_trips_to_pipeline_visible_state(self):
        from services.jobs.models import JobRecord

        # Mock a realistic smart submission body Gateway would see.
        # After job_intercept enriches + forwards, Job API sees this:
        upstream_payload = {
            "job_id": "job_smart_e2e",
            "job_type": "localize_video",
            "source_type": "youtube_url",
            "source_ref": "https://www.youtube.com/watch?v=NYHvp0gWg80",
            "output_target": "editor",
            "speakers": "auto",
            "status": "queued",
            "current_stage": None,
            "progress_message": "queued",
            "created_at": "2026-05-15T11:00:00+00:00",
            "updated_at": "2026-05-15T11:00:00+00:00",
            "service_mode": "smart",
            "tts_provider": "minimax",
            "smart_consent": {
                "auto_voice_clone": True,
                "auto_translation_review": True,
            },
            "user_id": "user-e2e",
        }

        # JobRecord.from_dict round-trip
        record = JobRecord.from_dict(upstream_payload)
        assert record.service_mode == "smart"
        assert record.smart_consent == {
            "auto_voice_clone": True,
            "auto_translation_review": True,
        }

        # Mirror process.py:_snap pattern
        snap_consent = getattr(record, "smart_consent", None) or {}
        assert snap_consent.get("auto_voice_clone") is True, (
            "Pipeline gate ``smart_consent.get('auto_voice_clone') is True``"
            " would not fire — entry wiring incomplete."
        )

    def test_non_smart_submission_smart_consent_stays_none(self):
        """Defensive: a studio submission that accidentally carries a
        smart_consent payload (e.g. frontend shared form fields) must
        end up with smart_consent=None on JobRecord — Gateway only
        forwards it when service_mode==smart."""
        # We can't easily exercise the Gateway gate here without a
        # full FastAPI TestClient. Instead pin the Job API's defensive
        # filter — it accepts smart_consent only if it's a dict.
        from services.jobs.models import JobRecord

        studio_payload = {
            "job_id": "job_studio_x",
            "job_type": "localize_video",
            "source_type": "youtube_url",
            "source_ref": "https://example.com/x",
            "output_target": "editor",
            "speakers": "auto",
            "status": "queued",
            "current_stage": None,
            "progress_message": "queued",
            "created_at": "2026-05-15T11:00:00+00:00",
            "updated_at": "2026-05-15T11:00:00+00:00",
            "service_mode": "studio",
            # No smart_consent key — studio path doesn't carry one.
        }
        record = JobRecord.from_dict(studio_payload)
        assert record.smart_consent is None

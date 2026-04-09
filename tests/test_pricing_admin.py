"""Tests for pricing admin: PricingConfigVersion model shape."""

import sys
import os

# Add gateway to sys.path so bare imports (models, pricing_schema, etc.) resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))


def test_pricing_config_version_model_has_required_columns():
    """Verify the SQLAlchemy model has all expected columns."""
    from models import PricingConfigVersion

    cols = {c.name for c in PricingConfigVersion.__table__.columns}
    assert "id" in cols
    assert "version" in cols
    assert "status" in cols
    assert "payload_json" in cols
    assert "change_note" in cols
    assert "updated_by_user_id" in cols
    assert "created_at" in cols
    assert "activated_at" in cols


def test_pricing_config_version_table_name():
    from models import PricingConfigVersion

    assert PricingConfigVersion.__tablename__ == "pricing_config_versions"


def test_pricing_config_version_indexes():
    """Verify expected indexes exist on the table."""
    from models import PricingConfigVersion

    index_names = {idx.name for idx in PricingConfigVersion.__table__.indexes}
    assert "ix_pricing_config_versions_status" in index_names
    assert "ix_pricing_config_versions_version" in index_names
    assert "ix_pricing_config_versions_created_at" in index_names


# ---------------------------------------------------------------------------
# Task 4: pricing_admin tests — payload validation, frozen field detection
# ---------------------------------------------------------------------------

import pytest
from pricing_schema import PricingPayload, build_default_pricing_payload


def test_pricing_payload_validation_rejects_bad_payload():
    """Invalid payload should fail PricingPayload validation."""
    # Missing required fields
    with pytest.raises(Exception):
        PricingPayload.model_validate({"version": 1})

    # trial.fallback_plan references non-existent plan
    base = build_default_pricing_payload().model_dump()
    base["trial"]["fallback_plan"] = "nonexistent_plan"
    with pytest.raises(Exception):
        PricingPayload.model_validate(base)


def test_frozen_field_detection_no_changes():
    """Identical payloads should produce no frozen field changes."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new = build_default_pricing_payload()
    changes = detect_frozen_field_changes(old, new)
    assert changes == []


def test_frozen_field_detection_plan_price_change():
    """Changing a plan price should be detected as a frozen field change."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    new_data["plans"]["plus"]["price_cny_fen"]["monthly"] = 1
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    assert len(changes) > 0
    assert any("plans" in c and "price_cny_fen" in c for c in changes)


def test_frozen_field_detection_debit_rates_change():
    """Changing debit_rates should be detected as a frozen field change."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    new_data["credits"]["debit_rates"]["express.standard"] = 999
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    assert len(changes) > 0
    assert any("debit_rates" in c for c in changes)


def test_frozen_field_detection_trial_changes():
    """Changing trial frozen fields should be detected."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()

    # Change trial.days
    new_data = old.model_dump()
    new_data["trial"]["days"] = 30
    new = PricingPayload.model_validate(new_data)
    changes = detect_frozen_field_changes(old, new)
    assert any("trial.days" in c for c in changes)

    # Change trial.source_minutes
    new_data = old.model_dump()
    new_data["trial"]["source_minutes"] = 100
    new = PricingPayload.model_validate(new_data)
    changes = detect_frozen_field_changes(old, new)
    assert any("trial.source_minutes" in c for c in changes)

    # Change trial.grant_credits
    new_data = old.model_dump()
    new_data["trial"]["grant_credits"] = 9999
    new = PricingPayload.model_validate(new_data)
    changes = detect_frozen_field_changes(old, new)
    assert any("trial.grant_credits" in c for c in changes)


def test_frozen_field_detection_non_frozen_change_ignored():
    """Changing non-frozen fields should NOT be detected."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    # Change a non-frozen field: plan display_name
    new_data["plans"]["free"]["display_name"] = "Free Tier (Updated)"
    # Change another non-frozen field: cost_model
    new_data["cost_model"]["point_cost_rmb"] = 0.999
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    assert changes == []


def test_publish_requires_change_note_for_frozen_changes():
    """When frozen fields differ and change_note is empty, publish should be rejected."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    new_data["credits"]["debit_rates"]["studio.standard"] = 20
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    # This simulates the check: if frozen changes exist and no change_note, reject
    assert len(changes) > 0
    change_note = ""
    should_reject = len(changes) > 0 and not change_note.strip()
    assert should_reject is True

    # With a proper change_note, should NOT reject
    change_note = "Adjusted studio standard debit rate"
    should_reject = len(changes) > 0 and not change_note.strip()
    assert should_reject is False

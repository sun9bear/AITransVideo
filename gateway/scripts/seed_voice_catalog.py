#!/usr/bin/env python3
"""Seed voice_catalog + voice_labels from existing static catalogs.

IMPORTANT — Execution context:
    This script imports from both `gateway/` (ORM models) and `src/services/tts/`
    (static voice catalogs).  It must be run from an environment that has access
    to both directories.  The gateway Docker container does NOT have `src/` —
    run this script from the **host machine** at the repo root, or from the
    **app container** after ensuring gateway modules are importable.

    Recommended:  Run on the host machine with a DATABASE_URL pointing to the
    gateway PostgreSQL instance (same DB the gateway uses).

Usage:
    # Dry run (show what would be inserted, no DB writes):
    python gateway/scripts/seed_voice_catalog.py --dry-run

    # Actual seed (host machine, pointing to gateway DB):
    DATABASE_URL=postgresql+asyncpg://avt:PASSWORD@localhost:5432/aivideotrans \\
        python gateway/scripts/seed_voice_catalog.py

    # Or via SSH tunnel to remote DB:
    DATABASE_URL=postgresql+asyncpg://avt:PASSWORD@127.0.0.1:5432/aivideotrans \\
        python gateway/scripts/seed_voice_catalog.py

Environment variables:
    DATABASE_URL or AVT_DATABASE_URL — async PostgreSQL connection string.
    Only required for actual seed (not --dry-run).

Idempotent: skips voices that already exist in voice_catalog.
            For labels, only inserts if no is_current=True label of
            the same (voice_id, label_type, labeled_by='seed_migration') exists.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src and gateway are importable
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))
sys.path.insert(0, str(_repo_root / "gateway"))


def _load_volcengine_voices() -> list[dict]:
    """Load VolcEngine voices from static catalog."""
    from services.tts.volcengine_voice_catalog import VOICES_1_0, VOICES_2_0
    voices = []
    for v in VOICES_1_0 + VOICES_2_0:
        resource_id = v.get("resource_id", "seed-tts-1.0")
        voices.append({
            "voice_id": v["voice_id"],
            "provider": "volcengine",
            "provider_config": {"resource_id": resource_id},
            "display_name": v.get("display_name", v["voice_id"]),
            "gender": v.get("gender"),
            "language": v.get("language", "zh"),
            "scene": v.get("scene"),
            "matchable": v.get("matchable", True),
            "source": "seed_migration",
            # Seed labels inline
            "_age_group": v.get("age_group"),
            "_persona_style": v.get("persona_style"),
            "_energy_level": v.get("energy_level"),
        })
    return voices


def _load_cosyvoice_voices() -> list[dict]:
    """Load CosyVoice voices from static catalog."""
    from services.tts.cosyvoice_voice_catalog import list_cosyvoice_v3_flash_builtin_voices
    voices = []
    for v in list_cosyvoice_v3_flash_builtin_voices():
        voices.append({
            "voice_id": v["voice_id"],
            "provider": "cosyvoice",
            "provider_config": {"model": "cosyvoice-v3-flash"},
            "display_name": v.get("name", v["voice_id"]),
            "gender": v.get("gender"),
            "language": "zh",
            "scene": v.get("category"),
            "matchable": v.get("matchable", True),
            "source": "seed_migration",
            "_age_group": None,  # CosyVoice catalog doesn't have these
            "_persona_style": None,
            "_energy_level": None,
        })
    return voices


def _load_volcengine_profiles() -> dict[str, dict]:
    """Load VolcEngine Phase 3 audio profiles."""
    path = _repo_root / "src" / "services" / "tts" / "volcengine_voice_profile_data.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_cosyvoice_profiles() -> dict[str, dict]:
    """Load CosyVoice B2 profiles (may not be available locally)."""
    # Production path
    prod_path = Path("/opt/aivideotrans/data/b2_voice_profiles_final.json")
    # Local fallback
    local_path = _repo_root / "data" / "b2_voice_profiles_final.json"

    for p in [prod_path, local_path]:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


def _build_seed_plan() -> dict:
    """Build the complete seed plan (voices + labels)."""
    vc_voices = _load_volcengine_voices()
    cv_voices = _load_cosyvoice_voices()
    vc_profiles = _load_volcengine_profiles()
    cv_profiles = _load_cosyvoice_profiles()

    all_voices = vc_voices + cv_voices

    # Build text labels from inline catalog data
    text_labels = []
    for v in all_voices:
        if v["_age_group"] or v["_persona_style"] or v["_energy_level"]:
            text_labels.append({
                "voice_id": v["voice_id"],
                "label_type": "text",
                "source_run_id": "seed-catalog-inline",
                "labeled_by": "seed_migration",
                "age_group": v["_age_group"],
                "persona_style": v["_persona_style"],
                "energy_level": v["_energy_level"],
            })

    # Build audio_round1 labels from profiles
    profile_labels = []
    for vid, profile in {**vc_profiles, **cv_profiles}.items():
        profile_labels.append({
            "voice_id": vid,
            "label_type": "audio_round1",
            "source_run_id": "seed-profile-import",
            "labeled_by": "seed_migration",
            "age_group": None,
            "persona_style": None,
            "energy_level": profile.get("energy_level"),
            "pitch_level": profile.get("pitch_level"),
            "warmth": profile.get("warmth"),
            "authority": profile.get("authority"),
            "intimacy": profile.get("intimacy"),
            "brightness": profile.get("brightness"),
            "maturity": profile.get("maturity"),
            "delivery_style": profile.get("delivery_style"),
            "texture_tags": profile.get("texture_tags"),
            "childlike": profile.get("childlike"),
        })

    return {
        "voices": all_voices,
        "text_labels": text_labels,
        "profile_labels": profile_labels,
    }


def _print_plan(plan: dict) -> None:
    """Print seed plan statistics."""
    voices = plan["voices"]
    text_labels = plan["text_labels"]
    profile_labels = plan["profile_labels"]

    vc_1 = sum(1 for v in voices if v["provider"] == "volcengine" and v["provider_config"].get("resource_id") == "seed-tts-1.0")
    vc_2 = sum(1 for v in voices if v["provider"] == "volcengine" and v["provider_config"].get("resource_id") == "seed-tts-2.0")
    cv = sum(1 for v in voices if v["provider"] == "cosyvoice")

    print(f"Seed plan:")
    print(f"  voice_catalog: {len(voices)} voices")
    print(f"    VolcEngine 1.0: {vc_1}")
    print(f"    VolcEngine 2.0: {vc_2}")
    print(f"    CosyVoice: {cv}")
    print(f"  voice_labels (text): {len(text_labels)}")
    print(f"  voice_labels (audio_round1 profiles): {len(profile_labels)}")
    print(f"  Total labels: {len(text_labels) + len(profile_labels)}")


async def _execute_seed(plan: dict) -> None:
    """Execute the seed plan against the database.

    Connects directly via DATABASE_URL / AVT_DATABASE_URL, bypassing the
    gateway's config module so this script can run from the host machine.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from voice_catalog_models import VoiceCatalog, VoiceLabel

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("AVT_DATABASE_URL") or ""
    if not db_url:
        print("ERROR: DATABASE_URL or AVT_DATABASE_URL must be set for actual seed.")
        print("       Use --dry-run to preview without a database connection.")
        sys.exit(1)

    engine = create_async_engine(db_url, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    verified_status = {"default": {"verified": True, "at": now.isoformat(), "error": None}}

    async with async_session() as db:
        # Check existing voices
        existing_result = await db.execute(select(VoiceCatalog.voice_id))
        existing_ids = {row[0] for row in existing_result}
        print(f"  Existing voices in DB: {len(existing_ids)}")

        # Insert new voices
        new_voices = [v for v in plan["voices"] if v["voice_id"] not in existing_ids]
        for v in new_voices:
            db.add(VoiceCatalog(
                voice_id=v["voice_id"],
                provider=v["provider"],
                provider_config=v["provider_config"],
                display_name=v["display_name"],
                gender=v["gender"],
                language=v["language"],
                scene=v["scene"],
                matchable=v["matchable"],
                verify_status=verified_status,
                source="seed_migration",
                created_at=now,
                updated_at=now,
            ))
        print(f"  New voices to insert: {len(new_voices)}")

        # Insert labels (skip if same voice_id + label_type + seed_migration already current)
        existing_labels_result = await db.execute(
            select(VoiceLabel.voice_id, VoiceLabel.label_type)
            .where(VoiceLabel.is_current == True)  # noqa: E712
            .where(VoiceLabel.labeled_by == "seed_migration")
        )
        existing_label_keys = {(row[0], row[1]) for row in existing_labels_result}

        all_labels = plan["text_labels"] + plan["profile_labels"]
        new_labels = [lbl for lbl in all_labels if (lbl["voice_id"], lbl["label_type"]) not in existing_label_keys]
        # Also skip labels for voices not in catalog
        all_voice_ids = existing_ids | {v["voice_id"] for v in new_voices}
        new_labels = [lbl for lbl in new_labels if lbl["voice_id"] in all_voice_ids]

        for lbl in new_labels:
            db.add(VoiceLabel(
                voice_id=lbl["voice_id"],
                label_type=lbl["label_type"],
                source_run_id=lbl["source_run_id"],
                is_current=True,
                age_group=lbl.get("age_group"),
                persona_style=lbl.get("persona_style"),
                energy_level=lbl.get("energy_level"),
                pitch_level=lbl.get("pitch_level"),
                warmth=lbl.get("warmth"),
                authority=lbl.get("authority"),
                intimacy=lbl.get("intimacy"),
                brightness=lbl.get("brightness"),
                maturity=lbl.get("maturity"),
                delivery_style=lbl.get("delivery_style"),
                texture_tags=lbl.get("texture_tags"),
                childlike=lbl.get("childlike"),
                labeled_by="seed_migration",
                labeled_at=now,
            ))
        print(f"  New labels to insert: {len(new_labels)}")

        await db.commit()
        print("  Committed.")


def main():
    parser = argparse.ArgumentParser(description="Seed voice_catalog + voice_labels")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing to DB")
    args = parser.parse_args()

    plan = _build_seed_plan()
    _print_plan(plan)

    if args.dry_run:
        print("\n[DRY RUN] No database writes performed.")
        return

    print("\nExecuting seed...")
    asyncio.run(_execute_seed(plan))
    print("Done.")


if __name__ == "__main__":
    main()

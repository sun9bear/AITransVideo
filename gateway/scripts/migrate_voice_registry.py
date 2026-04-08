"""One-time migration: copy cloned voices from voice_registry.json to user_voices table.

Run inside the gateway container AFTER migration 010 has been applied:
    docker exec aivideotrans-gateway python3 scripts/migrate_voice_registry.py

Reads voice_registry.json from the app container's project root, extracts all
cloned MiniMax voices, and inserts them for every existing user.
"""

import asyncio
import json
import sys
from pathlib import Path

# Add gateway root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import async_session
from models import UserVoice, User
from sqlalchemy import select


REGISTRY_PATHS = [
    Path("/opt/aivideotrans/app/voice_registry.json"),
    Path("/opt/aivideotrans/config/voice_registry.json"),
]


async def migrate():
    # Find voice_registry.json
    registry_path = None
    for p in REGISTRY_PATHS:
        if p.exists():
            registry_path = p
            break

    if registry_path is None:
        print("voice_registry.json not found at any expected path, skipping migration.")
        return

    print(f"Reading {registry_path}")
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    speakers = data.get("speakers", {})

    # Extract cloned voices
    cloned_voices = []
    for speaker_id, speaker_data in speakers.items():
        if not isinstance(speaker_data, dict):
            continue
        speaker_name = speaker_data.get("speaker_name", speaker_id)
        for voice in speaker_data.get("voices", []):
            if not isinstance(voice, dict):
                continue
            voice_type = voice.get("voice_type", "")
            if voice_type != "cloned":
                continue
            voice_id = voice.get("voice_id", "")
            if not voice_id:
                continue
            cloned_voices.append({
                "voice_id": voice_id,
                "label": voice.get("label", f"{speaker_name} Clone"),
                "provider": voice.get("provider", "minimax_voice_clone"),
                "tts_provider": voice.get("tts_provider", "minimax_tts"),
                "platform": voice.get("platform", "minimax_domestic"),
                "source_speaker_id": speaker_id,
                "notes": voice.get("notes"),
            })

    print(f"Found {len(cloned_voices)} cloned voices")
    if not cloned_voices:
        print("No cloned voices to migrate.")
        return

    async with async_session() as db:
        # Get all users
        result = await db.execute(select(User.id))
        user_ids = [row[0] for row in result.all()]
        print(f"Found {len(user_ids)} users")

        inserted = 0
        for user_id in user_ids:
            for voice in cloned_voices:
                # Check if already exists
                existing = await db.execute(
                    select(UserVoice).where(
                        UserVoice.user_id == user_id,
                        UserVoice.voice_id == voice["voice_id"],
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    continue

                db.add(UserVoice(
                    user_id=user_id,
                    voice_id=voice["voice_id"],
                    label=voice["label"],
                    provider=voice["provider"],
                    tts_provider=voice["tts_provider"],
                    platform=voice["platform"],
                    source_speaker_id=voice["source_speaker_id"],
                    notes=voice["notes"],
                ))
                inserted += 1

        await db.commit()
        print(f"Inserted {inserted} voice entries across {len(user_ids)} users")


if __name__ == "__main__":
    asyncio.run(migrate())

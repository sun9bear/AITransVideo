#!/usr/bin/env python3
"""Merge Phase 2 Gemini labels into volcengine_voice_catalog.py source code.

Reads the current catalog module, applies Gemini labels where available,
and writes the updated module back. Only modifies age_group, persona_style,
energy_level fields — does not touch voice_id, gender, or other fields.

Also saves Phase 3 profiles as a separate JSON file for future rerank.
"""
import json
import os
import re
import sys

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "services", "tts", "volcengine_voice_catalog.py")
LABELS_PATH = os.path.join(os.path.dirname(__file__), "..", ".tmp-vc-labels.json")
PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", ".tmp-vc-profiles.json")
PROFILES_OUTPUT = os.path.join(os.path.dirname(__file__), "..", "src", "services", "tts", "volcengine_voice_profile_data.json")


def load_json_with_prefix(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    # SSH cat may prepend proxy status lines before the JSON
    idx = raw.find("{")
    if idx < 0:
        return {}
    return json.loads(raw[idx:])


def main():
    labels = load_json_with_prefix(LABELS_PATH)
    print(f"Loaded {len(labels)} Phase 2 labels")

    profiles = load_json_with_prefix(PROFILES_PATH)
    print(f"Loaded {len(profiles)} Phase 3 profiles")

    # Read catalog source
    with open(CATALOG_PATH, encoding="utf-8") as f:
        source = f.read()

    # For each label, find the _v() call with matching voice_id and patch fields
    updated = 0
    for vid, lbl in labels.items():
        age = lbl.get("age_group", "")
        persona = lbl.get("persona_style", "")
        energy = lbl.get("energy_level", "")
        if not age and not persona and not energy:
            continue

        # Find the _v() line containing this voice_id
        # Pattern: _v("voice_id", "name", _R1, "gender", "age", "persona", "energy", ...)
        escaped_vid = re.escape(vid)
        pattern = rf'(_v\("{escaped_vid}",\s*"[^"]*",\s*_R[12],\s*"[^"]*",\s*)"([^"]*)",\s*"([^"]*)",\s*"([^"]*)"'

        def replacer(m):
            prefix = m.group(1)
            old_age = m.group(2)
            old_persona = m.group(3)
            old_energy = m.group(4)
            new_age = age if age else old_age
            new_persona = persona if persona else old_persona
            new_energy = energy if energy else old_energy
            return f'{prefix}"{new_age}", "{new_persona}", "{new_energy}"'

        new_source, count = re.subn(pattern, replacer, source)
        if count > 0:
            source = new_source
            updated += count

    print(f"Patched {updated} _v() entries in catalog")

    # Write updated catalog
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        f.write(source)
    print(f"Written to {CATALOG_PATH}")

    # Save Phase 3 profiles as standalone JSON
    with open(PROFILES_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    print(f"Phase 3 profiles saved to {PROFILES_OUTPUT}")


if __name__ == "__main__":
    main()

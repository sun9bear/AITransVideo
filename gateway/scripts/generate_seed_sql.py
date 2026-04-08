#!/usr/bin/env python3
"""Generate seed SQL from static catalogs. Output can be piped to psql.

Usage:
    python gateway/scripts/generate_seed_sql.py > /tmp/seed.sql
    # Then on server:
    docker exec -i aivideotrans-postgres psql -U avt -d aivideotrans < /tmp/seed.sql
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from services.tts.volcengine_voice_catalog import VOICES_1_0, VOICES_2_0
from services.tts.cosyvoice_voice_catalog import list_cosyvoice_v3_flash_builtin_voices


def _sql_str(v) -> str:
    if v is None:
        return "NULL"
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _sql_json(v) -> str:
    if v is None:
        return "NULL"
    return f"'{json.dumps(v, ensure_ascii=False).replace(chr(39), chr(39)+chr(39))}'"


def main():
    now = datetime.now(timezone.utc).isoformat()
    verified = json.dumps({"default": {"verified": True, "at": now, "error": None}})

    lines = ["BEGIN;", ""]

    # --- voice_catalog ---
    lines.append("-- VolcEngine voices")
    for v in VOICES_1_0 + VOICES_2_0:
        rid = v.get("resource_id", "seed-tts-1.0")
        pc = json.dumps({"resource_id": rid})
        matchable_sql = "true" if v.get("matchable", True) else "false"
        lines.append(
            f"INSERT INTO voice_catalog (voice_id, provider, provider_config, display_name, gender, language, scene, matchable, verify_status, source, created_at, updated_at) "
            f"VALUES ({_sql_str(v['voice_id'])}, 'volcengine', '{pc}', {_sql_str(v.get('display_name', v['voice_id']))}, "
            f"{_sql_str(v.get('gender'))}, {_sql_str(v.get('language', 'zh'))}, {_sql_str(v.get('scene'))}, "
            f"{matchable_sql}, '{verified}', 'seed_migration', NOW(), NOW()) "
            f"ON CONFLICT (voice_id) DO NOTHING;"
        )

    lines.append("")
    lines.append("-- CosyVoice voices")
    for v in list_cosyvoice_v3_flash_builtin_voices():
        pc = json.dumps({"model": "cosyvoice-v3-flash"})
        matchable_sql = "true" if v.get("matchable", True) else "false"
        lines.append(
            f"INSERT INTO voice_catalog (voice_id, provider, provider_config, display_name, gender, language, scene, matchable, verify_status, source, created_at, updated_at) "
            f"VALUES ({_sql_str(v['voice_id'])}, 'cosyvoice', '{pc}', {_sql_str(v.get('name', v['voice_id']))}, "
            f"{_sql_str(v.get('gender'))}, 'zh', {_sql_str(v.get('category'))}, "
            f"{matchable_sql}, '{verified}', 'seed_migration', NOW(), NOW()) "
            f"ON CONFLICT (voice_id) DO NOTHING;"
        )

    # --- MiniMax voices ---
    lines.append("")
    lines.append("-- MiniMax voices")
    mm_catalog_path = Path(__file__).resolve().parent.parent.parent / "src" / "services" / "tts" / "minimax_voice_catalog_604.json"
    mm_voices = []
    if mm_catalog_path.exists():
        mm_voices = json.loads(mm_catalog_path.read_text(encoding="utf-8"))
        # Import trait mapping from seed_voice_catalog
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from scripts.seed_voice_catalog import (
            _infer_from_traits, _infer_texture_from_traits,
            _MM_PERSONA_MAP, _MM_ENERGY_MAP, _MM_PITCH_MAP, _MM_DELIVERY_MAP, _MM_MATURITY_MAP,
        )
        matchable_langs = {"中文-普通话", "中文-粤语", "英语"}
        for v in mm_voices:
            vid = v["voice_id"]
            pc = json.dumps({"model": "speech-02-hd", "accent": v.get("accent", "")}, ensure_ascii=False)
            lang = v.get("language", "")
            matchable_sql = "true" if lang in matchable_langs else "false"
            display = v.get("name", vid)
            desc = v.get("description", "")
            lines.append(
                f"INSERT INTO voice_catalog (voice_id, provider, provider_config, display_name, gender, language, scene, matchable, verify_status, source, notes, created_at, updated_at) "
                f"VALUES ({_sql_str(vid)}, 'minimax', {_sql_str(pc)}::jsonb, {_sql_str(display)}, "
                f"{_sql_str(v.get('gender'))}, {_sql_str(lang)}, {_sql_str(', '.join(v.get('scene', [])))}, "
                f"{matchable_sql}, '{verified}', 'seed_migration', {_sql_str(desc)}, NOW(), NOW()) "
                f"ON CONFLICT (voice_id) DO NOTHING;"
            )

        # MiniMax text + profile labels from trait mapping
        lines.append("")
        lines.append("-- MiniMax labels (trait-mapped)")
        for v in mm_voices:
            vid = v["voice_id"]
            traits = v.get("traits", [])
            desc = v.get("description", "")
            age = v.get("age_group", "")
            persona = _infer_from_traits(traits, desc, _MM_PERSONA_MAP)
            energy = _infer_from_traits(traits, desc, _MM_ENERGY_MAP)
            pitch = _infer_from_traits(traits, desc, _MM_PITCH_MAP)
            delivery = _infer_from_traits(traits, desc, _MM_DELIVERY_MAP)
            texture = _infer_texture_from_traits(traits, desc)
            maturity = _MM_MATURITY_MAP.get(age, "adult") if age else ""
            childlike = "true" if age == "child" else "false"
            texture_sql = _sql_json(texture) if texture else "NULL"
            if age or persona or energy or pitch or delivery or maturity:
                lines.append(
                    f"INSERT INTO voice_labels (voice_id, label_type, source_run_id, is_current, "
                    f"age_group, persona_style, energy_level, pitch_level, maturity, delivery_style, texture_tags, childlike, "
                    f"labeled_by, labeled_at) "
                    f"SELECT {_sql_str(vid)}, 'final', 'seed-minimax-trait-mapping', true, "
                    f"{_sql_str(age)}, {_sql_str(persona)}, {_sql_str(energy)}, "
                    f"{_sql_str(pitch)}, {_sql_str(maturity)}, {_sql_str(delivery)}, "
                    f"{texture_sql}, {childlike}, "
                    f"'seed_migration', NOW() "
                    f"WHERE EXISTS (SELECT 1 FROM voice_catalog WHERE voice_id = {_sql_str(vid)}) "
                    f"AND NOT EXISTS (SELECT 1 FROM voice_labels WHERE voice_id = {_sql_str(vid)} AND label_type = 'text' AND labeled_by = 'seed_migration' AND is_current = true);"
                )

    # --- voice_labels: text labels from VolcEngine catalog ---
    lines.append("")
    lines.append("-- VolcEngine text labels (seed)")
    for v in VOICES_1_0 + VOICES_2_0:
        ag = v.get("age_group")
        ps = v.get("persona_style")
        el = v.get("energy_level")
        if ag or ps or el:
            lines.append(
                f"INSERT INTO voice_labels (voice_id, label_type, source_run_id, is_current, age_group, persona_style, energy_level, labeled_by, labeled_at) "
                f"SELECT {_sql_str(v['voice_id'])}, 'text', 'seed-catalog-inline', true, {_sql_str(ag)}, {_sql_str(ps)}, {_sql_str(el)}, 'seed_migration', NOW() "
                f"WHERE NOT EXISTS (SELECT 1 FROM voice_labels WHERE voice_id = {_sql_str(v['voice_id'])} AND label_type = 'text' AND labeled_by = 'seed_migration' AND is_current = true);"
            )

    # --- voice_labels: VolcEngine audio profiles ---
    profile_path = Path(__file__).resolve().parent.parent.parent / "src" / "services" / "tts" / "volcengine_voice_profile_data.json"
    if profile_path.exists():
        profiles = json.loads(profile_path.read_text(encoding="utf-8"))
        lines.append("")
        lines.append("-- VolcEngine audio profiles (seed)")
        for vid, p in profiles.items():
            tags_raw = p.get("texture_tags")
            if tags_raw:
                # JSONB requires double-quoted strings — use json.dumps for proper format
                tags_sql = "'" + json.dumps(tags_raw, ensure_ascii=False).replace("'", "''") + "'"
            else:
                tags_sql = "NULL"
            childlike = str(p.get("childlike", False)).lower() if p.get("childlike") is not None else "NULL"
            lines.append(
                f"INSERT INTO voice_labels (voice_id, label_type, source_run_id, is_current, "
                f"energy_level, pitch_level, warmth, authority, intimacy, brightness, maturity, delivery_style, texture_tags, childlike, "
                f"labeled_by, labeled_at) "
                f"SELECT {_sql_str(vid)}, 'audio_round1', 'seed-profile-import', true, "
                f"{_sql_str(p.get('energy_level'))}, {_sql_str(p.get('pitch_level'))}, {_sql_str(p.get('warmth'))}, "
                f"{_sql_str(p.get('authority'))}, {_sql_str(p.get('intimacy'))}, {_sql_str(p.get('brightness'))}, "
                f"{_sql_str(p.get('maturity'))}, {_sql_str(p.get('delivery_style'))}, "
                f"{tags_sql}, "
                f"{childlike}, "
                f"'seed_migration', NOW() "
                f"WHERE EXISTS (SELECT 1 FROM voice_catalog WHERE voice_id = {_sql_str(vid)}) "
                f"AND NOT EXISTS (SELECT 1 FROM voice_labels WHERE voice_id = {_sql_str(vid)} AND label_type = 'audio_round1' AND labeled_by = 'seed_migration' AND is_current = true);"
            )

    lines.append("")
    lines.append("COMMIT;")
    lines.append("")

    for line in lines:
        print(line)

    # Stats to stderr
    vc_count = len(VOICES_1_0) + len(VOICES_2_0)
    cv_count = len(list_cosyvoice_v3_flash_builtin_voices())
    mm_count = len(mm_voices)
    text_labels = sum(1 for v in VOICES_1_0 + VOICES_2_0 if v.get("age_group") or v.get("persona_style") or v.get("energy_level"))
    prof_count = len(profiles) if profile_path.exists() else 0
    print(f"-- Stats: {vc_count + cv_count + mm_count} voices (VC:{vc_count} CV:{cv_count} MM:{mm_count}), "
          f"{text_labels} VC text labels, {prof_count} VC audio profiles, {mm_count} MM trait labels", file=sys.stderr)


if __name__ == "__main__":
    main()

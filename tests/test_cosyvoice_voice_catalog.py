from services.tts.cosyvoice_voice_catalog import (
    _COSYVOICE_V3_FLASH_VOICES,
    get_cosyvoice_v3_flash_builtin_voice,
    is_cosyvoice_v3_flash_builtin_voice,
    list_cosyvoice_v3_flash_builtin_voices,
    list_matchable_cosyvoice_voices,
)


def test_all_voices_have_gender_field() -> None:
    for v in _COSYVOICE_V3_FLASH_VOICES:
        assert "gender" in v, f"{v['voice_id']} missing gender"
        assert v["gender"] in ("male", "female", "child"), f"{v['voice_id']} invalid gender={v['gender']}"


def test_all_voices_have_matchable_field() -> None:
    for v in _COSYVOICE_V3_FLASH_VOICES:
        assert "matchable" in v, f"{v['voice_id']} missing matchable"
        assert isinstance(v["matchable"], bool), f"{v['voice_id']} matchable not bool"


def test_dialect_voices_not_matchable() -> None:
    dialect = [v for v in _COSYVOICE_V3_FLASH_VOICES if v["category"] == "方言"]
    assert len(dialect) == 6
    for v in dialect:
        assert v["matchable"] is False, f"dialect voice {v['voice_id']} should not be matchable"


def test_overseas_voices_not_matchable() -> None:
    overseas = [v for v in _COSYVOICE_V3_FLASH_VOICES if v["category"] == "出海营销"]
    assert len(overseas) == 3
    for v in overseas:
        assert v["matchable"] is False, f"overseas voice {v['voice_id']} should not be matchable"


def test_character_voices_not_matchable() -> None:
    """角色/拟人音色（猴哥/机器人/黛玉）不得进自动匹配池——reranker 会把它们对普通成年
    男声/女声画像打高分，导致 Joe Rogan 这类视频被错配成孙悟空风音色（线上事故根因）。
    本断言与 DB seed（seed_voice_catalog.py 从本静态文件 seed）保持同源，防 re-seed 复活。"""
    character_ids = {"longhouge_v3", "longjiqi_v3", "longdaiyu_v3"}
    by_id = {v["voice_id"]: v for v in _COSYVOICE_V3_FLASH_VOICES}
    for vid in character_ids:
        assert vid in by_id, f"character voice {vid} missing from catalog"
        assert by_id[vid]["matchable"] is False, f"character voice {vid} must not be matchable"
        assert by_id[vid]["category"] == "短视频配音"
    # And they must be absent from the active matching pool helper.
    matchable_ids = {v["voice_id"] for v in list_matchable_cosyvoice_voices()}
    assert character_ids.isdisjoint(matchable_ids)


def test_matchable_count() -> None:
    matchable = list_matchable_cosyvoice_voices()
    non_matchable = [v for v in _COSYVOICE_V3_FLASH_VOICES if not v.get("matchable", True)]
    # 6 dialect + 3 overseas + 3 character (短视频配音) = 12 non-matchable.
    assert len(non_matchable) == 12
    assert len(matchable) == len(_COSYVOICE_V3_FLASH_VOICES) - 12


def test_list_matchable_excludes_non_matchable() -> None:
    matchable = list_matchable_cosyvoice_voices()
    ids = {v["voice_id"] for v in matchable}
    assert "longjiaxin_v3" not in ids
    assert "loongkyong_v3" not in ids
    assert "longanyang" in ids
    assert "longanhuan" in ids


def test_total_catalog_size() -> None:
    assert len(_COSYVOICE_V3_FLASH_VOICES) == 68


def test_existing_lookup_backward_compat() -> None:
    v = get_cosyvoice_v3_flash_builtin_voice("longanyang")
    assert v is not None
    assert v["voice_id"] == "longanyang"
    assert is_cosyvoice_v3_flash_builtin_voice("longanyang")
    assert not is_cosyvoice_v3_flash_builtin_voice("nonexistent_voice")


def test_list_all_backward_compat() -> None:
    all_voices = list_cosyvoice_v3_flash_builtin_voices()
    assert len(all_voices) == 68

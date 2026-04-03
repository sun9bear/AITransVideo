"""Official Aliyun DashScope builtin voice catalog for cosyvoice-v3-flash.

Source:
https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list
"""

from __future__ import annotations

from typing import Final


COSYVOICE_V3_FLASH_MODEL: Final[str] = "cosyvoice-v3-flash"
COSYVOICE_PROVIDER: Final[str] = "aliyun"
COSYVOICE_TTS_PROVIDER: Final[str] = "cosyvoice"
COSYVOICE_PLATFORM: Final[str] = "dashscope"
COSYVOICE_VOICE_LIST_SOURCE_URL: Final[str] = (
    "https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list"
)

_COSYVOICE_V3_FLASH_VOICES: tuple[dict[str, str | bool], ...] = (
    # --- 标杆音色 ---
    {"voice_id": "longanyang", "name": "龙安洋", "category": "社交陪伴（标杆音色）", "traits": "阳光大男孩", "age": "20~30", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longanhuan", "name": "龙安欢", "category": "社交陪伴（标杆音色）", "traits": "欢脱元气女", "age": "20~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    # --- 童声 ---
    {"voice_id": "longhuhu_v3", "name": "龙呼呼", "category": "童声（标杆音色）", "traits": "天真烂漫女童", "age": "6~10", "language": "中文（普通话）、英文", "gender": "child", "matchable": True},
    {"voice_id": "longpaopao_v3", "name": "龙泡泡", "category": "智能玩具/儿童故事机", "traits": "飞天泡泡音", "age": "6~15", "language": "中文（普通话）、英文", "gender": "child", "matchable": True},
    {"voice_id": "longjielidou_v3", "name": "龙杰力豆", "category": "智能玩具/儿童故事机", "traits": "阳光顽皮男", "age": "10", "language": "中文（普通话）、英文", "gender": "child", "matchable": True},
    {"voice_id": "longxian_v3", "name": "龙仙", "category": "智能玩具/儿童故事机", "traits": "豪放可爱女", "age": "12", "language": "中文（普通话）、英文", "gender": "child", "matchable": True},
    {"voice_id": "longling_v3", "name": "龙铃", "category": "智能玩具/儿童故事机", "traits": "稚气呆板女", "age": "10", "language": "中文（普通话）、英文", "gender": "child", "matchable": True},
    {"voice_id": "longshanshan_v3", "name": "龙闪闪", "category": "消费电子-儿童有声书", "traits": "戏剧化童声", "age": "6~15", "language": "中文（普通话）、英文", "gender": "child", "matchable": True},
    {"voice_id": "longniuniu_v3", "name": "龙牛牛", "category": "消费电子-儿童有声书", "traits": "阳光男童声", "age": "6~15", "language": "中文（普通话）、英文", "gender": "child", "matchable": True},
    # --- 方言 (matchable=False) ---
    {"voice_id": "longjiaxin_v3", "name": "龙嘉欣", "category": "方言", "traits": "优雅粤语女", "age": "30~35", "language": "中文（粤语）、英", "gender": "female", "matchable": False},
    {"voice_id": "longjiayi_v3", "name": "龙嘉怡", "category": "方言", "traits": "知性粤语女", "age": "25~30", "language": "中文（粤语）、英", "gender": "female", "matchable": False},
    {"voice_id": "longanyue_v3", "name": "龙安粤", "category": "方言", "traits": "欢脱粤语男", "age": "25~35", "language": "中文（粤语）、英文", "gender": "male", "matchable": False},
    {"voice_id": "longlaotie_v3", "name": "龙老铁", "category": "方言", "traits": "东北直率男", "age": "25~30", "language": "中文（东北话）、英", "gender": "male", "matchable": False},
    {"voice_id": "longshange_v3", "name": "龙陕哥", "category": "方言", "traits": "原味陕北男", "age": "25~35", "language": "中文（陕西话）、英文", "gender": "male", "matchable": False},
    {"voice_id": "longanmin_v3", "name": "龙安闽", "category": "方言", "traits": "清纯萝莉女", "age": "18~25", "language": "中文（闽南话）、英文", "gender": "female", "matchable": False},
    # --- 出海营销 (matchable=False) ---
    {"voice_id": "loongkyong_v3", "name": "loongkyong", "category": "出海营销", "traits": "韩语女", "age": "25~30", "language": "韩语", "gender": "female", "matchable": False},
    {"voice_id": "loongriko_v3", "name": "Riko", "category": "出海营销", "traits": "二次元霓虹女", "age": "18~25", "language": "日语", "gender": "female", "matchable": False},
    {"voice_id": "loongtomoka_v3", "name": "loongtomoka", "category": "出海营销", "traits": "日语女", "age": "30~35", "language": "日语", "gender": "female", "matchable": False},
    # --- 诗词朗诵 / 电话销售 / 客服 ---
    {"voice_id": "longfei_v3", "name": "龙飞", "category": "诗词朗诵", "traits": "热血磁性男", "age": "30~35", "language": "中文（普通话）、英", "gender": "male", "matchable": True},
    {"voice_id": "longyingxiao_v3", "name": "龙应笑", "category": "电话销售", "traits": "清甜推销女", "age": "20~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longyingxun_v3", "name": "龙应询", "category": "客服", "traits": "年轻青涩男", "age": "20~25", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longyingjing_v3", "name": "龙应静", "category": "客服", "traits": "低调冷静女", "age": "25~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longyingling_v3", "name": "龙应聆", "category": "客服", "traits": "温和共情女", "age": "25~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longyingtao_v3", "name": "龙应桃", "category": "客服", "traits": "温柔淡定女", "age": "25~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    # --- 语音助手 ---
    {"voice_id": "longxiaochun_v3", "name": "龙小淳", "category": "语音助手", "traits": "知性积极女", "age": "25~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longxiaoxia_v3", "name": "龙小夏", "category": "语音助手", "traits": "沉稳权威女", "age": "25~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longyumi_v3", "name": "YUMI", "category": "语音助手", "traits": "正经青年女", "age": "20~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longanyun_v3", "name": "龙安昀", "category": "语音助手", "traits": "居家暖男", "age": "30~35", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longanwen_v3", "name": "龙安温", "category": "语音助手", "traits": "优雅知性女", "age": "25~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longanli_v3", "name": "龙安莉", "category": "语音助手", "traits": "利落从容女", "age": "25~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longanlang_v3", "name": "龙安朗", "category": "语音助手", "traits": "清爽利落男", "age": "20~25", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longyingmu_v3", "name": "龙应沐", "category": "语音助手", "traits": "优雅知性女", "age": "25~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    # --- 社交陪伴 ---
    {"voice_id": "longantai_v3", "name": "龙安台", "category": "社交陪伴", "traits": "嗲甜台湾女", "age": "20~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longhua_v3", "name": "龙华", "category": "社交陪伴", "traits": "元气甜美女", "age": "20~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longcheng_v3", "name": "龙橙", "category": "社交陪伴", "traits": "智慧青年男", "age": "20~25", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longze_v3", "name": "龙泽", "category": "社交陪伴", "traits": "温暖元气男", "age": "25~30", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longzhe_v3", "name": "龙哲", "category": "社交陪伴", "traits": "呆板大暖男", "age": "25~30", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longyan_v3", "name": "龙颜", "category": "社交陪伴", "traits": "温暖春风女", "age": "30~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longxing_v3", "name": "龙星", "category": "社交陪伴", "traits": "温婉邻家女", "age": "20~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longtian_v3", "name": "龙天", "category": "社交陪伴", "traits": "磁性理智男", "age": "30~35", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longwan_v3", "name": "龙婉", "category": "社交陪伴", "traits": "细腻柔声女", "age": "20~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longqiang_v3", "name": "龙嫱", "category": "社交陪伴", "traits": "浪漫风情女", "age": "30~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longfeifei_v3", "name": "龙菲菲", "category": "社交陪伴", "traits": "甜美娇气女", "age": "20~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longhao_v3", "name": "龙浩", "category": "社交陪伴", "traits": "多情忧郁男", "age": "30~35", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longanrou_v3", "name": "龙安柔", "category": "社交陪伴", "traits": "温柔闺蜜女", "age": "20~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longhan_v3", "name": "龙寒", "category": "社交陪伴", "traits": "温暖痴情男", "age": "30~35", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longanzhi_v3", "name": "龙安智", "category": "社交陪伴", "traits": "睿智轻熟男", "age": "25~35", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longanling_v3", "name": "龙安灵", "category": "社交陪伴", "traits": "思维灵动女", "age": "20~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longanya_v3", "name": "龙安雅", "category": "社交陪伴", "traits": "高雅气质女", "age": "25~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longanqin_v3", "name": "龙安亲", "category": "社交陪伴", "traits": "亲和活泼女", "age": "20~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    # --- 有声书 ---
    {"voice_id": "longmiao_v3", "name": "龙妙", "category": "有声书", "traits": "抑扬顿挫女", "age": "25~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longsanshu_v3", "name": "龙三叔", "category": "有声书", "traits": "沉稳质感男", "age": "25~45", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longyuan_v3", "name": "龙媛", "category": "有声书", "traits": "温暖治愈女", "age": "35~40", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longyue_v3", "name": "龙悦", "category": "有声书", "traits": "温暖磁性女", "age": "30~35", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longxiu_v3", "name": "龙修", "category": "有声书", "traits": "博才说书男", "age": "25~35", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longnan_v3", "name": "龙楠", "category": "有声书", "traits": "睿智青年男", "age": "25~30", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longwanjun_v3", "name": "龙婉君", "category": "有声书", "traits": "细腻柔声女", "age": "20~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longyichen_v3", "name": "龙逸尘", "category": "有声书", "traits": "洒脱活力男", "age": "20~30", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longlaobo_v3", "name": "龙老伯", "category": "有声书", "traits": "沧桑岁月爷", "age": "60", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longlaoyi_v3", "name": "龙老姨", "category": "有声书", "traits": "烟火从容阿姨", "age": "60", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    # --- 短视频配音 ---
    {"voice_id": "longjiqi_v3", "name": "龙机器", "category": "短视频配音", "traits": "呆萌机器人", "age": "20~30", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longhouge_v3", "name": "龙猴哥", "category": "短视频配音", "traits": "经典猴哥", "age": "20~25", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longdaiyu_v3", "name": "龙黛玉", "category": "短视频配音", "traits": "娇率才女音", "age": "15~25", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    # --- 直播带货 ---
    {"voice_id": "longanran_v3", "name": "龙安燃", "category": "直播带货", "traits": "活泼质感女", "age": "30~40", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    {"voice_id": "longanxuan_v3", "name": "龙安宣", "category": "直播带货", "traits": "经典直播女", "age": "30~40", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
    # --- 新闻播报 ---
    {"voice_id": "longshuo_v3", "name": "龙硕", "category": "新闻播报", "traits": "博才干练男", "age": "25~30", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "longshu_v3", "name": "龙书", "category": "新闻播报", "traits": "沉稳青年男", "age": "20~25", "language": "中文（普通话）、英文", "gender": "male", "matchable": True},
    {"voice_id": "loongbella_v3", "name": "Bella3.0", "category": "新闻播报", "traits": "精准干练女", "age": "25~30", "language": "中文（普通话）、英文", "gender": "female", "matchable": True},
)

_COSYVOICE_V3_FLASH_LOOKUP: Final[dict[str, dict[str, str]]] = {
    item["voice_id"]: item for item in _COSYVOICE_V3_FLASH_VOICES
}


def list_cosyvoice_v3_flash_builtin_voices() -> list[dict[str, str]]:
    return [dict(item) for item in _COSYVOICE_V3_FLASH_VOICES]


def get_cosyvoice_v3_flash_builtin_voice(voice_id: str | None) -> dict[str, str] | None:
    normalized_voice_id = str(voice_id or "").strip()
    if not normalized_voice_id:
        return None
    entry = _COSYVOICE_V3_FLASH_LOOKUP.get(normalized_voice_id)
    return dict(entry) if entry is not None else None


def is_cosyvoice_v3_flash_builtin_voice(voice_id: str | None) -> bool:
    return get_cosyvoice_v3_flash_builtin_voice(voice_id) is not None


def build_cosyvoice_v3_flash_builtin_voice_option(voice_id: str) -> dict[str, object] | None:
    entry = get_cosyvoice_v3_flash_builtin_voice(voice_id)
    if entry is None:
        return None
    note = (
        f"Official Aliyun DashScope {COSYVOICE_V3_FLASH_MODEL} builtin voice. "
        f"Category: {entry['category']}; Traits: {entry['traits']}; "
        f"Age: {entry['age']}; Language: {entry['language']}; "
        f"Source: {COSYVOICE_VOICE_LIST_SOURCE_URL}"
    )
    return {
        "voice_id": entry["voice_id"],
        "speaker_id": f"cosyvoice_v3_flash::{entry['voice_id']}",
        "speaker_name": f"CosyVoice v3 Flash / {entry['category']}",
        "label": entry["name"],
        "provider": COSYVOICE_PROVIDER,
        "tts_provider": COSYVOICE_TTS_PROVIDER,
        "platform": COSYVOICE_PLATFORM,
        "voice_type": "builtin",
        "created_at": None,
        "verification_status": "verified",
        "notes": note,
        "category": entry["category"],
        "traits": entry["traits"],
        "age": entry["age"],
        "language": entry["language"],
        "catalog_model": COSYVOICE_V3_FLASH_MODEL,
        "source_url": COSYVOICE_VOICE_LIST_SOURCE_URL,
    }


# ---------------------------------------------------------------------------
# Static fallback functions
# ---------------------------------------------------------------------------

def _static_matchable() -> list[dict[str, str | bool]]:
    return [dict(item) for item in _COSYVOICE_V3_FLASH_VOICES if item.get("matchable", True)]


def _static_endpoint_available(mode: str) -> list[dict[str, str | bool]]:
    from services.tts.cosyvoice_endpoint_config import is_voice_available
    return [
        dict(item) for item in _COSYVOICE_V3_FLASH_VOICES
        if item.get("matchable", True) and is_voice_available(str(item["voice_id"]), mode)
    ]


# ---------------------------------------------------------------------------
# Dynamic catalog via Gateway internal API (same pattern as VolcEngine Phase 3)
# ---------------------------------------------------------------------------

import logging as _logging
import time as _time

import requests as _requests

_logger = _logging.getLogger(__name__)

_GATEWAY_URL = "http://127.0.0.1:8880/api/internal/voice-catalog"
_CACHE_TTL = 60.0  # seconds

# Cache: key → (voices, default_voice_id, timestamp)
_cosy_cache: dict[str, tuple[list[dict], str, float]] = {}


def _fetch_cosyvoice_from_gateway(endpoint_mode: str | None = None) -> tuple[list[dict], str]:
    """Fetch CosyVoice catalog from Gateway internal API."""
    params: dict[str, str] = {"provider": "cosyvoice"}
    if endpoint_mode:
        params["endpoint_mode"] = endpoint_mode

    resp = _requests.get(_GATEWAY_URL, params=params, timeout=5.0)
    resp.raise_for_status()
    data = resp.json()
    return data["voices"], data["default_voice_id"]


def _load_cosyvoice_dynamic(cache_key: str, endpoint_mode: str | None = None) -> list[dict]:
    """Load from Gateway with cache + static fallback."""
    cached = _cosy_cache.get(cache_key)
    if cached and (_time.time() - cached[2]) < _CACHE_TTL:
        return cached[0]

    try:
        voices, default_vid = _fetch_cosyvoice_from_gateway(endpoint_mode)
        _cosy_cache[cache_key] = (voices, default_vid, _time.time())
        return voices
    except Exception as exc:
        _logger.warning("Gateway CosyVoice catalog 不可用 (%s), fallback 到静态列表", exc)
        if endpoint_mode:
            return _static_endpoint_available(endpoint_mode)
        return _static_matchable()


# ---------------------------------------------------------------------------
# Public API — same signatures, now dynamic
# ---------------------------------------------------------------------------

def list_matchable_cosyvoice_voices() -> list[dict[str, str | bool]]:
    """Return only voices in the B1 active matching pool (matchable=True).

    Phase 3: reads from Gateway DB with 60s cache + static fallback.
    """
    return _load_cosyvoice_dynamic("all_matchable")


def list_endpoint_available_voices(mode: str) -> list[dict[str, str | bool]]:
    """Return matchable voices available on the given endpoint mode.

    Phase 3: reads from Gateway DB with endpoint_mode filter + 60s cache + static fallback.
    """
    return _load_cosyvoice_dynamic(f"endpoint_{mode}", endpoint_mode=mode)


def list_cosyvoice_v3_flash_builtin_voice_options() -> list[dict[str, object]]:
    options: list[dict[str, object]] = []
    for item in _COSYVOICE_V3_FLASH_VOICES:
        option = build_cosyvoice_v3_flash_builtin_voice_option(item["voice_id"])
        if option is not None:
            options.append(option)
    return options

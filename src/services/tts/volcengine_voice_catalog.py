"""VolcEngine (豆包) voice catalog — full official voice list for 1.0 and 2.0.

Covers all officially published voices EXCEPT:
- IP仿音 (鲁班七号, 唐僧, etc.)
- 方言/口音 (粤语, 四川, 台湾, etc.)
- 日语/西语/多语种 (non-Chinese-English)

Each entry carries demographic tags used by the B1 baseline matcher.
Phase 1 tags (gender, scene) are derived from official metadata.
Phase 1 age_group/persona_style/energy_level are coarse estimates from
display names — Phase 2 will refine them via Gemini batch labeling.

The ``matchable`` flag controls whether a voice participates in
automatic matching.  Non-matchable voices are kept for display / manual
selection only.

Resource IDs imported from the provider module to stay in sync.
"""

from __future__ import annotations

from typing import Final

from services.tts.volcengine_tts_provider import (
    DEFAULT_SPEAKER_1_0,
    DEFAULT_SPEAKER_2_0,
    RESOURCE_ID_1_0,
    RESOURCE_ID_2_0,
)

VoiceEntry = dict[str, str | bool]

# ===================================================================
# Helper to reduce repetition
# ===================================================================

def _v(voice_id: str, name: str, rid: str, gender: str, age: str, persona: str, energy: str, scene: str = "通用", lang: str = "zh", matchable: bool = True) -> VoiceEntry:
    return {"voice_id": voice_id, "display_name": name, "resource_id": rid, "gender": gender, "age_group": age, "persona_style": persona, "energy_level": energy, "scene": scene, "language": lang, "matchable": matchable}

_R1 = RESOURCE_ID_1_0
_R2 = RESOURCE_ID_2_0

# ===================================================================
# 豆包 2.0 voices
# ===================================================================

VOICES_2_0: Final[list[VoiceEntry]] = [
    # --- 2.0 uranus Chinese ---
    _v("zh_female_vv_uranus_bigtts",               "Vivi 2.0",         _R2, "female", "young", "cute", "medium",   "通用", "zh"),
    _v("zh_female_xiaohe_uranus_bigtts",            "小何 2.0",         _R2, "female", "young", "neutral", "medium", "通用"),
    _v("zh_male_m191_uranus_bigtts",                "云舟 2.0",         _R2, "male",   "young", "warm", "medium", "通用"),
    _v("zh_male_taocheng_uranus_bigtts",            "小天 2.0",         _R2, "male",   "young", "neutral", "medium",   "通用"),
    _v("zh_male_liufei_uranus_bigtts",              "刘飞 2.0",         _R2, "male",   "middle", "neutral", "medium",    "通用"),
    _v("zh_male_sophie_uranus_bigtts",              "魅力苏菲 2.0",     _R2, "male",   "young", "warm", "medium", "通用"),
    _v("zh_female_qingxinnvsheng_uranus_bigtts",    "清新女声 2.0",     _R2, "female", "young", "warm", "medium", "通用"),
    _v("zh_female_cancan_uranus_bigtts",            "知性灿灿 2.0",     _R2, "female", "middle", "professional", "medium", "角色扮演"),
    _v("zh_female_sajiaoxuemei_uranus_bigtts",      "撒娇学妹 2.0",     _R2, "female", "young", "cute", "medium",   "角色扮演"),
    _v("zh_female_tianmeixiaoyuan_uranus_bigtts",   "甜美小源 2.0",     _R2, "female", "young", "cute", "medium", "通用"),
    _v("zh_female_tianmeitaozi_uranus_bigtts",      "甜美桃子 2.0",     _R2, "female", "young", "cute", "medium",    "通用"),
    _v("zh_female_shuangkuaisisi_uranus_bigtts",    "爽快思思 2.0",     _R2, "female", "young", "energetic", "high",   "通用"),
    _v("zh_female_peiqi_uranus_bigtts",             "佩奇猪 2.0",      _R2, "child",  "child", "cute", "medium",   "视频配音"),
    _v("zh_female_linjianvhai_uranus_bigtts",       "邻家女孩 2.0",     _R2, "female", "young",  "warm",         "low",    "通用"),
    _v("zh_male_shaonianzixin_uranus_bigtts",       "少年梓辛 2.0",     _R2, "male",   "young",  "energetic",    "high",   "通用"),
    _v("zh_male_sunwukong_uranus_bigtts",           "猴哥 2.0",         _R2, "male",   "young",  "energetic",    "high",   "视频配音"),
    _v("zh_female_yingyujiaoxue_uranus_bigtts",     "Tina老师 2.0",    _R2, "female", "middle", "professional", "medium", "教育"),
    _v("zh_female_kefunvsheng_uranus_bigtts",       "暖阳女声 2.0",     _R2, "female", "middle", "warm",         "medium", "客服"),
    _v("zh_female_xiaoxue_uranus_bigtts",           "儿童绘本 2.0",     _R2, "child",  "young",  "warm",         "medium", "有声阅读"),
    _v("zh_male_dayi_uranus_bigtts",                "大壹 2.0",         _R2, "male",   "middle", "serious",      "medium", "视频配音"),
    _v("zh_female_mizai_uranus_bigtts",             "黑猫侦探社咪仔 2.0", _R2, "child", "young",  "energetic",    "high",   "视频配音"),
    _v("zh_female_jitangnv_uranus_bigtts",          "鸡汤女 2.0",       _R2, "female", "young",  "warm",         "medium", "视频配音"),
    _v("zh_female_meilinvyou_uranus_bigtts",        "魅力女友 2.0",     _R2, "female", "young",  "warm",         "medium", "通用"),
    _v("zh_female_liuchangnv_uranus_bigtts",        "流畅女声 2.0",     _R2, "female", "young", "professional", "medium", "视频配音"),
    _v("zh_male_ruyayichen_uranus_bigtts",          "儒雅逸辰 2.0",     _R2, "male",   "middle", "warm", "medium", "视频配音"),
    # --- 2.0 uranus English ---
    _v("en_male_tim_uranus_bigtts",                 "Tim",              _R2, "male",   "middle", "neutral", "medium", "多语种", "en"),
    _v("en_female_dacey_uranus_bigtts",             "Dacey",            _R2, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("en_female_stokie_uranus_bigtts",            "Stokie",           _R2, "female", "young", "neutral", "medium",   "多语种", "en"),
    # --- 2.0 Saturn 角色扮演 ---
    _v("saturn_zh_female_keainvsheng_tob",          "可爱女生",          _R2, "female", "young", "cute", "medium",   "角色扮演"),
    _v("saturn_zh_female_tiaopigongzhu_tob",        "调皮公主",          _R2, "female", "young", "energetic", "high",   "角色扮演"),
    _v("saturn_zh_male_shuanglangshaonian_tob",     "爽朗少年",          _R2, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("saturn_zh_male_tiancaitongzhuo_tob",        "天才同桌",          _R2, "male",   "young", "neutral", "medium", "角色扮演"),
    _v("saturn_zh_female_cancan_tob",               "知性灿灿",          _R2, "female", "middle", "professional", "medium", "角色扮演"),
    # --- 2.0 Saturn 客服 ---
    _v("saturn_zh_female_qingyingduoduo_cs_tob",    "轻盈朵朵 2.0",     _R2, "female", "young", "cute", "medium", "客服"),
    _v("saturn_zh_female_wenwanshanshan_cs_tob",    "温婉珊珊 2.0",     _R2, "female", "young", "warm", "medium", "客服"),
    _v("saturn_zh_female_reqingaina_cs_tob",        "热情艾娜 2.0",     _R2, "female", "young", "energetic", "high",   "客服"),
]

# ===================================================================
# 豆包 1.0 voices
# ===================================================================

VOICES_1_0: Final[list[VoiceEntry]] = [
    # ---------------------------------------------------------------
    # 多情感 (emo) — Chinese
    # ---------------------------------------------------------------
    _v("zh_male_lengkugege_emo_v2_mars_bigtts",         "冷酷哥哥",       _R1, "male",   "young", "serious", "low", "多情感"),
    _v("zh_female_tianxinxiaomei_emo_v2_mars_bigtts",   "甜心小美",       _R1, "female", "young", "cute", "medium", "多情感"),
    _v("zh_female_gaolengyujie_emo_v2_mars_bigtts",     "高冷御姐",       _R1, "female", "middle", "serious", "low", "多情感"),
    _v("zh_male_aojiaobazong_emo_v2_mars_bigtts",       "傲娇霸总",       _R1, "male",   "middle", "serious", "low",   "多情感"),
    _v("zh_male_guangzhoudege_emo_mars_bigtts",          "广州德哥",       _R1, "male",   "middle", "neutral", "medium", "多情感"),
    _v("zh_male_jingqiangkanye_emo_mars_bigtts",         "京腔侃爷",       _R1, "male",   "middle", "energetic", "high",   "多情感"),
    _v("zh_female_linjuayi_emo_v2_mars_bigtts",          "邻居阿姨",       _R1, "female", "elderly", "warm", "medium", "多情感"),
    _v("zh_male_yourougongzi_emo_v2_mars_bigtts",        "优柔公子",       _R1, "male",   "young", "warm", "low",    "多情感"),
    _v("zh_male_ruyayichen_emo_v2_mars_bigtts",          "儒雅男友",       _R1, "male",   "young", "warm", "medium", "多情感"),
    _v("zh_male_junlangnanyou_emo_v2_mars_bigtts",       "俊朗男友",       _R1, "male",   "young", "energetic", "high", "多情感"),
    _v("zh_male_beijingxiaoye_emo_v2_mars_bigtts",       "北京小爷",       _R1, "male",   "young", "energetic", "high",   "多情感"),
    _v("zh_female_roumeinvyou_emo_v2_mars_bigtts",       "柔美女友",       _R1, "female", "young", "warm", "medium",    "多情感"),
    _v("zh_male_yangguangqingnian_emo_v2_mars_bigtts",   "阳光青年",       _R1, "male",   "young", "energetic", "high",   "多情感"),
    _v("zh_female_meilinvyou_emo_v2_mars_bigtts",        "魅力女友",       _R1, "female", "young", "warm", "medium", "多情感"),
    _v("zh_female_shuangkuaisisi_emo_v2_mars_bigtts",    "爽快思思",       _R1, "female", "young", "energetic", "high",   "多情感"),
    _v("zh_male_shenyeboke_emo_v2_mars_bigtts",          "深夜播客",       _R1, "male",   "middle", "warm", "low",    "多情感"),
    # --- 多情感 English ---
    _v("en_female_candice_emo_v2_mars_bigtts",           "Candice",        _R1, "female", "young", "neutral", "medium", "多情感", "en"),
    _v("en_female_skye_emo_v2_mars_bigtts",              "Serena",         _R1, "female", "young", "neutral", "medium", "多情感", "en"),
    _v("en_male_glen_emo_v2_mars_bigtts",                "Glen",           _R1, "male",   "middle", "neutral", "medium", "多情感", "en"),
    _v("en_male_sylus_emo_v2_mars_bigtts",               "Sylus",          _R1, "male",   "young", "neutral", "medium",    "多情感", "en"),
    _v("en_male_corey_emo_v2_mars_bigtts",               "Corey",          _R1, "male",   "middle", "professional", "medium", "多情感", "en"),
    _v("en_female_nadia_tips_emo_v2_mars_bigtts",        "Nadia",          _R1, "female", "young",  "warm",         "medium", "多情感", "en"),
    # ---------------------------------------------------------------
    # 通用场景
    # ---------------------------------------------------------------
    _v("zh_female_yingyujiaoyu_mars_bigtts",         "Tina老师",         _R1, "female", "middle", "professional", "medium", "教育"),
    _v("ICL_zh_female_wenrounvshen_239eff5e8ffa_tob","温柔女神",         _R1, "female", "middle", "warm",         "low",    "通用"),
    _v("zh_female_vv_mars_bigtts",                   "Vivi",             _R1, "female", "young",  "energetic",    "high",   "通用"),
    _v("zh_female_qinqienvsheng_moon_bigtts",        "亲切女声",         _R1, "female", "middle", "warm",         "medium", "通用"),
    _v("ICL_zh_male_shenmi_v1_tob",                  "机灵小伙",         _R1, "male",   "young",  "energetic",    "high",   "通用"),
    _v("ICL_zh_female_wuxi_tob",                     "元气甜妹",         _R1, "female", "young",  "energetic",    "high",   "通用"),
    _v("ICL_zh_female_wenyinvsheng_v1_tob",          "知心姐姐",         _R1, "female", "middle", "warm",         "medium", "通用"),
    _v("zh_male_qingyiyuxuan_mars_bigtts",           "阳光阿辰",         _R1, "male",   "young",  "energetic",    "high",   "通用"),
    _v("zh_male_xudong_conversation_wvae_bigtts",    "快乐小东",         _R1, "male",   "young", "energetic", "high",   "通用"),
    _v("ICL_zh_male_lengkugege_v1_tob",              "冷酷哥哥",         _R1, "male",   "young", "serious", "low", "通用"),
    _v("ICL_zh_female_feicui_v1_tob",                "纯澈女生",         _R1, "female", "young", "neutral", "medium", "通用"),
    _v("ICL_zh_female_yuxin_v1_tob",                 "初恋女友",         _R1, "female", "young", "warm", "medium",    "通用"),
    _v("ICL_zh_female_xnx_tob",                      "贴心闺蜜",         _R1, "female", "young", "warm", "medium", "通用"),
    _v("ICL_zh_female_yry_tob",                      "温柔白月光",       _R1, "female", "young", "warm", "low",    "通用"),
    _v("ICL_zh_male_BV705_streaming_cs_tob",         "炀炀",             _R1, "male",   "young", "neutral", "medium", "通用"),
    _v("en_male_jason_conversation_wvae_bigtts",     "开朗学长",         _R1, "male",   "young", "energetic", "high",   "通用", "en"),
    _v("zh_female_sophie_conversation_wvae_bigtts",  "魅力苏菲",         _R1, "female", "middle", "neutral", "medium", "通用"),
    _v("ICL_zh_female_yilin_tob",                    "贴心妹妹",         _R1, "female", "young", "warm", "medium", "通用"),
    _v("zh_female_tianmeitaozi_mars_bigtts",         "甜美桃子",         _R1, "female", "young", "cute", "medium",    "通用"),
    _v("zh_female_qingxinnvsheng_mars_bigtts",       "清新女声",         _R1, "female", "young", "neutral", "medium", "通用"),
    _v("zh_female_zhixingnvsheng_mars_bigtts",       "知性女声",         _R1, "female", "middle", "professional", "medium", "通用"),
    _v("zh_male_qingshuangnanda_mars_bigtts",        "清爽男大",         _R1, "male",   "young", "neutral", "medium", "通用"),
    _v("zh_female_linjianvhai_moon_bigtts",          "邻家女孩",         _R1, "female", "young", "warm", "medium",    "通用"),
    _v("zh_male_yuanboxiaoshu_moon_bigtts",          "渊博小叔",         _R1, "male",   "middle", "professional", "medium", "通用"),
    _v("zh_male_yangguangqingnian_moon_bigtts",      "阳光青年",         _R1, "male",   "young", "energetic", "high",   "通用"),
    _v("zh_female_tianmeixiaoyuan_moon_bigtts",      "甜美小源",         _R1, "female", "young", "cute", "medium", "通用"),
    _v("zh_female_qingchezizi_moon_bigtts",          "清澈梓梓",         _R1, "female", "young", "neutral", "medium", "通用"),
    _v("zh_male_jieshuoxiaoming_moon_bigtts",        "解说小明",         _R1, "male",   "young", "professional", "medium",   "通用"),
    _v("zh_female_kailangjiejie_moon_bigtts",        "开朗姐姐",         _R1, "female", "middle", "energetic", "high",   "通用"),
    _v("zh_male_linjiananhai_moon_bigtts",           "邻家男孩",         _R1, "male",   "young", "warm", "medium", "通用"),
    _v("zh_female_tianmeiyueyue_moon_bigtts",        "甜美悦悦",         _R1, "female", "young", "cute", "medium", "通用"),
    _v("zh_female_xinlingjitang_moon_bigtts",        "心灵鸡汤",         _R1, "female", "middle", "warm", "low", "通用"),
    _v("ICL_zh_female_zhixingwenwan_tob",            "知性温婉",         _R1, "female", "middle", "professional", "medium", "通用"),
    _v("ICL_zh_male_nuanxintitie_tob",               "暖心体贴",         _R1, "male",   "middle", "warm", "medium", "通用"),
    _v("ICL_zh_male_kailangqingkuai_tob",            "开朗轻快",         _R1, "male",   "young", "energetic", "high",   "通用"),
    _v("ICL_zh_male_huoposhuanglang_tob",            "活泼爽朗",         _R1, "male",   "young", "energetic", "high",   "通用"),
    _v("ICL_zh_male_shuaizhenxiaohuo_tob",           "率真小伙",         _R1, "male",   "young", "energetic", "high", "通用"),
    _v("zh_male_wenrouxiaoge_mars_bigtts",           "温柔小哥",         _R1, "male",   "young", "warm", "medium",    "通用"),
    _v("zh_female_cancan_mars_bigtts",               "灿灿",             _R1, "female", "young",  "energetic",    "high",   "通用"),
    _v("zh_female_shuangkuaisisi_moon_bigtts",       "爽快思思",         _R1, "female", "young",  "energetic",    "high",   "通用"),
    _v("zh_male_wennuanahu_moon_bigtts",             "温暖阿虎",         _R1, "male",   "middle", "warm",         "medium", "通用"),
    _v("zh_male_shaonianzixin_moon_bigtts",          "少年梓辛",         _R1, "male",   "young",  "energetic",    "high",   "通用"),
    _v("ICL_zh_female_wenrouwenya_tob",              "温柔文雅",         _R1, "female", "middle", "warm",         "low",    "通用"),
    # ---------------------------------------------------------------
    # 角色扮演
    # ---------------------------------------------------------------
    _v("ICL_zh_female_chunzhenshaonv_e588402fb8ad_tob",        "纯真少女",     _R1, "female", "young",  "warm",      "medium", "角色扮演"),
    _v("ICL_zh_male_xiaonaigou_edf58cf28b8b_tob",             "奶气小生",     _R1, "male",   "young",  "warm",      "medium", "角色扮演"),
    _v("ICL_zh_female_jinglingxiangdao_1beb294a9e3e_tob",      "精灵向导",     _R1, "female", "young",  "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_menyoupingxiaoge_ffed9fc2fee7_tob",        "闷油瓶小哥",   _R1, "male",   "young",  "serious",   "low",    "角色扮演"),
    _v("ICL_zh_male_anrenqinzhu_cd62e63dcdab_tob",             "黯刃秦主",     _R1, "male",   "middle", "serious",   "low",    "角色扮演"),
    _v("ICL_zh_male_badaozongcai_v1_tob",                      "霸道总裁",     _R1, "male",   "middle", "serious", "low",   "角色扮演"),
    _v("ICL_zh_female_ganli_v1_tob",                            "妩媚可人",     _R1, "female", "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_female_xiangliangya_v1_tob",                     "邪魅御姐",     _R1, "female", "middle", "serious", "low", "角色扮演"),
    _v("ICL_zh_male_ms_tob",                                    "嚣张小哥",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_you_tob",                                   "油腻大叔",     _R1, "male",   "middle", "neutral", "medium",    "角色扮演"),
    _v("ICL_zh_male_guaogongzi_v1_tob",                         "孤傲公子",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_huzi_v1_tob",                               "胡子叔叔",     _R1, "male",   "middle", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_female_luoqing_v1_tob",                          "性感魅惑",     _R1, "female", "middle", "neutral", "medium",    "角色扮演"),
    _v("ICL_zh_male_bingruogongzi_tob",                         "病弱公子",     _R1, "male",   "young", "neutral", "low",    "角色扮演"),
    _v("ICL_zh_female_bingjiao3_tob",                            "邪魅女王",     _R1, "female", "middle", "serious", "medium", "角色扮演"),
    _v("ICL_zh_male_aomanqingnian_tob",                          "傲慢青年",     _R1, "male",   "young", "serious", "medium", "角色扮演"),
    _v("ICL_zh_male_cujingnansheng_tob",                         "醋精男生",     _R1, "male",   "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_male_shuanglangshaonian_tob",                     "爽朗少年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_sajiaonanyou_tob",                           "撒娇男友",     _R1, "male",   "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_male_wenrounanyou_tob",                           "温柔男友",     _R1, "male",   "young", "warm", "medium",    "角色扮演"),
    _v("ICL_zh_male_wenshunshaonian_tob",                        "温顺少年",     _R1, "male",   "young", "warm", "low",    "角色扮演"),
    _v("ICL_zh_male_naigounanyou_tob",                           "粘人男友",     _R1, "male",   "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_male_sajiaonansheng_tob",                         "撒娇男生",     _R1, "male",   "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_male_huoponanyou_tob",                            "活泼男友",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_tianxinanyou_tob",                           "甜系男友",     _R1, "male",   "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_male_huoliqingnian_tob",                          "活力青年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_kailangqingnian_tob",                        "开朗青年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_lengmoxiongzhang_tob",                       "冷漠兄长",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_tiancaitongzhuo_tob",                        "天才同桌",     _R1, "male",   "young", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_male_pianpiangongzi_tob",                         "翩翩公子",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_mengdongqingnian_tob",                       "懵懂青年",     _R1, "male",   "young", "cute", "medium",    "角色扮演"),
    _v("ICL_zh_male_lenglianxiongzhang_tob",                     "冷脸兄长",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_bingjiaoshaonian_tob",                       "病娇少年",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_bingjiaonanyou_tob",                         "病娇男友",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_bingruoshaonian_tob",                        "病弱少年",     _R1, "male",   "young", "neutral", "low",    "角色扮演"),
    _v("ICL_zh_male_yiqishaonian_tob",                           "意气少年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_ganjingshaonian_tob",                        "干净少年",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_lengmonanyou_tob",                           "冷漠男友",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_jingyingqingnian_tob",                       "精英青年",     _R1, "male",   "young", "professional", "medium","角色扮演"),
    _v("ICL_zh_male_rexueshaonian_tob",                          "热血少年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_qingshuangshaonian_tob",                     "清爽少年",     _R1, "male",   "young", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_male_zhongerqingnian_tob",                        "中二青年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_lingyunqingnian_tob",                        "凌云青年",     _R1, "male",   "young", "energetic", "high", "角色扮演"),
    _v("ICL_zh_male_zifuqingnian_tob",                           "自负青年",     _R1, "male",   "young", "serious", "low", "角色扮演"),
    _v("ICL_zh_male_bujiqingnian_tob",                           "不羁青年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_ruyajunzi_tob",                              "儒雅君子",     _R1, "male",   "middle", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_diyinchenyu_tob",                            "低音沉郁",     _R1, "male",   "middle", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_lenglianxueba_tob",                          "冷脸学霸",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_ruyazongcai_tob",                            "儒雅总裁",     _R1, "male",   "middle", "warm", "medium","角色扮演"),
    _v("ICL_zh_male_shenchenzongcai_tob",                        "深沉总裁",     _R1, "male",   "middle", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_xiaohouye_tob",                              "小侯爷",       _R1, "male",   "young", "energetic", "high", "角色扮演"),
    _v("ICL_zh_male_gugaogongzi_tob",                            "孤高公子",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_zhangjianjunzi_tob",                         "仗剑君子",     _R1, "male",   "young", "energetic", "high", "角色扮演"),
    _v("ICL_zh_male_wenrunxuezhe_tob",                           "温润学者",     _R1, "male",   "middle", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_qinqieqingnian_tob",                        "亲切青年",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_wenrouxuezhang_tob",                         "温柔学长",     _R1, "male",   "young", "warm", "medium",    "角色扮演"),
    _v("ICL_zh_male_gaolengzongcai_tob",                         "高冷总裁",     _R1, "male",   "middle", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_lengjungaozhi_tob",                          "冷峻高智",     _R1, "male",   "middle", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_chanruoshaoye_tob",                          "孱弱少爷",     _R1, "male",   "young", "neutral", "low",    "角色扮演"),
    _v("ICL_zh_male_zixinqingnian_tob",                          "自信青年",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_qingseqingnian_tob",                         "青涩青年",     _R1, "male",   "young",  "warm",      "low",    "角色扮演"),
    _v("ICL_zh_male_xuebatongzhuo_tob",                          "学霸同桌",     _R1, "male",   "young",  "professional","medium","角色扮演"),
    _v("ICL_zh_male_lengaozongcai_tob",                          "冷傲总裁",     _R1, "male",   "middle", "serious",   "low",    "角色扮演"),
    _v("ICL_zh_male_yuanqishaonian_tob",                         "元气少年",     _R1, "male",   "young",  "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_satuoqingnian_tob",                          "洒脱青年",     _R1, "male",   "young",  "energetic", "medium", "角色扮演"),
    _v("ICL_zh_male_zhishuaiqingnian_tob",                       "直率青年",     _R1, "male",   "young",  "energetic", "medium", "角色扮演"),
    _v("ICL_zh_male_siwenqingnian_tob",                          "斯文青年",     _R1, "male",   "young",  "warm",      "low",    "角色扮演"),
    _v("ICL_zh_male_junyigongzi_tob",                            "俊逸公子",     _R1, "male",   "young",  "warm",      "medium", "角色扮演"),
    _v("ICL_zh_male_zhangjianxiake_tob",                         "仗剑侠客",     _R1, "male",   "middle", "serious",   "medium", "角色扮演"),
    _v("ICL_zh_male_jijiaozhineng_tob",                          "机甲智能",     _R1, "male",   "young",  "professional","medium","角色扮演"),
    _v("zh_male_naiqimengwa_mars_bigtts",                        "奶气萌娃",     _R1, "child",  "child", "cute", "medium",   "角色扮演"),
    _v("zh_female_popo_mars_bigtts",                             "婆婆",         _R1, "female", "elderly", "neutral", "low", "角色扮演"),
    _v("zh_female_gaolengyujie_moon_bigtts",                     "高冷御姐",     _R1, "female", "middle", "serious", "low", "角色扮演"),
    _v("zh_male_aojiaobazong_moon_bigtts",                       "傲娇霸总",     _R1, "male",   "middle", "serious", "low",   "角色扮演"),
    _v("zh_female_meilinvyou_moon_bigtts",                       "魅力女友",     _R1, "female", "young", "warm", "medium", "角色扮演"),
    _v("zh_male_shenyeboke_moon_bigtts",                         "深夜播客",     _R1, "male",   "middle", "professional", "medium",    "角色扮演"),
    _v("zh_female_sajiaonvyou_moon_bigtts",                      "柔美女友",     _R1, "female", "young", "warm", "low",    "角色扮演"),
    _v("zh_female_yuanqinvyou_moon_bigtts",                      "撒娇学妹",     _R1, "female", "young", "cute", "medium",   "角色扮演"),
    _v("ICL_zh_female_bingruoshaonv_tob",                        "病弱少女",     _R1, "female", "young", "neutral", "low",    "角色扮演"),
    _v("ICL_zh_female_huoponvhai_tob",                           "活泼女孩",     _R1, "female", "young", "energetic", "high",   "角色扮演"),
    _v("zh_male_dongfanghaoran_moon_bigtts",                     "东方浩然",     _R1, "male",   "young", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_male_lvchaxiaoge_tob",                            "绿茶小哥",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_female_jiaoruoluoli_tob",                         "娇弱萝莉",     _R1, "female", "child", "cute", "medium",    "角色扮演"),
    _v("ICL_zh_male_lengdanshuli_tob",                           "冷淡疏离",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_hanhoudunshi_tob",                           "憨厚敦实",     _R1, "male",   "middle", "warm", "medium", "角色扮演"),
    _v("ICL_zh_female_huopodiaoman_tob",                         "活泼刁蛮",     _R1, "female", "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_guzhibingjiao_tob",                          "固执病娇",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_sajiaonianren_tob",                          "撒娇粘人",     _R1, "male",   "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_female_aomanjiaosheng_tob",                       "傲慢娇声",     _R1, "female", "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_male_xiaosasuixing_tob",                          "潇洒随性",     _R1, "male",   "young", "energetic", "high", "角色扮演"),
    _v("ICL_zh_male_guiyishenmi_tob",                            "诡异神秘",     _R1, "male",   "middle", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_ruyacaijun_tob",                             "儒雅才俊",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_zhengzhiqingnian_tob",                       "正直青年",     _R1, "male",   "young", "serious", "medium","角色扮演"),
    _v("ICL_zh_female_jiaohannvwang_tob",                        "娇憨女王",     _R1, "female", "middle", "cute", "medium",   "角色扮演"),
    _v("ICL_zh_female_bingjiaomengmei_tob",                      "病娇萌妹",     _R1, "female", "young", "cute", "medium",    "角色扮演"),
    _v("ICL_zh_male_qingsenaigou_tob",                           "青涩小生",     _R1, "male",   "young", "neutral", "medium",    "角色扮演"),
    _v("ICL_zh_male_chunzhenxuedi_tob",                          "纯真学弟",     _R1, "male",   "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_male_youroubangzhu_tob",                          "优柔帮主",     _R1, "male",   "middle", "neutral", "low",    "角色扮演"),
    _v("ICL_zh_male_yourougongzi_tob",                           "优柔公子",     _R1, "male",   "young", "neutral", "low",    "角色扮演"),
    _v("ICL_zh_female_tiaopigongzhu_tob",                        "调皮公主",     _R1, "female", "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_tiexinnanyou_tob",                           "贴心男友",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_shaonianjiangjun_tob",                       "少年将军",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_bingjiaogege_tob",                           "病娇哥哥",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_xuebanantongzhuo_tob",                       "学霸男同桌",   _R1, "male",   "young", "neutral", "medium","角色扮演"),
    _v("ICL_zh_male_youmoshushu_tob",                            "幽默叔叔",     _R1, "male",   "middle", "energetic", "high", "角色扮演"),
    _v("ICL_zh_female_jiaxiaozi_tob",                            "假小子",       _R1, "female", "young", "energetic", "high",   "角色扮演"),
    _v("ICL_zh_male_wenrounantongzhuo_tob",                      "温柔男同桌",   _R1, "male",   "young", "warm", "medium",    "角色扮演"),
    _v("ICL_zh_male_youmodaye_tob",                              "幽默大爷",     _R1, "male",   "elderly", "energetic", "high", "角色扮演"),
    _v("ICL_zh_male_asmryexiu_tob",                              "枕边低语",     _R1, "male",   "young", "warm", "low",    "角色扮演"),
    _v("ICL_zh_male_shenmifashi_tob",                            "神秘法师",     _R1, "male",   "middle", "serious", "low",    "角色扮演"),
    _v("zh_female_jiaochuan_mars_bigtts",                        "娇喘女声",     _R1, "female", "young", "cute", "medium",    "角色扮演"),
    _v("zh_male_livelybro_mars_bigtts",                          "开朗弟弟",     _R1, "male",   "young", "energetic", "high",   "角色扮演"),
    _v("zh_female_flattery_mars_bigtts",                         "谄媚女声",     _R1, "female", "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_lengjunshangsi_tob",                         "冷峻上司",     _R1, "male",   "middle", "serious", "low",    "角色扮演"),
    # --- 角色扮演 continued: additional voices from the second half of the list ---
    _v("ICL_zh_male_xiaoge_v1_tob",                  "寡言小哥",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_renyuwangzi_v1_tob",             "清朗温润",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_xiaosha_v1_tob",                 "潇洒随性",     _R1, "male",   "young", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_male_liyisheng_v1_tob",               "清冷矜贵",     _R1, "male",   "young", "serious", "low",    "角色扮演"),
    _v("ICL_zh_male_qinglen_v1_tob",                 "沉稳优雅",     _R1, "male",   "middle", "serious", "low", "角色扮演"),
    _v("ICL_zh_male_chongqingzhanzhan_v1_tob",       "清逸苏感",     _R1, "male",   "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_male_xingjiwangzi_v1_tob",            "温柔内敛",     _R1, "male",   "young",  "warm",         "low",    "角色扮演"),
    _v("ICL_zh_male_sigeshiye_v1_tob",               "低沉缱绻",     _R1, "male",   "middle", "warm",         "low",    "角色扮演"),
    _v("ICL_zh_male_lanyingcaohunshi_v1_tob",        "蓝银草魂师",   _R1, "male",   "young",  "serious",      "medium", "角色扮演"),
    _v("ICL_zh_female_liumengdie_v1_tob",            "清冷高雅",     _R1, "female", "middle", "serious",      "low",    "角色扮演"),
    _v("ICL_zh_female_linxueying_v1_tob",            "甜美娇俏",     _R1, "female", "young",  "warm",         "medium", "角色扮演"),
    _v("ICL_zh_female_rouguhunshi_v1_tob",           "柔骨魂师",     _R1, "female", "young",  "warm",         "low",    "角色扮演"),
    _v("ICL_zh_female_tianmei_v1_tob",               "甜美活泼",     _R1, "female", "young",  "energetic",    "high",   "角色扮演"),
    _v("ICL_zh_female_chengshu_v1_tob",              "成熟温柔",     _R1, "female", "middle", "warm",         "medium", "角色扮演"),
    _v("ICL_zh_female_xnx_v1_tob",                   "贴心闺蜜",     _R1, "female", "young",  "warm",         "medium", "角色扮演"),
    _v("ICL_zh_female_yry_v1_tob",                   "温柔白月光",   _R1, "female", "young",  "warm",         "low",    "角色扮演"),
    _v("zh_male_bv139_audiobook_ummv3_bigtts",       "高冷沉稳",     _R1, "male",   "middle", "serious",      "low",    "角色扮演"),
    # --- S2S-SC additional role-play ---
    _v("ICL_zh_male_cujingnanyou_tob",               "醋精男友",     _R1, "male",   "young",  "energetic",    "medium", "角色扮演"),
    _v("ICL_zh_male_fengfashaonian_tob",             "风发少年",     _R1, "male",   "young",  "energetic",    "high",   "角色扮演"),
    _v("ICL_zh_male_cixingnansang_tob",              "磁性男嗓",     _R1, "male",   "middle", "warm",         "low",    "角色扮演"),
    _v("ICL_zh_male_chengshuzongcai_tob",            "成熟总裁",     _R1, "male",   "middle", "professional", "medium", "角色扮演"),
    _v("ICL_zh_male_aojiaojingying_tob",             "傲娇精英",     _R1, "male",   "young",  "serious",      "high",   "角色扮演"),
    _v("ICL_zh_male_aojiaogongzi_tob",               "傲娇公子",     _R1, "male",   "young",  "energetic",    "medium", "角色扮演"),
    _v("ICL_zh_male_badaoshaoye_tob",                "霸道少爷",     _R1, "male",   "young",  "serious",      "high",   "角色扮演"),
    _v("ICL_zh_male_fuheigongzi_tob",                "腹黑公子",     _R1, "male",   "young",  "serious",      "medium", "角色扮演"),
    _v("ICL_zh_female_nuanxinxuejie_tob",            "暖心学姐",     _R1, "female", "young",  "warm",         "medium", "角色扮演"),
    _v("ICL_zh_female_keainvsheng_tob",              "可爱女生",     _R1, "female", "young", "cute", "medium",   "角色扮演"),
    _v("ICL_zh_female_chengshujiejie_tob",           "成熟姐姐",     _R1, "female", "middle", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_female_bingjiaojiejie_tob",           "病娇姐姐",     _R1, "female", "middle", "neutral", "medium",    "角色扮演"),
    _v("ICL_zh_female_wumeiyujie_tob",               "妩媚御姐",     _R1, "female", "middle", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_female_aojiaonvyou_tob",              "傲娇女友",     _R1, "female", "young", "cute", "medium", "角色扮演"),
    _v("ICL_zh_female_tiexinnvyou_tob",              "贴心女友",     _R1, "female", "young", "warm", "medium", "角色扮演"),
    _v("ICL_zh_female_xingganyujie_tob",             "性感御姐",     _R1, "female", "middle", "neutral", "medium", "角色扮演"),
    _v("ICL_zh_male_bingjiaodidi_tob",               "病娇弟弟",     _R1, "male",   "young", "neutral", "medium",    "角色扮演"),
    _v("ICL_zh_male_aomanshaoye_tob",                "傲慢少爷",     _R1, "male",   "young", "serious", "low", "角色扮演"),
    _v("ICL_zh_male_aiqilingren_tob",                "傲气凌人",     _R1, "male",   "young", "serious", "low",   "角色扮演"),
    _v("ICL_zh_male_bingjiaobailian_tob",            "病娇白莲",     _R1, "male",   "young", "neutral", "medium",    "角色扮演"),
    # ---------------------------------------------------------------
    # 客服
    # ---------------------------------------------------------------
    _v("ICL_zh_female_lixingyuanzi_cs_tob",          "理性圆子",     _R1, "female", "young", "professional", "medium", "客服"),
    _v("ICL_zh_female_qingtiantaotao_cs_tob",        "清甜桃桃",     _R1, "female", "young", "cute", "medium", "客服"),
    _v("ICL_zh_female_qingxixiaoxue_cs_tob",         "清晰小雪",     _R1, "female", "young", "professional", "medium", "客服"),
    _v("ICL_zh_female_qingtianmeimei_cs_tob",        "清甜莓莓",     _R1, "female", "young", "cute", "medium", "客服"),
    _v("ICL_zh_female_kailangtingting_cs_tob",        "开朗婷婷",     _R1, "female", "young", "energetic", "high",   "客服"),
    _v("ICL_zh_male_qingxinmumu_cs_tob",              "清新沐沐",     _R1, "male",   "young", "warm", "medium", "客服"),
    _v("ICL_zh_male_shuanglangxiaoyang_cs_tob",       "爽朗小阳",     _R1, "male",   "young", "energetic", "high",   "客服"),
    _v("ICL_zh_male_qingxinbobo_cs_tob",              "清新波波",     _R1, "male",   "young", "warm", "medium", "客服"),
    _v("ICL_zh_female_wenwanshanshan_cs_tob",          "温婉珊珊",     _R1, "female", "young", "warm", "medium", "客服"),
    _v("ICL_zh_female_tianmeixiaoyu_cs_tob",           "甜美小雨",     _R1, "female", "young", "cute", "medium", "客服"),
    _v("ICL_zh_female_reqingaina_cs_tob",              "热情艾娜",     _R1, "female", "young", "energetic", "high",   "客服"),
    _v("ICL_zh_female_tianmeixiaoju_cs_tob",           "甜美小橘",     _R1, "female", "young", "cute", "medium", "客服"),
    _v("ICL_zh_male_chenwenmingzai_cs_tob",            "沉稳明仔",     _R1, "male",   "young", "serious", "low", "客服"),
    _v("ICL_zh_male_qinqiexiaozhuo_cs_tob",           "亲切小卓",     _R1, "male",   "young", "warm", "medium", "客服"),
    _v("ICL_zh_female_lingdongxinxin_cs_tob",          "灵动欣欣",     _R1, "female", "young", "energetic", "high",   "客服"),
    _v("ICL_zh_female_guaiqiaokeer_cs_tob",            "乖巧可儿",     _R1, "female", "young", "cute", "medium",    "客服"),
    _v("ICL_zh_female_nuanxinqianqian_cs_tob",         "暖心茜茜",     _R1, "female", "young", "warm", "medium", "客服"),
    _v("ICL_zh_female_ruanmengtuanzi_cs_tob",          "软萌团子",     _R1, "female", "young", "cute", "medium",    "客服"),
    _v("ICL_zh_male_yangguangyangyang_cs_tob",         "阳光洋洋",     _R1, "male",   "young", "energetic", "high",   "客服"),
    _v("ICL_zh_female_ruanmengtangtang_cs_tob",        "软萌糖糖",     _R1, "female", "young",  "warm",         "low",    "客服"),
    _v("ICL_zh_female_xiuliqianqian_cs_tob",           "秀丽倩倩",     _R1, "female", "young",  "warm",         "medium", "客服"),
    _v("ICL_zh_female_kaixinxiaohong_cs_tob",          "开心小鸿",     _R1, "female", "young",  "energetic",    "high",   "客服"),
    _v("ICL_zh_female_qingyingduoduo_cs_tob",          "轻盈朵朵",     _R1, "female", "young",  "warm",         "medium", "客服"),
    _v("zh_female_kefunvsheng_mars_bigtts",            "暖阳女声",     _R1, "female", "middle", "warm",         "medium", "客服"),
    # ---------------------------------------------------------------
    # 视频配音
    # ---------------------------------------------------------------
    _v("zh_male_M100_conversation_wvae_bigtts",      "悠悠君子",     _R1, "male",   "middle", "warm",         "medium", "视频配音"),
    _v("zh_female_maomao_conversation_wvae_bigtts",  "文静毛毛",     _R1, "female", "young",  "warm",         "low",    "视频配音"),
    _v("ICL_zh_female_qiuling_v1_tob",               "倾心少女",     _R1, "female", "young",  "warm",         "medium", "视频配音"),
    _v("ICL_zh_male_buyan_v1_tob",                   "醇厚低音",     _R1, "male",   "middle", "serious",      "low",    "视频配音"),
    _v("ICL_zh_male_BV144_paoxiaoge_v1_tob",         "咆哮小哥",     _R1, "male",   "young",  "energetic",    "high",   "视频配音"),
    _v("ICL_zh_female_heainainai_tob",               "和蔼奶奶",     _R1, "female", "elderly", "warm", "medium",    "视频配音"),
    _v("ICL_zh_female_linjuayi_tob",                 "邻居阿姨",     _R1, "female", "elderly", "neutral", "medium", "视频配音"),
    _v("zh_female_wenrouxiaoya_moon_bigtts",         "温柔小雅",     _R1, "female", "young", "warm", "low",    "视频配音"),
    _v("zh_male_tiancaitongsheng_mars_bigtts",       "天才童声",     _R1, "child",  "child", "neutral", "medium",   "视频配音"),
    _v("zh_male_sunwukong_mars_bigtts",              "猴哥",         _R1, "male",   "young", "energetic", "high",   "视频配音"),
    _v("zh_male_xionger_mars_bigtts",                "熊二",         _R1, "child",  "child", "cute", "medium",   "视频配音"),
    _v("zh_female_peiqi_mars_bigtts",                "佩奇猪",       _R1, "child",  "child", "cute", "medium",   "视频配音"),
    _v("zh_female_gujie_mars_bigtts",                "顾姐",         _R1, "female", "middle", "neutral", "medium", "视频配音"),
    _v("zh_female_yingtaowanzi_mars_bigtts",         "樱桃丸子",     _R1, "child",  "child", "cute", "medium",   "视频配音"),
    _v("zh_male_chunhui_mars_bigtts",                "广告解说",     _R1, "male",   "middle", "professional", "medium",   "视频配音"),
    _v("zh_female_shaoergushi_mars_bigtts",          "少儿故事",     _R1, "female", "young",  "warm",         "medium", "视频配音"),
    _v("zh_male_silang_mars_bigtts",                 "四郎",         _R1, "male",   "middle", "serious",      "medium", "视频配音"),
    _v("zh_female_qiaopinvsheng_mars_bigtts",        "俏皮女声",     _R1, "female", "young",  "energetic",    "high",   "视频配音"),
    _v("zh_male_lanxiaoyang_mars_bigtts",            "懒音绵宝",     _R1, "male",   "young",  "warm",         "low",    "视频配音"),
    _v("zh_male_dongmanhaimian_mars_bigtts",         "亮嗓萌仔",     _R1, "child",  "young",  "energetic",    "high",   "视频配音"),
    _v("zh_male_jieshuonansheng_mars_bigtts",        "磁性解说男声", _R1, "male",   "middle", "professional", "medium", "视频配音", "zh"),
    _v("zh_female_jitangmeimei_mars_bigtts",         "鸡汤妹妹",     _R1, "female", "young",  "warm",         "medium", "视频配音"),
    _v("zh_female_tiexinnvsheng_mars_bigtts",        "贴心女声",     _R1, "female", "young",  "warm",         "medium", "视频配音"),
    _v("zh_female_mengyatou_mars_bigtts",            "萌丫头",       _R1, "female", "young",  "energetic",    "high",   "视频配音"),
    # ---------------------------------------------------------------
    # 有声阅读
    # ---------------------------------------------------------------
    _v("ICL_zh_male_neiliancaijun_e991be511569_tob", "内敛才俊",     _R1, "male",   "middle", "warm",         "medium", "有声阅读"),
    _v("ICL_zh_male_yangyang_v1_tob",                "温暖少年",     _R1, "male",   "young", "warm", "medium", "有声阅读"),
    _v("ICL_zh_male_flc_v1_tob",                     "儒雅公子",     _R1, "male",   "young", "neutral", "medium", "有声阅读"),
    _v("zh_male_changtianyi_mars_bigtts",            "悬疑解说",     _R1, "male",   "middle", "professional", "medium",    "有声阅读"),
    _v("zh_male_ruyaqingnian_mars_bigtts",           "儒雅青年",     _R1, "male",   "young", "neutral", "medium", "有声阅读"),
    _v("zh_male_baqiqingshu_mars_bigtts",            "霸气青叔",     _R1, "male",   "middle", "serious", "low",   "有声阅读"),
    _v("zh_male_qingcang_mars_bigtts",               "擎苍",         _R1, "male",   "middle", "serious", "low", "有声阅读"),
    _v("zh_male_yangguangqingnian_mars_bigtts",      "活力小哥",     _R1, "male",   "young", "energetic", "high",   "有声阅读"),
    _v("zh_female_gufengshaoyu_mars_bigtts",         "古风少御",     _R1, "female", "middle", "neutral", "medium", "有声阅读"),
    _v("zh_female_wenroushunv_mars_bigtts",          "温柔淑女",     _R1, "female", "young", "warm", "medium",    "有声阅读"),
    _v("zh_male_fanjuanqingnian_mars_bigtts",        "反卷青年",     _R1, "male",   "young", "neutral", "medium", "有声阅读"),
    # ---------------------------------------------------------------
    # 英语 (1.0 moon/mars)
    # ---------------------------------------------------------------
    _v("en_female_lauren_moon_bigtts",               "Lauren",         _R1, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("en_male_campaign_jamal_moon_bigtts",         "Energetic Male", _R1, "male",   "young", "energetic", "high",   "多语种", "en"),
    _v("en_male_chris_moon_bigtts",                  "Gotham Hero",    _R1, "male",   "middle", "serious", "low", "多语种", "en"),
    _v("en_female_product_darcie_moon_bigtts",       "Flirty Female",  _R1, "female", "young", "cute", "medium",   "多语种", "en"),
    _v("en_female_emotional_moon_bigtts",            "Peaceful Female",_R1, "female", "young", "warm", "low",    "多语种", "en"),
    _v("en_female_nara_moon_bigtts",                 "Nara",           _R1, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("en_male_bruce_moon_bigtts",                  "Bruce",          _R1, "male",   "middle", "neutral", "medium", "多语种", "en"),
    _v("en_male_michael_moon_bigtts",                "Michael",        _R1, "male",   "middle", "professional", "medium", "多语种", "en"),
    _v("ICL_en_male_cc_sha_v1_tob",                  "Cartoon Chef",   _R1, "male",   "middle", "energetic", "high",   "多语种", "en"),
    # zh_male_M100_conversation_wvae_bigtts is also "悠悠君子" in video dubbing — dedup, keep the Chinese entry
    _v("en_female_dacey_conversation_wvae_bigtts",   "Daisy",          _R1, "female", "young", "cute", "medium", "多语种", "en"),
    _v("en_male_charlie_conversation_wvae_bigtts",   "Owen",           _R1, "male",   "young", "neutral", "medium", "多语种", "en"),
    _v("en_female_sarah_new_conversation_wvae_bigtts","Luna",          _R1, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("ICL_en_male_michael_tob",                    "Michael",        _R1, "male",   "middle", "professional", "medium", "多语种", "en"),
    _v("en_male_adam_mars_bigtts",                   "Adam",           _R1, "male",   "young", "neutral", "medium", "多语种", "en"),
    _v("en_female_amanda_mars_bigtts",               "Amanda",         _R1, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("en_male_jackson_mars_bigtts",                "Jackson",        _R1, "male",   "young", "neutral", "medium",   "多语种", "en"),
    _v("en_female_daisy_moon_bigtts",                "Delicate Girl",  _R1, "female", "young", "warm", "medium",    "多语种", "en"),
    _v("en_male_dave_moon_bigtts",                   "Dave",           _R1, "male",   "young", "neutral", "medium", "多语种", "en"),
    _v("en_male_hades_moon_bigtts",                  "Hades",          _R1, "male",   "middle", "serious", "low",    "多语种", "en"),
    _v("en_female_onez_moon_bigtts",                 "Onez",           _R1, "female", "young", "neutral", "medium",   "多语种", "en"),
    _v("en_female_emily_mars_bigtts",                "Emily",          _R1, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("en_male_smith_mars_bigtts",                  "Smith",          _R1, "male",   "middle", "professional", "medium", "多语种", "en"),
    _v("en_female_anna_mars_bigtts",                 "Anna",           _R1, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("ICL_en_male_aussie_v1_tob",                  "Ethan",          _R1, "male",   "young", "neutral", "medium", "多语种", "en"),
    _v("en_female_sarah_mars_bigtts",                "Sarah",          _R1, "female", "young", "neutral", "medium", "多语种", "en"),
    _v("en_male_dryw_mars_bigtts",                   "Dryw",           _R1, "male",   "young", "neutral", "medium", "多语种", "en"),
    _v("zh_female_wuzetian_mars_bigtts",             "武则天",         _R1, "female", "middle", "serious", "low",   "视频配音"),
]

# ===================================================================
# Lookup helpers — static fallback data
# ===================================================================

# Combined list for resource-level queries
_ALL_VOICES: Final[list[VoiceEntry]] = VOICES_1_0 + VOICES_2_0

# Pre-built sets for fast membership checks
_VOICE_IDS_1_0: Final[frozenset[str]] = frozenset(v["voice_id"] for v in VOICES_1_0)
_VOICE_IDS_2_0: Final[frozenset[str]] = frozenset(v["voice_id"] for v in VOICES_2_0)


def _static_voices_for_resource(resource_id: str) -> list[VoiceEntry]:
    """Static fallback — the original Phase 1-2 inline data."""
    if resource_id == RESOURCE_ID_2_0:
        return [v for v in VOICES_2_0 if v.get("matchable", True)]
    return [v for v in VOICES_1_0 if v.get("matchable", True)]


def _static_all_ids_for_resource(resource_id: str) -> frozenset[str]:
    if resource_id == RESOURCE_ID_2_0:
        return _VOICE_IDS_2_0
    return _VOICE_IDS_1_0


# ===================================================================
# Phase 3: Dynamic catalog via Gateway internal API
# ===================================================================

import logging
import time

import requests as _requests

_logger = logging.getLogger(__name__)

_GATEWAY_URL = "http://127.0.0.1:8880/api/internal/voice-catalog"
_CACHE_TTL = 60.0  # seconds

# Cache: resource_id → (voices, default_voice_id, all_voice_ids, timestamp)
_cache: dict[str, tuple[list[VoiceEntry], str, frozenset[str], float]] = {}


def _fetch_from_gateway(resource_id: str) -> tuple[list[VoiceEntry], str, frozenset[str]]:
    """Fetch voice catalog from Gateway internal API (synchronous)."""
    resp = _requests.get(
        _GATEWAY_URL,
        params={"provider": "volcengine", "resource_id": resource_id},
        timeout=5.0,
    )
    resp.raise_for_status()
    data = resp.json()

    voices: list[VoiceEntry] = data["voices"]
    default_vid: str = data["default_voice_id"]
    all_ids = frozenset(v["voice_id"] for v in voices)

    return voices, default_vid, all_ids


def _get_cached(resource_id: str) -> tuple[list[VoiceEntry], str, frozenset[str]] | None:
    """Return cached data if fresh, else None."""
    entry = _cache.get(resource_id)
    if entry and (time.time() - entry[3]) < _CACHE_TTL:
        return entry[0], entry[1], entry[2]
    return None


def _load_dynamic(resource_id: str) -> tuple[list[VoiceEntry], str, frozenset[str]]:
    """Load from Gateway with cache + static fallback."""
    cached = _get_cached(resource_id)
    if cached:
        return cached

    try:
        voices, default_vid, all_ids = _fetch_from_gateway(resource_id)
        _cache[resource_id] = (voices, default_vid, all_ids, time.time())
        return voices, default_vid, all_ids
    except Exception as exc:
        _logger.warning("Gateway voice-catalog 不可用 (%s), fallback 到静态列表", exc)
        static_voices = _static_voices_for_resource(resource_id)
        default_vid = DEFAULT_SPEAKER_2_0 if resource_id == RESOURCE_ID_2_0 else DEFAULT_SPEAKER_1_0
        all_ids = _static_all_ids_for_resource(resource_id)
        return static_voices, default_vid, all_ids


# ===================================================================
# Public API — same signatures, now dynamic
# ===================================================================

def get_voices_for_resource(resource_id: str) -> list[VoiceEntry]:
    """Return the matchable voice pool for a given resource_id.

    Phase 3: reads from Gateway DB with 60s cache + static fallback.
    """
    voices, _, _ = _load_dynamic(resource_id)
    return voices


def get_default_voice_id(resource_id: str) -> str:
    """Return the safe default voice_id for a given resource_id."""
    _, default_vid, _ = _load_dynamic(resource_id)
    return default_vid


def is_voice_in_resource(voice_id: str, resource_id: str) -> bool:
    """Check whether a voice_id belongs to the given resource's catalog."""
    _, _, all_ids = _load_dynamic(resource_id)
    return voice_id in all_ids


def get_all_voice_ids_for_resource(resource_id: str) -> frozenset[str]:
    """Return all voice_ids (matchable + verified) for a resource."""
    _, _, all_ids = _load_dynamic(resource_id)
    return all_ids

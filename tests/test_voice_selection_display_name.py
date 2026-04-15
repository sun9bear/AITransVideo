"""Tests for voice_selection_api._resolve_speaker_display_name.

Bug history (2026-04-15): clone API was looking up speaker_names dict
+ speaker_name_a/speaker_name_b in review_state payload, but the actual
schema is voice_selection_review.payload.speakers[i].speaker_name (and
translation_review.payload.segments[*].display_name as a secondary
source). The lookup always missed and label fell back to "speaker_a Clone"
instead of e.g. "查理·芒格 Clone".
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Wire sys.path the same way the production tests do.
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# database module is shimmed (same pattern as test_voice_selection_clone_lock.py).
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from voice_selection_api import _resolve_speaker_display_name  # noqa: E402


# ----- Strategy 1: speakers[] in voice_selection_review (current schema) -----

def test_returns_speaker_name_from_voice_selection_review_speakers_array():
    """Current schema: voice_selection_review.payload.speakers is a list of
    dicts each carrying speaker_id + speaker_name."""
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speakers": [
                        {"speaker_id": "speaker_a", "speaker_name": "查理·芒格"},
                        {"speaker_id": "speaker_b", "speaker_name": "贝基·奎克"},
                    ],
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "查理·芒格"
    assert _resolve_speaker_display_name(rs, "speaker_b") == "贝基·奎克"


def test_strips_whitespace_from_speaker_name():
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speakers": [
                        {"speaker_id": "speaker_a", "speaker_name": "  芒格  "},
                    ],
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "芒格"


def test_skips_empty_speaker_name_in_speakers_array():
    """An empty/whitespace name shouldn't masquerade as the answer."""
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speakers": [
                        {"speaker_id": "speaker_a", "speaker_name": "   "},
                    ],
                },
            },
            "translation_review": {
                "payload": {
                    "segments": {
                        "1": {"speaker_id": "speaker_a", "display_name": "芒格"},
                    },
                },
            },
        }
    }
    # Strategy 1 yielded blank → falls through to Strategy 2
    assert _resolve_speaker_display_name(rs, "speaker_a") == "芒格"


# ----- Strategy 2: translation_review segments display_name -----

def test_falls_back_to_translation_review_segments_display_name():
    """If voice_selection_review has no speakers[], use any segment whose
    speaker_id matches and take its display_name."""
    rs = {
        "stages": {
            "translation_review": {
                "payload": {
                    "segments": {
                        "1": {"speaker_id": "speaker_b", "display_name": "贝基·奎克"},
                        "2": {"speaker_id": "speaker_a", "display_name": "查理·芒格"},
                        "3": {"speaker_id": "speaker_a", "display_name": "查理·芒格"},
                    }
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "查理·芒格"
    assert _resolve_speaker_display_name(rs, "speaker_b") == "贝基·奎克"


def test_returns_first_matching_segment_display_name():
    """Doesn't matter which key (1/2/3...) we hit first as long as it matches."""
    rs = {
        "stages": {
            "translation_review": {
                "payload": {
                    "segments": {
                        "5": {"speaker_id": "speaker_c", "display_name": "Dan Koe"},
                        "1": {"speaker_id": "speaker_c", "display_name": "Dan Koe"},
                    }
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_c") == "Dan Koe"


# ----- Strategy 3: legacy speaker_names dict -----

def test_legacy_speaker_names_dict_in_voice_selection_review():
    """Old review_state schemas: speaker_names is a flat {sid: name} dict."""
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speaker_names": {
                        "speaker_a": "Old Charlie",
                        "speaker_b": "Old Becky",
                    }
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "Old Charlie"


def test_legacy_speaker_names_in_translation_review():
    rs = {
        "stages": {
            "translation_review": {
                "payload": {
                    "speaker_names": {"speaker_a": "Legacy Name"}
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "Legacy Name"


# ----- Strategy 4: legacy speaker_name_a / speaker_name_b -----

def test_legacy_speaker_name_a_b_pair():
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speaker_name_a": "Alice",
                    "speaker_name_b": "Bob",
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "Alice"
    assert _resolve_speaker_display_name(rs, "speaker_b") == "Bob"


def test_speaker_name_a_does_not_apply_to_speaker_c():
    """The _a/_b heuristic only fires for the matching speaker_id."""
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speaker_name_a": "Alice",
                    "speaker_name_b": "Bob",
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_c") is None


# ----- Strategy precedence -----

def test_strategy_1_wins_over_legacy():
    """speakers[] entry should win over speaker_names dict, even if both exist."""
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speakers": [
                        {"speaker_id": "speaker_a", "speaker_name": "New Charlie"},
                    ],
                    "speaker_names": {"speaker_a": "Legacy Charlie"},
                    "speaker_name_a": "Even Older Charlie",
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "New Charlie"


def test_strategy_2_wins_over_strategy_3():
    """translation_review segments wins over legacy speaker_names dict."""
    rs = {
        "stages": {
            "translation_review": {
                "payload": {
                    "segments": {
                        "1": {"speaker_id": "speaker_a", "display_name": "From Segments"},
                    },
                    "speaker_names": {"speaker_a": "From Legacy"},
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "From Segments"


# ----- No match → None -----

def test_returns_none_when_speaker_id_unknown():
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "speakers": [
                        {"speaker_id": "speaker_a", "speaker_name": "Charlie"},
                    ]
                }
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_zzz") is None


def test_returns_none_when_no_review_state():
    assert _resolve_speaker_display_name({}, "speaker_a") is None
    assert _resolve_speaker_display_name({"stages": {}}, "speaker_a") is None


def test_returns_none_for_non_dict_input():
    """Defensive: never raise if review_state isn't a dict."""
    assert _resolve_speaker_display_name(None, "speaker_a") is None  # type: ignore[arg-type]
    assert _resolve_speaker_display_name("garbage", "speaker_a") is None  # type: ignore[arg-type]
    assert _resolve_speaker_display_name([], "speaker_a") is None  # type: ignore[arg-type]


def test_handles_missing_payload_gracefully():
    """Stages exist but payload is missing/None — should not raise."""
    rs = {"stages": {"voice_selection_review": {}, "translation_review": None}}
    assert _resolve_speaker_display_name(rs, "speaker_a") is None


def test_handles_speakers_not_a_list():
    """Defensive: speakers field exists but isn't a list."""
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {"speakers": "not a list"}
            }
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") is None


# ----- Real-world snapshot regression test -----

def test_2026_04_15_real_review_state_snapshot():
    """Snapshot of the actual review_state.json that exposed the bug:
    voice_selection_review payload has speakers[] with speaker_name field
    (Strategy 1), translation_review has segments with display_name
    (Strategy 2 fallback). Both should yield the same Chinese name."""
    rs = {
        "stages": {
            "voice_selection_review": {
                "payload": {
                    "all_providers": {"cosyvoice": [], "minimax": [], "volcengine": []},
                    "available_voices": [],
                    "clone_cost_credits": 500,
                    "message": "请为每位说话人选择或克隆配音音色",
                    "speakers": [
                        {
                            "speaker_id": "speaker_a",
                            "speaker_name": "凯特林·詹纳",
                            "voice_id": "vt_speaker_a_1776252490214",
                            "voice_source": "cloned",
                            "tts_provider": "minimax",
                            "segment_count": 45,
                            "total_duration_s": 1097.4,
                            "can_clone": True,
                        }
                    ],
                    "tts_provider": "minimax",
                }
            },
            "translation_review": {
                "payload": {
                    "segment_count": 45,
                    "segments": {
                        "1": {
                            "cn_text": "...",
                            "display_name": "凯特林·詹纳",
                            "speaker_id": "speaker_a",
                            "segment_id": 1,
                        }
                    },
                }
            },
        }
    }
    assert _resolve_speaker_display_name(rs, "speaker_a") == "凯特林·詹纳"

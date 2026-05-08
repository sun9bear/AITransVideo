"""Tests for the deterministic template router."""
from __future__ import annotations


def test_high_freq_template_for_trial_question():
    from gateway.support_templates import match_high_frequency_template

    m = match_high_frequency_template("试用结束后会自动扣费吗？")
    assert m is not None
    assert m.template_id == "tpl_trial_auto_charge"
    assert m.category == "trial"
    assert m.handoff_recommended is False


def test_high_freq_template_for_jianying_question():
    from gateway.support_templates import match_high_frequency_template

    m = match_high_frequency_template("怎么下载剪映草稿？")
    assert m is not None
    assert m.template_id == "tpl_jianying_draft_export"


def test_no_high_freq_template_for_off_topic():
    from gateway.support_templates import match_high_frequency_template

    m = match_high_frequency_template("今天天气怎么样")
    assert m is None


def test_route_message_sensitive_keyword_priority():
    from gateway.support_templates import route_message

    m = route_message("我要投诉到工信部")
    assert m is not None
    assert m.handoff_required is True
    assert m.handoff_recommended is True
    # Ordering matters — the policy module mirrors this list. We must keep
    # "投诉" / "工信部" as abuse_review.
    assert m.handoff_reason == "abuse_review"


def test_route_message_user_request_returns_voluntary_handoff():
    from gateway.support_templates import route_message

    m = route_message("我想找人工客服")
    assert m is not None
    assert m.handoff_required is False
    assert m.handoff_recommended is True
    assert m.handoff_reason == "user_requested_human"


def test_route_message_job_error_template_when_category_known():
    from gateway.support_templates import route_message

    m = route_message("处理失败", error_category="tts_failed")
    assert m is not None
    # Sensitive keywords should NOT be triggered by "处理失败" alone.
    assert m.handoff_recommended is False
    assert m.template_id == "tpl_err_tts_failed"
    assert m.category == "job_error:tts_failed"


def test_route_message_falls_through_to_high_frequency():
    from gateway.support_templates import route_message

    m = route_message("Express 和 Studio 哪个适合我")
    assert m is not None
    assert m.template_id == "tpl_express_vs_studio"


def test_detect_sensitive_keyword_with_custom_list():
    from gateway.support_templates import detect_sensitive_keyword

    assert detect_sensitive_keyword("我想要 special_word", keywords=["special_word"]) == "special_word"
    assert detect_sensitive_keyword("普通问题", keywords=["special_word"]) is None


def test_looks_english_only():
    from gateway.support_templates import looks_english_only

    assert looks_english_only("Can I upload a long video?") is True
    assert looks_english_only("我想上传一个视频") is False
    # Mixed content with majority Chinese is NOT english-only.
    assert looks_english_only("Studio 模式怎么用 demo") is False
    # Empty / whitespace doesn't trigger.
    assert looks_english_only("") is False

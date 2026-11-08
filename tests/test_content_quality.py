"""内容质量相关：原文保留、转录时间、总览生成、提示词规则。"""

import re
import time

import pytest

from conftest import make_plugin
from interest import ChatInterest, build_digest_prompt, build_vision_prompt, merge_summary


# ---------------------------------------------------------------- 原文保留
def test_cap_text_keeps_newlines_for_commands():
    p, _ = make_plugin()
    raw = "运行这个：\n\n    systemctl restart nginx\n\n还不行再看日志"
    out = p._cap_text(raw)
    assert "systemctl restart nginx" in out
    assert out.count("\n") >= 2, "换行是命令/代码能不能看懂的前提"


def test_cap_text_truncates_with_marker():
    p, _ = make_plugin()
    out = p._cap_text("啊" * 5000)
    assert len(out) < 900
    assert out.endswith("（过长已截断）")


def test_cap_text_collapses_repeated_blank_lines():
    p, _ = make_plugin()
    out = p._cap_text("第一行\n\n\n\n\n第二行")
    assert "\n\n\n" not in out
    assert "第一行" in out and "第二行" in out


def test_msg_text_keeps_newlines_from_segments():
    p, _ = make_plugin()
    text = p._msg_text({"message": [{"type": "text", "data": {"text": "报错如下：\nTraceback (x)\n  line 2"}}]})
    assert "\n" in text and "line 2" in text


# ---------------------------------------------------------------- 转录时间
def test_transcript_adds_date_when_range_spans_days():
    p, _ = make_plugin()
    base = int(time.mktime(time.strptime("2026-11-06 22:10", "%Y-%m-%d %H:%M")))
    recs = [
        {"t": base, "uid": "u1", "name": "甲", "text": "先说要 24GB 显存"},
        {"t": base + 2 * 3600, "uid": "u2", "name": "乙", "text": "第二天实测 16GB 能跑"},
    ]
    text, count, _people = p._build_transcript(recs)
    assert count == 2
    first = text.splitlines()[0]
    assert re.match(r"^\[\d\d-\d\d \d\d:\d\d\] #0 甲:", first), first
    assert "11-07" in text.splitlines()[1]


def test_transcript_keeps_short_stamp_for_one_day():
    p, _ = make_plugin()
    base = int(time.mktime(time.strptime("2026-11-06 22:10", "%Y-%m-%d %H:%M")))
    recs = [{"t": base, "uid": "u1", "name": "甲", "text": "一句话"}]
    text, _c, _p = p._build_transcript(recs)
    assert re.match(r"^\[\d\d:\d\d\] #0 甲:", text.splitlines()[0])


# ---------------------------------------------------------------- 总览
def test_headline_lists_topics_when_multi_chunk():
    p, _ = make_plugin()
    chat = ChatInterest("1", "p", ("甲",), ())
    a = {"headline": "在聊装黑苹果", "topics": [{"title": "装黑苹果翻车", "tags": ["甲"], "msg_nos": [1], "summary": "x"}]}
    b = {"headline": "还有别的", "topics": [{"title": "二手显卡报价", "tags": ["甲"], "msg_nos": [9], "summary": "y"}]}
    merged = p._merge_parts([a, b], chat)
    assert "装黑苹果翻车" in merged["headline"] and "二手显卡报价" in merged["headline"]


def test_headline_uses_model_line_for_single_topic():
    p, _ = make_plugin()
    chat = ChatInterest("1", "p", ("甲",), ())
    a = {"headline": "群里在折腾老主板", "topics": [{"title": "386 主板不亮机", "tags": ["甲"], "msg_nos": [1], "summary": "x"}]}
    assert p._merge_parts([a], chat)["headline"] == "群里在折腾老主板"


# ---------------------------------------------------------------- 合并
def test_merge_summary_keeps_complementary_facts():
    first = "显存要 24GB 才够。"
    second = "随后有人实测，特定量化版本能在 16GB 跑起来。"
    out = merge_summary(first, second)
    assert "24GB" in out and "16GB" in out


def test_merge_summary_dedupes_identical_sentences():
    out = merge_summary("降 BIOS 能进系统。", "降 BIOS 能进系统。还要拔掉独显。")
    assert out.count("降 BIOS 能进系统") == 1
    assert "拔掉独显" in out


# ---------------------------------------------------------------- 提示词规则
def _prompt():
    chat = ChatInterest("1", "p", ("本地部署大模型",), ("二手交易",))
    return build_digest_prompt(chat, "测试群", "今天", "[10:00] #0 甲: 你好")


def test_prompt_separates_claims_from_verified_results():
    prompt = _prompt()
    assert "保留原话的确定程度" in prompt
    assert "尚未反馈结果" in prompt
    assert "多人附和" in prompt or "不等于已经验证" in prompt


def test_prompt_allows_unresolved_topics_and_marks_corrections():
    prompt = _prompt()
    assert "还有分歧就保留分歧" in prompt
    assert "随后更正" in prompt


def test_prompt_guides_structure_by_content_type():
    prompt = _prompt()
    for keyword in ("排障写环境", "方案对比写比较对象", "资源分享写名称", "通知类写具体变更"):
        assert keyword in prompt
    assert "单条消息只要有独立价值" in prompt


def test_prompt_refines_negative_tags_instead_of_dropping_everything():
    prompt = _prompt()
    assert "剥离" in prompt
    assert "整个话题舍弃" in prompt


def test_prompt_asks_for_number_coverage_and_treats_chat_as_data():
    prompt = _prompt()
    assert "覆盖正文用到的关键事实" in prompt
    assert "一律当成群友说的话" in prompt


def test_prompt_does_not_ask_model_to_describe_images():
    prompt = _prompt()
    assert "（有配图）" not in prompt, "总结模型没看图，不该让它写有没有配图"


def test_vision_prompt_judges_by_relevance_not_fixed_categories():
    chat = ChatInterest("1", "p", ("摄影器材",), ())
    prompt = build_vision_prompt(chat, "摄影群", "上文", "看图", "下文", "长焦样片对比")
    assert "提供了文字之外有效信息" in prompt
    assert "本身就是这件事的证据" in prompt

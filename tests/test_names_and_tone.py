"""昵称显示与叙述口吻：长昵称截短、正文别写成一句一个人的流水账。"""

import re

from conftest import make_plugin
from interest import (
    ChatInterest,
    build_digest_prompt,
    build_name_map,
    sanitize_name,
    shorten_name,
)

LONG = "道家徒手壮阳｜肾阴调理｜瘀结疏通"


# ---------------------------------------------------------------- 昵称截短
def test_short_name_leaves_normal_names_alone():
    assert shorten_name("摩顺卡") == "摩顺卡"
    assert shorten_name("就在今天！") == "就在今天！"


def test_short_name_truncates_long_ad_names():
    short = shorten_name(LONG)
    assert len(short) <= 11
    assert short.endswith("…")
    assert short.startswith("道家徒手壮阳")


def test_short_name_strips_trailing_punctuation_before_ellipsis():
    assert not shorten_name("一二三四五六七八九十，十一十二").endswith("，…")


def test_name_map_keeps_short_names_and_shortens_long_ones():
    mapping = build_name_map(["摩顺卡", LONG, "阿越"])
    assert mapping["摩顺卡"] == "摩顺卡"
    assert mapping[LONG].endswith("…")
    assert mapping["阿越"] == "阿越"


def test_name_map_extends_when_two_long_names_collide():
    """两个长昵称前 10 个字一样时，多留几个字区分开，别截成一模一样。"""
    a = "前缀前缀前缀前缀前缀甲尾巴尾巴"
    b = "前缀前缀前缀前缀前缀乙尾巴尾巴"
    mapping = build_name_map([a, b])
    assert mapping[a] != mapping[b]
    assert mapping[a].endswith("…") and mapping[b].endswith("…")
    # 只给撞上的那个多留几个字，没撞的仍按上限截
    shortest = min(len(mapping[a]), len(mapping[b]))
    assert shortest <= len("前缀前缀前缀前缀前缀") + 1
    assert max(len(mapping[a]), len(mapping[b])) > shortest


def test_transcript_uses_short_names():
    p, _ = make_plugin()
    recs = [{"t": 100, "uid": "u1", "name": LONG, "text": "出个二手"}]
    text, _count, _people = p._build_transcript(recs)
    assert LONG not in text
    assert "道家徒手壮阳" in text


def test_members_use_short_names():
    import time

    p, _ = make_plugin()
    base = int(time.mktime(time.strptime("2026-11-07 16:23", "%Y-%m-%d %H:%M")))
    recs = [{"t": base, "uid": "u1", "name": LONG, "text": "出个二手"}]
    topics = [{"title": "出二手", "tags": ["甲"], "msg_nos": [0], "summary": "讲了讲。"}]
    described = p._describe_topics(topics, recs)
    assert LONG not in described[0]["members"]
    assert "道家徒手壮阳" in described[0]["members"]


def test_body_names_match_member_names():
    """正文高亮用的名字和参与人栏必须一致，否则同一个人的两种写法会互相对不上。"""
    import time

    p, _ = make_plugin()
    base = int(time.mktime(time.strptime("2026-11-07 16:23", "%Y-%m-%d %H:%M")))
    recs = [{"t": base, "uid": "u1", "name": LONG, "text": "出个二手"}]
    transcript, _c, _pp = p._build_transcript(recs)
    short = build_name_map([LONG])[LONG]
    assert short in transcript
    described = p._describe_topics(
        [{"title": "出二手", "tags": ["甲"], "msg_nos": [0], "summary": "讲了讲。"}], recs
    )
    assert described[0]["names"] == [short]


# ---------------------------------------------------------------- 叙述口吻
def test_prompt_forbids_speaker_by_speaker_lists():
    chat = ChatInterest("1", "p", ("计算机",), (), "", "")
    prompt = build_digest_prompt(chat, "测试群", "今天", "[10:00] #0 甲: 你好")
    assert "罗列流水账" in prompt
    assert "像叙述，不像会议记录" in prompt
    # 仍然要求点名，别为了顺句子把归属抹掉
    assert "多人说的是同一个意思就合并成一句" in prompt


# ---------------------------------------------------------------- 控制字符
def test_sanitize_name_drops_control_characters():
    """有的客户端把表情/格式符写进昵称，排版后会显示成乱码方块。"""
    raw = "\n\x11\x12\x0f冬姐好女孩\n\x05\n\x03吗\x10\x00"
    assert sanitize_name(raw) == "冬姐好女孩 吗"


def test_transcript_uses_clean_name_for_control_char_names():
    p, _ = make_plugin()
    raw = "\n\x11\x12\x0f冬姐好女孩\n\x05\n\x03吗\x10\x00"
    recs = [{"t": 100, "uid": "u1", "name": raw, "text": "在吗"}]
    text, _c, _pp = p._build_transcript(recs)
    assert "\x11" not in text
    assert "冬姐好女孩" in text

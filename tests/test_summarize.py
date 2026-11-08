import re

from conftest import make_plugin
from interest import ChatInterest


def _topic(title, nums, summary="讲述得很完整的一段总结，长度够长。"):
    return {"title": title, "tags": ["甲"], "msg_nos": nums, "summary": summary}


def test_transcript_has_index_numbers_from_zero():
    p, _ = make_plugin()
    recs = [
        {"t": 100, "gid": "1", "uid": "u1", "name": "甲", "text": "显卡降价"},
        {"t": 160, "gid": "1", "uid": "u2", "name": "乙", "text": "哦"},
    ]
    text, count, people = p._build_transcript(recs)
    assert count == 2 and people == 2
    first = text.splitlines()[0]
    assert re.match(r"^\[\d\d:\d\d\] #0 甲: ", first), first
    assert "#1 乙" in text


def test_transcript_keeps_index_when_lines_are_skipped():
    """被跳过的表情消息仍占编号，保证模型引用的编号能对回原记录。"""
    p, _ = make_plugin()
    recs = [
        {"t": 100, "gid": "1", "uid": "u1", "name": "甲", "text": "[表情]"},
        {"t": 160, "gid": "1", "uid": "u2", "name": "乙", "text": "真机图来了"},
    ]
    text, count, _people = p._build_transcript(recs)
    assert count == 1
    assert "#1 乙" in text


def test_merge_parts_collects_topics_across_chunks():
    p, _ = make_plugin()
    chat = ChatInterest("1", "p", ("甲",), ())
    a = {"topics": [_topic("装黑苹果翻车", [1, 2, 3], "前半段。")], "quotes": []}
    b = {"topics": [_topic("装黑苹果翻车", [4, 5], "前半段，补上后半段之后这个总结明显更长了。")]}
    merged = p._merge_parts([a, b], chat)
    assert len(merged["topics"]) == 1
    assert merged["topics"][0]["msg_nos"] == [1, 2, 3, 4, 5]
    assert "补上后半段" in merged["topics"][0]["summary"]


def test_merge_parts_drops_topics_without_numbers():
    p, _ = make_plugin()
    chat = ChatInterest("1", "p", ("甲",), ())
    merged = p._merge_parts(
        [{"topics": [_topic("没有编号", []), _topic("有编号", [7])]}], chat
    )
    assert [t["title"] for t in merged["topics"]] == ["有编号"]


def test_describe_topics_computes_time_and_members_from_records():
    """时间和参与人由编号回查原始记录算出来，不信模型自己写。"""
    import time

    p, _ = make_plugin()
    base = time.mktime(time.strptime("2026-11-07 16:23", "%Y-%m-%d %H:%M"))
    recs = [
        {"t": int(base), "name": "摩顺卡", "text": "本地跑 Qwen3.8 比 3.5 强"},
        {"t": int(base + 5 * 60), "name": "阿越", "text": "Mac mini 16G 爆内存"},
        {"t": int(base + 11 * 60), "name": "悠嫣咪", "text": "硬盘盒外置 HDD 稳"},
    ]
    topics = [
        {
            "title": "本地跑 Qwen3.8 与 Mac Mini NAS",
            "tags": ["电脑"],
            "msg_nos": [0, 1, 2],
            "summary": "完整讲述。",
        }
    ]
    out = p._describe_topics(topics, recs)
    assert out[0]["time"] == "16:23-16:34"
    assert out[0]["members"] == "摩顺卡、阿越、悠嫣咪"
    assert out[0]["count"] == 3


def test_describe_topics_ignores_out_of_range_numbers():
    p, _ = make_plugin()
    recs = [{"t": 100, "name": "甲", "text": "x"}]
    out = p._describe_topics(
        [{"title": "t", "tags": [], "msg_nos": [0, 99], "summary": "s"}], recs
    )
    assert out[0]["count"] == 1
    assert out[0]["members"] == "甲"

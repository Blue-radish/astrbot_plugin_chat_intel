import re

from conftest import make_plugin
from interest import ChatInterest


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


def test_merge_parts_groups_by_want_and_counts_truncation():
    p, _ = make_plugin()
    chat = ChatInterest("1", "p", ("复古硬件", "元件行情"), (), (), "", "")
    items = [
        {"want_item": "复古硬件", "topic": f"t{i}", "detail": "", "kind": "讨论"} for i in range(14)
    ]
    merged = p._merge_parts([{"items": items, "quotes": []}], chat)
    assert merged["sections"][0][0] == "复古硬件"
    assert len(merged["sections"][0][1]) == 12
    assert merged["truncated"] == 2


def test_merge_parts_dedupes_across_chunks():
    p, _ = make_plugin()
    chat = ChatInterest("1", "p", ("甲",), (), (), "", "")
    same = {"want_item": "甲", "topic": "同一件事", "detail": "x", "kind": "讨论"}
    merged = p._merge_parts([{"items": [same]}, {"items": [dict(same)]}], chat)
    assert len(merged["sections"][0][1]) == 1


def test_merge_parts_drops_items_without_topic():
    p, _ = make_plugin()
    chat = ChatInterest("1", "p", ("甲",), (), (), "", "")
    merged = p._merge_parts(
        [{"items": [{"want_item": "甲", "topic": "  ", "detail": "x"}, {"topic": "有话题"}]}], chat
    )
    topics = [item["topic"] for _, bucket in merged["sections"] for item in bucket]
    assert topics == ["有话题"]

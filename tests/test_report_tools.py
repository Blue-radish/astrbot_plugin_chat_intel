"""两个只读工具：读已有报告、查原始消息。"""

import asyncio
import json
import time

import pytest

import reports
from conftest import make_plugin, today_base

GID = "220001"
UMO = "bot_a:FriendMessage:110001"


class Ev:
    def __init__(self, text="", gid="", umo=UMO, sender="110001"):
        self.message_str = text
        self.unified_msg_origin = umo
        self._gid = gid
        self._sender = sender

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender


def build_with_report(tmp_path, **attrs):
    p, mod = make_plugin(
        reports_dir=tmp_path / "reports",
        group_ids=[GID],
        group_name_map={GID: "極東電子考古學結社"},
        owner_uids={"110001"},
        state={},
        store_dir=tmp_path / "messages",
        **attrs,
    )
    record = reports.build_record(
        report_id=f"chat_intel_{GID}_20261108_120000_今天",
        gid=GID,
        group="極東電子考古學結社",
        platform="bot_a",
        label="今天",
        since=1762500000,
        until=1762503600,
        topics=[
            {
                "title": "刷固件变砖",
                "tags": ["刷机"],
                "summary": "有人刷固件变砖，后来用 EDL 救回",
                "msg_nos": [0, 1],
                "time": "10:00-10:20",
                "members": "人0、人1",
                "count": 2,
            }
        ],
        records=[
            {"mid": "m0", "t": 1762500000, "uid": "u0", "name": "人0", "text": "我刷完就砖了"},
            {"mid": "m1", "t": 1762500060, "uid": "u1", "name": "人1", "text": "用 EDL 能救回来"},
        ],
        image_name=f"chat_intel_{GID}_20261108_120000_今天.png",
        headline="刷机翻车成了主线",
    )
    reports.save(tmp_path / "reports", record)
    (tmp_path / "reports" / f"{record['id']}.png").write_bytes(b"png")
    return p, record


def call(p, coro_fn):
    return asyncio.run(coro_fn())


# ---------------- get_group_report ----------------


def test_reads_the_report_by_explicit_id(tmp_path):
    p, record = build_with_report(tmp_path)
    out = call(p, lambda: p.tool_get_group_report(Ev(), report_id=record["id"]))
    assert record["id"] in out
    assert "極東電子考古學結社" in out
    assert "刷固件变砖" in out
    assert "完整" in out
    assert "总览：刷机翻车成了主线" in out


def test_topic_no_returns_refs_and_points_at_the_query_tool(tmp_path):
    p, record = build_with_report(tmp_path)
    out = call(
        p, lambda: p.tool_get_group_report(Ev(), report_id=record["id"], topic_no=1)
    )
    assert "来源消息引用" in out
    assert "m0" in out and "m1" in out
    assert "不含原文" in out, "要说清报告里没有原文，别让模型以为能直接念"
    assert "query_group_records" in out, "要告诉模型下一步怎么查"
    assert "我刷完就砖了" not in out, "报告里不存原文，自然也不该出现"


def test_topic_no_out_of_range_is_explained(tmp_path):
    p, record = build_with_report(tmp_path)
    out = call(
        p, lambda: p.tool_get_group_report(Ev(), report_id=record["id"], topic_no=5)
    )
    assert "没有第 5 个" in out


def test_report_id_can_be_given_directly(tmp_path):
    p, record = build_with_report(tmp_path)
    out = call(p, lambda: p.tool_get_group_report(Ev(), report_id=record["id"]))
    assert record["id"] in out


def test_legacy_report_without_json_is_explained(tmp_path):
    p, _ = build_with_report(tmp_path)
    (tmp_path / "reports" / "chat_intel_999_20261101_120000_昨天.png").write_bytes(b"png")
    out = call(
        p, lambda: p.tool_get_group_report(Ev(), report_id="chat_intel_999_20261101_120000_昨天")
    )
    assert "升级前" in out and "只有图片" in out


def test_missing_report_says_retention(tmp_path):
    p, _ = build_with_report(tmp_path)
    out = call(p, lambda: p.tool_get_group_report(Ev(), report_id="chat_intel_x_20200101_000000_无"))
    assert "找不到报告" in out
    assert "保留" in out


def test_no_report_at_all_is_explained(tmp_path):
    p, _ = make_plugin(
        reports_dir=tmp_path / "empty",
        group_ids=[GID],
        owner_uids={"110001"},
        state={},
    )
    out = call(p, lambda: p.tool_get_group_report(Ev()))
    assert "先让我生成一份" in out


def test_group_members_cannot_read_reports(tmp_path):
    p, _ = build_with_report(tmp_path)
    out = call(p, lambda: p.tool_get_group_report(Ev(gid=GID)))
    assert "私聊" in out and "群里不会" in out


def test_non_owner_cannot_read_reports(tmp_path):
    p, _ = build_with_report(tmp_path)
    out = call(p, lambda: p.tool_get_group_report(Ev(sender="123456")))
    assert "私聊" in out


# ---------------- query_group_records ----------------


def write_store(tmp_path, rows):
    folder = tmp_path / "messages"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y-%m-%d')}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_rows(n=24, base=None):
    base = base or today_base()
    return [
        {
            "gid": GID,
            "mid": f"m{i}",
            "t": int(base + i * 10),
            "uid": f"u{i % 3}",
            "name": f"人{i % 3}",
            "text": "刷完就砖了，求 EDL 包" if i % 4 == 0 else f"闲聊 {i}",
        }
        for i in range(n)
    ]


def make_query_plugin(tmp_path, rows):
    write_store(tmp_path, rows)
    p, mod = make_plugin(
        reports_dir=tmp_path / "reports",
        group_ids=[GID],
        group_name_map={GID: "極東電子考古學結社"},
        owner_uids={"110001"},
        state={},
        store_dir=tmp_path / "messages",
        max_messages=1000,
    )

    async def no_history(*_a, **_k):
        return []

    async def group_name(gid):
        return "極東電子考古學結社"

    p._fetch_history = no_history
    p._group_name = group_name
    return p


def test_keyword_query_returns_time_sender_and_id(tmp_path):
    p = make_query_plugin(tmp_path, make_rows())
    out = call(p, lambda: p.tool_query_records(Ev(), when="今天", keyword="EDL"))
    assert "命中" in out
    assert "人0" in out, "要带发送者"
    assert "id=m0" in out, "要带稳定 id，方便继续追"
    assert "砖" in out


def test_query_reports_coverage_and_limits(tmp_path):
    p = make_query_plugin(tmp_path, make_rows(60))
    out = call(p, lambda: p.tool_query_records(Ev(), when="今天", keyword="闲聊"))
    assert "本地覆盖" in out
    assert "offset=" in out or "命中 60" in out


def test_query_by_message_ids(tmp_path):
    p = make_query_plugin(tmp_path, make_rows())
    out = call(p, lambda: p.tool_query_records(Ev(), when="今天", msg_ids="m5,m7"))
    assert "id=m5" in out and "id=m7" in out
    assert "闲聊 6" not in out.split("id=m5")[0]


def test_query_by_ref_works_for_legacy_rows(tmp_path):
    """按 ref（发送者@时间）也要能查到没有 mid 的旧消息。

    时间基准必须落在「今天」窗口里：直接写 `time.time() - 900`，在 0 点到 0 点 15 分
    之间会掉到昨天，`when="今天"` 自然查不到——不是代码坏了，是基准歪了。
    """
    base = today_base(count=2, step=100)
    rows = [
        {"gid": GID, "t": base, "uid": "u7", "name": "老张", "text": "板子烧了"},
        {"gid": GID, "t": base + 100, "uid": "u8", "name": "老王", "text": "换一片"},
    ]
    p = make_query_plugin(tmp_path, rows)
    ref = f"u7@{rows[0]['t']}"
    out = call(p, lambda: p.tool_query_records(Ev(), when="今天", msg_ids=ref))
    assert "板子烧了" in out
    assert f"id={ref}" in out


def test_query_without_hits_explains_why(tmp_path):
    p = make_query_plugin(tmp_path, make_rows())
    out = call(p, lambda: p.tool_query_records(Ev(), when="今天", keyword="根本不存在的词"))
    assert "没有找到" in out
    assert "不代表没聊过" in out


def test_query_empty_window_mentions_retention(tmp_path):
    p, _mod = make_plugin(
        reports_dir=tmp_path / "reports",
        group_ids=[GID],
        group_name_map={GID: "極東電子考古學結社"},
        owner_uids={"110001"},
        state={},
        store_dir=tmp_path / "空的",
    )

    async def no_history(*_a, **_k):
        return []

    async def group_name(gid):
        return "極東電子考古學結社"

    p._fetch_history = no_history
    p._group_name = group_name
    out = call(p, lambda: p.tool_query_records(Ev(), when="昨天"))
    assert "没有找到" in out
    assert "保留" in out


def test_query_unknown_group_is_rejected(tmp_path):
    p = make_query_plugin(tmp_path, make_rows())
    out = call(p, lambda: p.tool_query_records(Ev(), gid="不存在的群"))
    assert "没有「不存在的群」这个群" in out


def test_query_denied_in_group_chat(tmp_path):
    p = make_query_plugin(tmp_path, make_rows())
    out = call(p, lambda: p.tool_query_records(Ev(gid=GID), when="今天"))
    assert "私聊" in out

"""报告存档：结构化 JSON、会话索引、读取与清理。"""

import json

from conftest import PLUGIN_DIR  # noqa: F401  (确保插件目录在 sys.path)

import reports


def records(n=6):
    return [
        {
            "gid": "220001",
            "mid": f"m{i}",
            "t": 1762500000 + i * 60,
            "uid": f"u{i % 2}",
            "name": f"人{i % 2}",
            "text": f"第 {i} 条：这个型号刷固件会砖",
        }
        for i in range(n)
    ]


def record_of(**over):
    kwargs = dict(
        report_id="chat_intel_220001_20261108_120000_今天",
        gid="220001",
        group="極東電子考古學結社",
        platform="bot_a",
        label="今天",
        since=1762500000,
        until=1762503600,
        headline="刷机翻车成了主线",
        topics=[
            {
                "title": "刷固件变砖",
                "tags": ["刷机"],
                "summary": "有人刷固件变砖，后来用 EDL 救回",
                "msg_nos": [0, 1, 2],
                "time": "10:00-10:20",
                "members": "人0、人1 等 2 人",
                "count": 3,
            },
            {
                "title": "求推荐耳机",
                "tags": ["数码"],
                "summary": "问了两款入门耳机",
                "msg_nos": [3, 4],
                "time": "10:30",
                "members": "人1",
                "count": 2,
            },
        ],
        records=records(),
        image_name="chat_intel_220001_20261108_120000_今天.png",
        images=[{"topic": 1, "file": "a.jpg", "caption": "变砖截图"}],
        incomplete=False,
        failed_chunks=[],
        chunk_total=1,
        truncated=False,
    )
    kwargs.update(over)
    return reports.build_record(**kwargs)


def test_record_carries_stable_refs_not_array_indexes(tmp_path):
    rec = record_of()
    first = rec["topics"][0]
    assert first["refs"] == ["m0", "m1", "m2"], "落盘要记稳定引用，不能只记数组下标"


def test_record_stores_no_message_text_at_all(tmp_path):
    """报告里不抄原文：要原话让模型去消息库按引用取，免得截断的快照被当成完整原话。"""
    rec = record_of()
    blob = json.dumps(rec, ensure_ascii=False)
    assert "第 0 条" not in blob, "正文不该出现在报告存档里"
    assert "evidence" not in blob
    assert "text" not in blob


def test_record_gives_legacy_messages_a_stable_ref():
    """真实库里一大半旧消息没有 mid：要用「发送者@秒级时间」回落，否则永远查不回来。"""
    legacy = [
        {"gid": "220001", "t": 1762500000 + i, "uid": "u9", "name": "老消息", "text": f"第 {i} 条"}
        for i in range(3)
    ]
    rec = record_of(
        records=legacy,
        topics=[{"title": "老话题", "tags": [], "summary": "x", "msg_nos": [0, 1], "count": 2}],
    )
    topic = rec["topics"][0]
    assert topic["refs"] == ["u9@1762500000", "u9@1762500001"], "没有 mid 也要有可回查的引用"


def test_record_keeps_report_metadata():
    rec = record_of()
    assert rec["status"] == "complete"
    assert rec["msg_count"] == 6
    assert rec["range"]["label"] == "今天"
    assert rec["covered"]["first_time"] and rec["covered"]["last_time"]
    assert rec["group"] == "極東電子考古學結社"
    assert rec["image"].endswith(".png")
    assert rec["images"][0]["caption"] == "变砖截图"


def test_incomplete_report_says_so():
    rec = record_of(incomplete=True, failed_chunks=[2], chunk_total=3)
    assert rec["status"] == "incomplete"
    assert rec["chunks"] == {"total": 3, "failed": [2]}


def test_save_and_load_roundtrip(tmp_path):
    rec = record_of()
    path = reports.save(tmp_path, rec)
    assert path is not None and path.is_file()
    back = reports.load(tmp_path, rec["id"])
    assert back["topics"][0]["title"] == "刷固件变砖"
    assert back["id"] == rec["id"]


def test_load_missing_returns_none(tmp_path):
    assert reports.load(tmp_path, "不存在的报告") is None


def test_load_broken_json_returns_none(tmp_path):
    (tmp_path / "bad.json").write_text("{不是 json", encoding="utf-8")
    assert reports.load(tmp_path, "bad") is None


def test_newest_filters_by_group(tmp_path):
    for rid, gid in (("chat_intel_111_20261108_100000_今天", None), ("chat_intel_222_20261108_110000_今天", None)):
        rec = record_of(report_id=rid, gid=gid or "111")
        reports.save(tmp_path, rec)
    assert reports.newest(tmp_path, "111", 1) == ["chat_intel_111_20261108_100000_今天"]
    assert len(reports.newest(tmp_path, None, 5)) == 2


# ---------------- 会话索引 ----------------

UMO = "bot_a:FriendMessage:110001"


def test_index_marks_generating_then_resolves():
    state = {}
    reports.mark_generating(state, UMO, gid="220001", group="考古群", label="今天")
    assert reports.entries(state, UMO)[0]["status"] == "generating"

    reports.resolve(state, UMO, record_of())
    items = reports.entries(state, UMO)
    assert len(items) == 1, "同一份报告不该留两条（占位要被替换）"
    assert items[0]["status"] == "generated"
    assert items[0]["id"] == record_of()["id"]
    assert items[0]["topics"] == ["刷固件变砖", "求推荐耳机"]


def test_index_marks_delivered():
    state = {}
    rec = record_of()
    reports.resolve(state, UMO, rec)
    assert reports.mark_delivered(state, UMO, rec["id"]) is True
    assert reports.entries(state, UMO)[0]["status"] == "delivered"


def test_index_keeps_only_recent():
    state = {}
    for i in range(6):
        rec = record_of(report_id=f"r{i}", label=f"第{i}天")
        reports.resolve(state, UMO, rec)
    items = reports.entries(state, UMO)
    assert len(items) == reports.INDEX_KEEP
    assert [e["id"] for e in items] == ["r3", "r4", "r5"]


def test_index_mark_failed():
    state = {}
    reports.mark_generating(state, UMO, gid="220001", group="考古群", label="今天")
    reports.mark_failed(state, UMO, gid="220001", label="今天", reason="模型没给出结构")
    assert reports.entries(state, UMO)[0]["status"] == "failed"


def test_index_is_per_conversation():
    state = {}
    reports.resolve(state, UMO, record_of())
    assert reports.entries(state, "bot_a:FriendMessage:999") == []


def test_blurb_is_short_and_actionable():
    state = {}
    reports.resolve(state, UMO, record_of())
    text = reports.blurb(state, UMO)
    assert "报告 ID=" in text
    assert "刷固件变砖" in text
    assert len(text) < 400, "注入提示词的部分要短"
    assert reports.blurb(state, "空会话") == ""


# ---------------- 清理 ----------------


def test_prune_removes_old_reports_and_images(tmp_path):
    import os
    import time

    old = record_of(report_id="chat_intel_220001_20260101_000000_昨天")
    reports.save(tmp_path, old)
    (tmp_path / f"{old['id']}.png").write_bytes(b"x")
    stale = time.time() - 30 * 86400
    for p in tmp_path.iterdir():
        os.utime(p, (stale, stale))

    fresh = record_of()
    reports.save(tmp_path, fresh)
    (tmp_path / f"{fresh['id']}.png").write_bytes(b"x")

    removed = reports.prune(tmp_path, keep_days=7, protect={fresh["id"]})
    assert removed == (1, 1)
    assert reports.load(tmp_path, old["id"]) is None
    assert reports.load(tmp_path, fresh["id"]) is not None


def test_prune_never_removes_protected(tmp_path):
    import os
    import time

    rec = record_of(report_id="keep_me")
    reports.save(tmp_path, rec)
    stale = time.time() - 60 * 86400
    for p in tmp_path.iterdir():
        os.utime(p, (stale, stale))
    assert reports.prune(tmp_path, keep_days=7, protect={"keep_me"}) == (0, 0)
    assert reports.load(tmp_path, "keep_me") is not None


def test_drop_expired_marks_entries_without_files(tmp_path):
    state = {}
    rec = record_of()
    reports.resolve(state, UMO, rec)
    assert reports.entries(state, UMO)[0]["status"] == "generated"
    reports.drop_expired(state, tmp_path)
    assert reports.entries(state, UMO)[0]["status"] == "expired"

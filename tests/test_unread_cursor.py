"""「最新消息」必须等于「没读过的」：任何一次完整生成的报告都算读过。

真机事故（2026-11-10）：每晚 23:00 定时报告正常生成，但「已读」边界只在**增量**跑法里
推进，定时和范围报告（incremental=False）都不动它。于是第二天问「总结电脑互助群的最新消息」，
游标还停在两天前，一次拉了 9791 条、77 个话题，把早就看过的内容又总结了一遍。
"""

import asyncio

from conftest import make_plugin
from interest import ChatInterest

GID = "1"


def _stub(p, chunk_result, records=None):
    """把一次生成需要的模型/IO 全部打桩，只留下「游标怎么动」这一条逻辑。"""
    p.chats = [ChatInterest(GID, "p", ("甲",))]
    p.group_ids = [GID]
    p.state = {"groups": {}}
    p.days = 1.0
    p.max_images = 4
    p.max_messages = 10000
    p.include_images = False
    p.ppi = 72
    p.context = None
    p._running = False
    p._load_records = lambda gid, since, until: list(
        records
        if records is not None
        else [{"t": 100, "name": "甲", "text": "说点什么", "imgs": []}]
    )
    p._fetch_history = lambda *a, **k: asyncio.sleep(0, result=[])
    p._group_name = lambda gid: asyncio.sleep(0, result="测试群")
    p._images_for_topics = lambda *a, **k: asyncio.sleep(0, result={})
    p._save_state = lambda: None
    p._prune_old_files = lambda: None

    async def fake_extract(*args, **kwargs):
        return chunk_result

    p._extract_chunk = fake_extract
    return p


def _cursor(p):
    return p.state.get("groups", {}).get(GID, {}).get("last_ts")


def test_scheduled_range_run_marks_the_window_as_read():
    """定时报告（incremental=False）生成成功，同样要推进已读边界。"""
    p, _ = make_plugin()
    _stub(p, {"topics": []})
    path, info, is_error = asyncio.run(
        p.generate_group(GID, 0, 200, "最近 24 小时", False)
    )
    assert path is None and is_error is False
    assert _cursor(p) == 200, "定时报告覆盖到哪，就该读到哪"


def test_explicit_range_run_marks_the_window_as_read():
    """「总结今天的」这种范围报告也算读过，否则「最新」会把它重跑一遍。"""
    p, _ = make_plugin()
    _stub(p, {"topics": []})
    asyncio.run(p.generate_group(GID, 0, 500, "今天", False))
    assert _cursor(p) == 500


def test_range_run_never_rewinds_the_cursor():
    """回头总结「昨天」不该把已读边界拉回去，否则今天的内容又变成未读。"""
    p, _ = make_plugin()
    _stub(p, {"topics": []})
    p.state = {"groups": {GID: {"last_ts": 5000}}}
    asyncio.run(p.generate_group(GID, 0, 2000, "昨天", False))
    assert _cursor(p) == 5000, "已读边界只进不退"


def test_incomplete_run_keeps_the_cursor_even_when_not_incremental():
    """分段失败时那一段没读到，不能算已读——非增量跑法同理。"""
    p, _ = make_plugin()
    _stub(p, None)  # 这一段直接判失败
    asyncio.run(p.generate_group(GID, 0, 200, "最近 24 小时", False))
    assert not _cursor(p), "没跑完的范围不能标成已读"


def test_latest_after_a_scheduled_report_only_covers_what_came_next():
    """核心诉求：23:00 出了报告，之后问「最新」就只该看 23:00 之后的。"""
    p, _ = make_plugin()
    _stub(p, {"topics": []})
    asyncio.run(p.generate_group(GID, 0, 23_00_00, "最近 24 小时", False))
    assert _cursor(p) == 23_00_00
    # 下一次「最新」用的起点就是这个边界
    assert p._resolve_since(GID, 0, True, 24_00_00) == 23_00_00


def test_real_report_from_a_scheduled_run_advances_the_cursor(tmp_path):
    """真报告路径（不是空话题路径）也必须推进：09:00 定时出图后「最新」只看之后的。"""
    p, _ = make_plugin()
    p.reports_dir = tmp_path
    p.chats = [ChatInterest(GID, "p", ("甲",))]
    p.group_ids = [GID]
    p.state = {"groups": {}}
    p.days = 1.0
    p.include_images = False
    p.max_images = 4
    p.max_messages = 10000
    p.ppi = 72
    p.context = None
    p._running = False
    p._load_records = lambda gid, since, until: [
        {"gid": GID, "mid": "x1", "t": 100, "uid": "u1", "name": "甲", "text": "说点什么", "imgs": []}
    ]
    p._fetch_history = lambda *a, **k: asyncio.sleep(0, result=[])
    p._group_name = lambda gid: asyncio.sleep(0, result="测试群")
    p._images_for_topics = lambda *a, **k: asyncio.sleep(0, result={})
    p._save_state = lambda: None
    p._prune_old_files = lambda: None
    p._build_transcript = lambda recs, name_map=None: ("转录", len(recs), 1)
    p._split_chunks = lambda transcript: ["第1段"]

    async def fake_extract(*_a, **_k):
        return {"topics": [{"title": "话题", "tags": ["甲"], "msg_nos": [0], "summary": "说了点"}]}

    p._extract_chunk = fake_extract
    rendered = {}

    def fake_render(payload, gid, label):
        rendered.update(payload)
        return tmp_path / "chat_intel_1_20261110_090000_最近24小时.png"

    (tmp_path / "chat_intel_1_20261110_090000_最近24小时.png").write_bytes(b"png")
    p._render = fake_render

    path, info, is_error = asyncio.run(
        p.generate_group(GID, 0, 200, "最近 24 小时", False)
    )
    assert path is not None, info
    assert _cursor(p) == 200, "出过报告的时刻就是已读边界"


def test_short_window_keeps_no_cursor_and_falls_back_to_days():
    """从没生成过报告时仍然按 days 回溯，不能因为这条改动变成 0 范围。"""
    p, _ = make_plugin()
    _stub(p, {"topics": []})
    p.days = 1.0
    assert p._resolve_since(GID, 0, True, 2 * 86400) == 2 * 86400 - 86400

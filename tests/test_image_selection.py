"""选图：明确引用优先、同一张图不重复出现、采集不再按 280 字截断。"""

import asyncio
import json
import pathlib

from conftest import make_plugin
from interest import ChatInterest

CHAT = ChatInterest("1", "p", ("电脑",), (), "", "")


def _shots(tmp_path, names):
    for i, name in enumerate(names):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n" + str(i).encode())


def _prepare(tmp_path, max_images=4):
    p, _ = make_plugin(data_dir=tmp_path, include_images=True, max_images=max_images)

    async def keep_all(provider, candidates, chat):
        return list(candidates)

    p._get_vision_provider = lambda: object()
    p._vision_picks = keep_all
    return p


def _names(entries):
    return [pathlib.Path(entry["path"]).name for entry in entries]


def test_explicit_reference_beats_span(tmp_path):
    """话题 B 明确引用第 1 条图的，不该被话题 A 的编号跨度抢走。"""
    _shots(tmp_path, ["a.jpg", "b.jpg"])
    p = _prepare(tmp_path)
    records = [
        {"t": 1, "name": "甲", "text": "先说一句", "imgs": []},
        {"t": 2, "name": "乙", "text": "截图", "imgs": ["a.jpg"]},
        {"t": 3, "name": "丙", "text": "截图", "imgs": ["b.jpg"]},
    ]
    topics = [
        {"title": "话题A", "msg_nos": [0, 2], "tags": ["电脑"], "summary": "s"},
        {"title": "话题B", "msg_nos": [1], "tags": ["电脑"], "summary": "s"},
    ]
    out = asyncio.run(p._images_for_topics(records, topics, CHAT))
    assert _names(out.get(0, [])) == ["b.jpg"], "A 只明确引用 #2"
    assert _names(out.get(1, [])) == ["a.jpg"], "B 明确引用 #1，必须给它"


def test_span_still_used_when_nothing_cited(tmp_path):
    """模型没引用纯「[图]」那一行时，跨度兜底仍然生效。"""
    _shots(tmp_path, ["a.jpg"])
    p = _prepare(tmp_path)
    records = [
        {"t": 1, "name": "甲", "text": "先说", "imgs": []},
        {"t": 2, "name": "乙", "text": "", "imgs": ["a.jpg"]},
        {"t": 3, "name": "甲", "text": "后面", "imgs": []},
    ]
    topics = [{"title": "整段", "msg_nos": [0, 2], "tags": ["电脑"], "summary": "s"}]
    out = asyncio.run(p._images_for_topics(records, topics, CHAT))
    assert _names(out[0]) == ["a.jpg"]


def test_identical_file_is_not_shown_twice(tmp_path):
    """同一个文件被转发两次（文件名不同、内容一样），报告里只出现一次。"""
    (tmp_path / "x.jpg").write_bytes(b"\x89PNG\r\nsame")
    (tmp_path / "y.jpg").write_bytes(b"\x89PNG\r\nsame")
    p = _prepare(tmp_path, max_images=4)
    records = [
        {"t": 1, "name": "甲", "text": "图", "imgs": ["x.jpg"]},
        {"t": 2, "name": "乙", "text": "再发一次", "imgs": ["y.jpg"]},
    ]
    topics = [{"title": "同一张图", "msg_nos": [0, 1], "tags": ["电脑"], "summary": "s"}]
    out = asyncio.run(p._images_for_topics(records, topics, CHAT))
    assert len(out[0]) == 1


def test_one_image_never_appears_in_two_topics(tmp_path):
    _shots(tmp_path, ["a.jpg"])
    p = _prepare(tmp_path)
    records = [{"t": 1, "name": "甲", "text": "图", "imgs": ["a.jpg"]}]
    topics = [
        {"title": "话题甲", "msg_nos": [0], "tags": ["电脑"], "summary": "s"},
        {"title": "话题乙", "msg_nos": [0], "tags": ["电脑"], "summary": "s"},
    ]
    out = asyncio.run(p._images_for_topics(records, topics, CHAT))
    assert sum(len(v) for v in out.values()) == 1


def test_judged_by_topic_so_same_image_can_be_rejudged(tmp_path):
    """同一张图出现在不同话题里要重新判（判图时带着话题标题看）。"""
    _shots(tmp_path, ["a.jpg"])
    p, mod = make_plugin(data_dir=tmp_path, include_images=True, max_images=4)
    seen: list[str] = []

    async def record_topic(provider, candidates, chat):
        for cand in candidates:
            seen.append(cand.get("topic") or "")
        return list(candidates)

    p._get_vision_provider = lambda: object()
    p._vision_picks = record_topic
    records = [{"t": 1, "name": "甲", "text": "图", "imgs": ["a.jpg"]}]
    topics = [{"title": "话题甲", "msg_nos": [0], "tags": ["电脑"], "summary": "s"}]
    asyncio.run(p._images_for_topics(records, topics, CHAT))
    assert seen == ["话题甲"]
    key_a = p._image_cache_key(CHAT, tmp_path / "a.jpg", "话题甲")
    key_b = p._image_cache_key(CHAT, tmp_path / "a.jpg", "话题乙")
    assert key_a != key_b


# ---------------------------------------------------------------- 采集保留原文
class _Event:
    def __init__(self, text):
        self.message_str = text
        self.message_obj = None
        self._platform = "bot_a"

    def get_platform_id(self):
        return self._platform

    def get_group_id(self):
        return "1"

    def get_sender_id(self):
        return "a"

    def get_sender_name(self):
        return "甲"

    def stop_event(self):
        pass


def test_long_message_is_not_cut_at_280_chars(tmp_path):
    """采集落盘要保留到 800 字：280 字的截断会把命令、报错尾部砍掉。"""
    p, _ = make_plugin(store_dir=tmp_path)
    p.group_ids = ["1"]
    p.silence_platforms = []
    p._diag_count = 0
    long_text = "开头" + "命令参数 " * 160 + "结尾标记"
    asyncio.run(p.silence_events(_Event(long_text)))
    path = next(tmp_path.glob("*.jsonl"))
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "结尾标记" in row["text"] or row["text"].endswith("（过长已截断）")
    assert len(row["text"]) > 280

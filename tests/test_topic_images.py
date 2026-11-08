"""图要挂在它所属的话题下面：只认该话题引用的消息，每个话题有张数上限。"""

import asyncio
import pathlib

import pytest

from conftest import make_plugin
from interest import ChatInterest

CHAT = ChatInterest("1", "p", ("电脑",), (), "", "")


def _shots(tmp_path, names):
    for i, name in enumerate(names):
        # 内容要各不相同：同内容的文件会被当成同一张图去重掉
        (tmp_path / name).write_bytes(b"\x89PNG\r\n" + str(i).encode())


def _records():
    return [
        {"t": 1, "name": "甲", "text": "没图", "imgs": []},
        {"t": 2, "name": "甲", "text": "截图", "imgs": ["a.jpg"]},
        {"t": 3, "name": "乙", "text": "没图", "imgs": []},
        {"t": 4, "name": "乙", "text": "截图", "imgs": ["b.jpg"]},
        {"t": 5, "name": "乙", "text": "截图", "imgs": ["c.jpg"]},
    ]


def _topics():
    return [
        {"title": "话题一", "msg_nos": [0, 1], "tags": ["电脑"], "summary": "s"},
        {"title": "话题二", "msg_nos": [2, 3, 4], "tags": ["电脑"], "summary": "s"},
    ]


def _prepare(tmp_path, max_images=2):
    p, _ = make_plugin(data_dir=tmp_path, include_images=True, max_images=max_images)

    async def keep_all(provider, candidates, chat):
        return list(candidates)

    p._get_vision_provider = lambda: object()
    p._vision_picks = keep_all
    return p


def test_describe_topics_keeps_msg_nos(tmp_path):
    """回归：_describe_topics 以前把 msg_nos 丢了，导致所有话题都配不上图。"""
    p, _ = make_plugin()
    records = [{"t": 100 + i, "name": f"人{i}", "text": f"第{i}条", "imgs": []} for i in range(6)]
    topics = [{"title": "话题", "tags": ["电脑"], "summary": "s", "msg_nos": [1, 4]}]
    out = p._describe_topics(topics, records)
    assert out[0]["msg_nos"] == [1, 4]
    assert out[0]["count"] == 2


def test_images_follow_their_own_topic(tmp_path):
    _shots(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])
    p = _prepare(tmp_path)
    out = asyncio.run(p._images_for_topics(_records(), _topics(), CHAT))
    assert sorted(out) == [0, 1]
    assert [pathlib.Path(i["path"]).name for i in out[0]] == ["a.jpg"]
    assert [pathlib.Path(i["path"]).name for i in out[1]] == ["b.jpg", "c.jpg"]


def test_images_per_topic_capped(tmp_path):
    _shots(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])
    p = _prepare(tmp_path, max_images=1)
    out = asyncio.run(p._images_for_topics(_records(), _topics(), CHAT))
    assert [len(v) for v in out.values()] == [1, 1]


def test_no_topic_images_when_disabled(tmp_path):
    _shots(tmp_path, ["a.jpg"])
    p, _ = make_plugin(data_dir=tmp_path, include_images=False, max_images=4)
    assert asyncio.run(p._images_for_topics(_records(), _topics(), CHAT)) == {}


def test_topic_without_own_images_gets_none(tmp_path):
    _shots(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])
    p = _prepare(tmp_path)
    # 话题只覆盖 #0（没图），图都在它后面 —— 不该被塞进来
    topics = [{"title": "纯文字话题", "msg_nos": [0], "tags": ["电脑"], "summary": "s"}]
    assert asyncio.run(p._images_for_topics(_records(), topics, CHAT)) == {}


def test_kept_verdicts_missing_files_are_skipped(tmp_path):
    """记录里写了图但文件不在了（被清理过），不能崩，也不能占位。"""
    p = _prepare(tmp_path)
    out = asyncio.run(p._images_for_topics(_records(), _topics(), CHAT))
    assert out == {}


def test_image_inside_topic_span_counts_even_if_not_cited(tmp_path):
    """模型经常不引用纯「[图]」那行，只要图落在话题引用的编号跨度里就归这个话题。"""
    _shots(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])
    p = _prepare(tmp_path)
    # 话题只引用了没图的 #0 和 #2，图在 #1 —— 跨度内，应归它
    topics = [{"title": "跨过去的话题", "msg_nos": [0, 2], "tags": ["电脑"], "summary": "s"}]
    out = asyncio.run(p._images_for_topics(_records(), topics, CHAT))
    assert [pathlib.Path(i["path"]).name for i in out[0]] == ["a.jpg"]


def test_one_image_belongs_to_one_topic_only(tmp_path):
    """跨度重叠时，先出现（更热）的话题先拿，同一张图不会贴两遍。"""
    _shots(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])
    p = _prepare(tmp_path, max_images=4)
    topics = [
        {"title": "话题甲", "msg_nos": [0, 1, 2], "tags": ["电脑"], "summary": "s"},
        {"title": "话题乙", "msg_nos": [1, 3, 4], "tags": ["电脑"], "summary": "s"},
    ]
    out = asyncio.run(p._images_for_topics(_records(), topics, CHAT))
    got = [pathlib.Path(i["path"]).name for v in out.values() for i in v]
    assert sorted(got) == ["a.jpg", "b.jpg", "c.jpg"]  # 每张只出现一次
    assert [pathlib.Path(i["path"]).name for i in out[0]] == ["a.jpg"]

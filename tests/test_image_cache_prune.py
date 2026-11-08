"""判图缓存清理：键里有两段指纹，取文件名要从最后一个冒号切。

键是 `兴趣指纹:话题指纹:文件名`，清理时用 `split(":", 1)[-1]` 会拿到
`话题指纹:文件名`，永远匹配不上磁盘上的文件名——于是每次清理都把整张缓存清空，
下一份报告重新判图、重新烧模型调用。
"""

import pathlib

from PIL import Image

from conftest import make_plugin
from interest import ChatInterest

GID = "220003"


def _plugin(tmp_path):
    p, _mod = make_plugin(
        chats=[ChatInterest(GID, "bot_b", ("电脑",), (), "电脑互助群")],
        group_ids=[GID],
        state={},
        reports_dir=tmp_path / "reports",
    )
    p.images_dir = tmp_path / "imgs"
    p.images_dir.mkdir()
    p._save_state = lambda: None
    return p


def _img(tmp_path, name="f980aa0dc0149345.jpg"):
    path = tmp_path / "imgs" / name
    Image.new("RGB", (40, 40), (200, 200, 200)).save(path)
    return path


def test_cache_survives_cleanup_while_the_image_is_still_there(tmp_path):
    p = _plugin(tmp_path)
    img = _img(tmp_path)
    key = p._image_cache_key(p.chats[0], img, "刷固件变砖")
    assert key.count(":") == 2, "键是 兴趣指纹:话题指纹:文件名"
    p.state["images"] = {key: {"keep": True}}

    removed = p._prune_image_cache()

    assert removed == 0, "图片还在磁盘上，缓存不该被删"
    assert key in p.state["images"]


def test_cache_is_dropped_once_the_image_is_gone(tmp_path):
    p = _plugin(tmp_path)
    img = _img(tmp_path)
    key = p._image_cache_key(p.chats[0], img, "刷固件变砖")
    p.state["images"] = {key: {"keep": True}}
    img.unlink()

    assert p._prune_image_cache() == 1
    assert p.state["images"] == {}


def test_cache_with_odd_legacy_key_is_not_kept_forever(tmp_path):
    """老键（没有话题段 / 名字里带冒号）也不能把清理逻辑带崩。"""
    p = _plugin(tmp_path)
    p.state["images"] = {"abcdef12:gone.jpg": {"keep": True}, "weird": {"keep": True}}

    assert p._prune_image_cache() == 2
    assert p.state["images"] == {}

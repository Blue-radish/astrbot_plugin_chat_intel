import pathlib

from conftest import load_main, make_plugin
from interest import ChatInterest, build_vision_prompt, interest_fingerprint


def _chat():
    return ChatInterest("1", "p", ("复古硬件", "元件行情"), ("键政",), "", "")


def test_image_cache_key_differs_by_topic():
    """同一张图在别的话题里意义不同，缓存不能直接复用。"""
    p, _ = make_plugin()
    key_a = p._image_cache_key(_chat(), pathlib.Path("a.jpg"), "装黑苹果翻车")
    key_b = p._image_cache_key(_chat(), pathlib.Path("a.jpg"), "二手显卡报价")
    assert key_a != key_b


def test_image_cache_key_differs_by_interest():
    p, _ = make_plugin()
    other = ChatInterest("1", "p", ("完全别的话题",), (), "", "")
    key_a = p._image_cache_key(_chat(), pathlib.Path("a.jpg"))
    key_b = p._image_cache_key(other, pathlib.Path("a.jpg"))
    assert key_a != key_b
    assert interest_fingerprint(_chat()) in key_a


def test_reset_image_cache_if_stale_clears_old_version():
    mod = load_main()
    p, _ = make_plugin()
    p.state = {"images_version": 3, "images": {"a.jpg": {"keep": True}}}
    p._reset_image_cache_if_stale()
    assert p.state["images"] == {}
    assert p.state["images_version"] == mod.IMG_CACHE_VERSION


def test_reset_image_cache_keeps_current_version():
    mod = load_main()
    p, _ = make_plugin()
    p.state = {"images_version": mod.IMG_CACHE_VERSION, "images": {"a.jpg": {"keep": True}}}
    p._reset_image_cache_if_stale()
    assert p.state["images"] == {"a.jpg": {"keep": True}}


def test_build_vision_prompt_lists_tags():
    prompt = build_vision_prompt(_chat(), "考古群", "上文", "本条", "下文")
    assert "复古硬件" in prompt and "元件行情" in prompt
    assert "键政" in prompt
    assert "考古群" in prompt
    assert '"keep"' in prompt

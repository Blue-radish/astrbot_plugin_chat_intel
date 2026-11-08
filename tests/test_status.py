from conftest import make_plugin
from interest import ChatInterest

RAW = [
    {"gid": "220001", "platform": "bot_b", "tags_pos": "复古硬件"},
    {"gid": "220002", "platform": "bot_a", "tags_pos": "AI 工具"},
]


def test_apply_chats_derives_group_ids_and_lookup():
    p, _ = make_plugin()
    p._apply_chats(RAW)
    assert p.group_ids == ["220001", "220002"]
    assert p._chat("220002").platform == "bot_a"
    assert p._chat("999") is None


def test_apply_chats_has_no_global_fallback_account():
    """读取账号是每群必填：没有「全局默认账号」可以顶替。"""
    p, _ = make_plugin()
    p.legacy_platform_id = "bot_b"
    p._apply_chats([{"gid": "1", "tags_pos": "x"}])
    assert p._chat("1").platform == ""


def test_status_line_shows_read_account_and_warns_missing_want():
    p, _ = make_plugin()
    chat = ChatInterest("1", "bot_b", ("复古硬件",), ())
    line = p._chat_status_line(chat, "古董群", 12, 3, "11-07 23:00")
    assert "读历史用 bot_b" in line
    assert "12 条" in line
    assert "带图 3 条" in line

    empty = ChatInterest("2", "bot_a", (), ())
    assert "未配置标签" in p._chat_status_line(empty, "空群", 0, 0, "")


def test_status_line_flags_missing_read_account():
    p, _ = make_plugin()
    chat = ChatInterest("3", "", ("复古硬件",), ())
    assert "⚠ 没填读取账号" in p._chat_status_line(chat, "某群", 0, 0, "")


def test_apply_chats_without_platform_attribute_is_safe():
    p, _ = make_plugin()
    p._apply_chats([{"gid": "1", "tags_pos": "x"}])
    assert p._chat("1").platform == ""

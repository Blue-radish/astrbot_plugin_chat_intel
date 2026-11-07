from conftest import make_plugin
from interest import ChatInterest

RAW = [
    {"gid": "220001", "platform": "bot_b", "tags_pos": "复古硬件"},
    {"gid": "220002", "platform": "bot_a", "tags_pos": "AI 工具"},
]


def test_apply_chats_derives_group_ids_and_lookup():
    p, _ = make_plugin(platform_id="bot_b")
    p._apply_chats(RAW)
    assert p.group_ids == ["220001", "220002"]
    assert p._chat("220002").platform == "bot_a"
    assert p._chat("999") is None


def test_apply_chats_falls_back_to_platform_id():
    p, _ = make_plugin(platform_id="bot_b")
    p._apply_chats([{"gid": "1", "tags_pos": "x"}])
    assert p._chat("1").platform == "bot_b"


def test_status_line_shows_collect_platform_and_warns_missing_want():
    p, _ = make_plugin()
    chat = ChatInterest("1", "bot_b", ("复古硬件",), (), "", "")
    line = p._chat_status_line(chat, "古董群", 12, 3, "11-07 23:00")
    assert "读历史用 bot_b" in line
    assert "12 条" in line
    assert "带图 3 条" in line

    empty = ChatInterest("2", "bot_a", (), (), "", "")
    assert "未配置标签" in p._chat_status_line(empty, "空群", 0, 0, "")


def test_apply_chats_without_platform_id_attribute():
    """__init__ 里的调用顺序不该被 _apply_chats 依赖：实例上还没有 platform_id 时也要能解析。"""
    p, _ = make_plugin()
    p._apply_chats([{"gid": "1", "tags_pos": "x"}])
    assert p._chat("1").platform == ""

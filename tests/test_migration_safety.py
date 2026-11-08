"""老配置迁移的两条安全线：

1. 旧推送目标只迁**私聊**。`oldbot:GroupMessage:123` 以前会被拼成
   `oldbot:FriendMessage:123`——那是给另一个用户私聊发报告，不是原来的群。
2. 配置写不回磁盘时不能打「已迁移」标记，否则下次启动读到旧配置却跳过迁移，
   推送目标静默变成空。
"""

import pathlib

import pytest

from conftest import load_main, make_plugin

PLUGIN_NAME = "astrbot_plugin_chat_intel"


def _plugin(config: dict, state: dict | None = None, saver=None, **attrs):
    p, mod = make_plugin(state=state if state is not None else {}, reports_dir=pathlib.Path("unused"), **attrs)
    p.legacy_platform_id = ""
    p._save_state = lambda: None

    class Cfg(dict):
        def save_config(self):
            if saver is None:
                return None
            saver()

    p.config = Cfg(config)
    return p, mod


# ---------------------------------------------------------------- 只迁私聊
def test_group_push_target_is_not_migrated():
    p, mod = _plugin({"push_umo": "oldbot:GroupMessage:123"})
    p._migrate_legacy_config()

    assert p.config.get("push_platform") in (None, ""), "群聊目标不能变成私聊收件人"
    assert p.config.get("push_uid") in (None, "")
    assert p.push_umo == "", "宁可不推送，也不发给错误的人"
    assert "私聊" in (p._push_cfg_error or ""), "要明确告诉用户重新配置"


def test_private_legacy_push_target_still_migrates():
    p, mod = _plugin({"push_umo": "bot_a:FriendMessage:110001"})
    p._migrate_legacy_config()

    assert p.config.get("push_platform") == "bot_a"
    assert p.config.get("push_uid") == "110001"
    assert p.push_umo == "bot_a:FriendMessage:110001"
    assert p._push_cfg_error == ""


def test_unparsable_legacy_target_is_dropped_with_a_note():
    p, mod = _plugin({"push_umo": "乱七八糟"})
    p._migrate_legacy_config()

    assert p.push_umo == ""
    assert p._push_cfg_error, "解析不了也要说清楚"


# ---------------------------------------------------------------- 保存失败可重试
def test_failed_config_save_keeps_migration_retryable():
    def boom():
        raise RuntimeError("磁盘满")

    p, mod = _plugin({"push_umo": "bot_a:FriendMessage:110001"}, saver=boom)
    p._migrate_legacy_config()

    assert p.state.get("config_migrated_v224") is None, "写不回配置就别打标记"

    # 重启：读到旧配置、状态里也没有标记 → 这次保存成功
    p2, _ = _plugin({"push_umo": "bot_a:FriendMessage:110001"}, state=dict(p.state))
    p2._migrate_legacy_config()

    assert p2.config.get("push_uid") == "110001"
    assert p2.push_umo == "bot_a:FriendMessage:110001", "重试后推送目标要回来"
    assert p2.state.get("config_migrated_v224") is True


def test_successful_migration_marks_done():
    p, mod = _plugin({"push_umo": "bot_a:FriendMessage:110001"})
    p._migrate_legacy_config()

    assert p.state.get("config_migrated_v224") is True


def test_note_survives_restart():
    """旧目标被拒之后重启：提醒不能跟着消失，否则推送静默失效没人看得出原因。"""
    p, mod = _plugin({"push_umo": "oldbot:GroupMessage:123"}, saver=lambda: None)
    p._migrate_legacy_config()
    note = p._push_cfg_error
    assert note

    # 重启：配置还是那份旧的群聊目标，状态里带着标记和提醒
    p2, _ = _plugin({"push_umo": "oldbot:GroupMessage:123"}, state=dict(p.state))
    p2.push_umo, p2._push_cfg_error = p2.build_push_target(p2.config)  # __init__ 先算一遍
    p2._migrate_legacy_config()

    assert p2.push_umo == ""
    assert p2._push_cfg_error == note


def test_already_migrated_state_skips_work():
    """已经迁过：只按现有字段算目标，不再碰配置。"""
    cfg = {"push_platform": "bot_a", "push_uid": "110001"}
    target, note = load_main().TechDigestPlugin.migrate_push_target(
        dict(cfg), {"config_migrated_v224": True}
    )
    assert target == "bot_a:FriendMessage:110001" and note == ""

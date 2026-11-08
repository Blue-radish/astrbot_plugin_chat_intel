"""报告标题：群名/备注名 + 全局标题，不再给每个群单独设标题。"""

import json
from pathlib import Path

import interest
from conftest import load_main

PLUGIN = Path(__file__).resolve().parent.parent


def _plugin(report_title: str = "群聊情报"):
    mod = load_main()
    p = object.__new__(mod.TechDigestPlugin)
    p.report_title = report_title
    return p


def _chat(name: str = "", gid: str = "220003"):
    return interest.ChatInterest(gid, "bot_b", ("电脑",), (), name)


def test_title_is_group_name_plus_global_title():
    assert _plugin()._report_title_for(_chat(), "电脑互助群") == "电脑互助群 · 群聊情报"


def test_remark_name_wins_over_group_name():
    p = _plugin()
    assert p._report_title_for(_chat(name="老电脑群"), "电脑互助群") == "老电脑群 · 群聊情报"


def test_blank_global_title_leaves_just_the_name():
    assert _plugin(report_title="")._report_title_for(_chat(), "电脑互助群") == "电脑互助群"


def test_unreadable_group_name_falls_back_to_gid():
    """_group_name 认不出群名时给的是占位词「群聊」，那种情况用群号更清楚。"""
    p = _plugin()
    assert p._report_title_for(_chat(), "群聊") == "220003 · 群聊情报"
    assert p._report_title_for(_chat(), "") == "220003 · 群聊情报"


def test_chat_interest_drops_the_title_field():
    assert "title" not in interest.ChatInterest.__dataclass_fields__


def test_schema_drops_per_group_title():
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    items = schema["chats"]["templates"]["chat"]["items"]
    assert "title" not in items, "每群单独设标题的开关已经删掉"
    assert "name" in items, "备注名还在，它就是标题的来源"

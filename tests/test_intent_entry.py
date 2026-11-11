"""插件不在模型之前动手：自然语言一律先交给模型理解。

这轮架构改动的主线：撤掉按关键词直通生成/重跑/重发/取消的入口，
`intent.py` 只剩一个用途——没配 owner 时提示一次怎么配。
"这份群聊情报总结得真好"当初就是在这条前置入口被当成新请求的。
"""

import asyncio

from conftest import make_plugin
from intent import mentions_plugin

UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"


class Event:
    def __init__(self, text, gid=""):
        self.message_str = text
        self.unified_msg_origin = UMO
        self._gid = gid
        self.stopped = False

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return OWNER

    def get_platform_id(self):
        return "bot_a"

    def stop_event(self):
        self.stopped = True


def make(**attrs):
    p, _mod = make_plugin(group_ids=["g1"], group_name_map={"g1": "群1"}, state={}, **attrs)
    p._save_state = lambda: None
    submitted = []

    async def fake_submit(req):
        submitted.append(req)
        return "accepted", "开始生成"

    p._submit_digest = fake_submit
    sent = []

    async def fake_send(umo, text):
        sent.append(text)
        return True

    p._send_text = fake_send
    return p, submitted, sent


def test_no_message_is_stopped_when_owner_is_configured():
    """配好了 owner：插件一句话都不抢，全部由模型理解并选工具。"""
    p, submitted, sent = make(owner_uids={OWNER})
    for text in (
        "群聊情报今天聊了啥",
        "这份群聊情报总结得真好",
        "你发的日报总结得很好",
        "总结一下昨天的群",
        "日报第二条有什么依据",
        "再发我一遍刚才那份",
        "取消这次总结",
        "跑到哪了",
    ):
        ev = Event(text)
        asyncio.run(p.hint_missing_owner(ev))
        assert ev.stopped is False, f"插件不该替模型决定「{text}」"
    assert submitted == [] and sent == []


def test_group_messages_are_never_touched():
    p, submitted, sent = make(owner_uids=[])
    ev = Event("群聊情报跑一份", gid="g1")
    asyncio.run(p.hint_missing_owner(ev))
    assert sent == [] and ev.stopped is False


def test_missing_owner_gets_one_bounded_hint():
    p, submitted, sent = make(owner_uids=[])
    ev = Event("群聊情报今天有什么")
    asyncio.run(p.hint_missing_owner(ev))
    assert ev.stopped is True, "该说清为什么不能生成"
    assert sent and "owner_uids" in sent[0]
    assert submitted == [], "提示归提示，不执行任何动作"


def test_smalltalk_without_plugin_mention_is_left_alone():
    p, submitted, sent = make(owner_uids=[])
    ev = Event("今天天气不错")
    asyncio.run(p.hint_missing_owner(ev))
    assert ev.stopped is False and sent == []


def test_anchor_is_the_plugin_not_the_action():
    """指向词只认插件名，不认「总结」这类动作词——动作词归模型理解。"""
    assert mentions_plugin("群聊情报今天聊了啥") is True
    assert mentions_plugin("日报跑一份") is True
    assert mentions_plugin("总结一下这段代码") is False
    assert mentions_plugin("群里今天有什么好玩的") is False

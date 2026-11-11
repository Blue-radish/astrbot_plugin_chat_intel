"""大白话直通入口不该被「夸奖」触发。

用户夸一句「这份群聊情报总结得真好」，插件曾经直接停掉事件、起一次生成，
模型连解释的机会都没有——这就是 2.2.24 要修的那个故障的另一半：
投递快照只能影响模型，管不到这个在模型之前就动手的入口。

判定改成看**用法**而不是加词表：`总结得真好` 是补语，`这个群总结` 是名词短语，
两者都不是「要一份新的」。请求句照旧命中，一条都不许漏。
"""

import asyncio

from conftest import make_plugin
from intent import detect_intent

UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"

PRAISE = (
    "这份群聊情报总结得真好",
    "你发的日报总结得很好",
    "做得真好，这个群总结",
    "做得真好，这个群总结的报告图",
)

REQUESTS = (
    "群聊情报今天聊了啥",
    "帮我总结一下今天的群聊情报",
    "总结一下昨天的群",
    "群聊总结做一份今天的",
    "总结得详细一点，群聊情报",
    "群里今天聊了啥",
    "群聊情报把今天的MeowShelter跑一份",
)


class Event:
    def __init__(self, text):
        self.message_str = text
        self.unified_msg_origin = UMO
        self.stopped = False

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return OWNER

    def get_platform_id(self):
        return "bot_a"

    def stop_event(self):
        self.stopped = True


def make():
    p, _mod = make_plugin(
        owner_uids={OWNER},
        group_ids=["g1"],
        group_name_map={"g1": "群1"},
        state={},
        reports_dir="unused",
    )
    p._save_state = lambda: None
    spawned = []

    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()  # 只关心「有没有起生成」，不真跑
        return object()

    p._spawn = fake_spawn
    return p, spawned


def test_praise_is_not_a_digest_request():
    for text in PRAISE:
        assert detect_intent(text) is None, f"夸奖被当成请求：{text}"


def test_real_requests_still_match():
    for text in REQUESTS:
        intent = detect_intent(text)
        assert intent is not None and intent.kind == "digest", f"正常请求被漏掉：{text}"


def test_praise_does_not_reach_the_entry(monkeypatch):
    """夸奖句进入真实入口后：不停事件、不起生成、留给模型正常回话。"""
    p, spawned = make()
    for text in PRAISE:
        event = Event(text)
        asyncio.run(p.intercept_digest_intent(event))
        assert event.stopped is False, f"夸奖把事件停了，模型没机会说话：{text}"
    assert spawned == [], "夸一句又跑一次生成"


def test_request_does_reach_the_entry():
    p, spawned = make()
    event = Event("群聊情报今天聊了啥")
    asyncio.run(p.intercept_digest_intent(event))
    assert event.stopped is True, "明确的请求该由插件直接接手"
    assert len(spawned) == 1

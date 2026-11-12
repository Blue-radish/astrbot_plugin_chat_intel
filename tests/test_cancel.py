"""取消：叫停这个会话还没跑完的生成。

以前只有「发起」和「重跑」，满了就拒绝，中途不想看了没辙。现在：
- 在跑的那份掐掉（任务真被取消，不是只改索引）
- 排队的清空
- 占位统一收成「已取消」，不是「生成失败」
- 只动自己的会话：定时档（没有请求对象）和别人会话不碰
"""

import asyncio
import pathlib

import reports
from conftest import make_plugin
from interest import ChatInterest

UMO = "bot_a:FriendMessage:110001"
OTHER = "bot_a:FriendMessage:999"
GID, GID2 = "220003", "220002"


def fresh(**attrs):
    sent = []

    async def send_text(umo, text):
        sent.append(text)
        return True  # 假的发送也要如实返回成败：工具现在按返回值决定要不要让模型闭嘴

    p, mod = make_plugin(
        chats=[ChatInterest(GID, "bot_b", ("电脑",), (), "电脑互助群"), ChatInterest(GID2, "bot_b", ("电脑",), (), "MeowShelter")],
        group_ids=[GID, GID2],
        group_name_map={GID: "电脑互助群", GID2: "MeowShelter"},
        owner_uids={"110001"},
        state={},
        reports_dir=pathlib.Path("unused"),
        **attrs,
    )
    p._save_state = lambda: None
    p._send_text = send_text
    return p, mod, sent


def generating(p, umo=UMO):
    return [e for e in reports.entries(p.state, umo) if reports.status_of(e) == "generating"]


def test_cancel_with_nothing_running_says_so():
    p, mod, _ = fresh()
    assert asyncio.run(p.cancel_digest(UMO)) == "现在没有在跑或排队的生成"


class FakeTask:
    """只够 cancel_digest 用：判断在不在跑、发取消、加回调。"""

    def __init__(self):
        self.cancelled = False
        self.callbacks = []

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True

    def add_done_callback(self, cb):
        self.callbacks.append(cb)


def test_cancel_clears_queue_and_settles_placeholders():
    p, mod, _ = fresh(_running=False)
    p._spawn = lambda coro: (coro.close(), FakeTask())[1]  # 只关心队列和占位
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID)))
    p._running = True  # 第二份只能排队
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID2)))
    assert len(p._queue) == 1

    text = asyncio.run(p.cancel_digest(UMO))

    assert p._queue == type(p._queue)() or len(p._queue) == 0
    assert generating(p) == []
    assert all(e["fail_reason"] == "已取消（用户叫停）" for e in reports.entries(p.state, UMO))
    assert "取消" in text


def test_cancel_only_touches_its_own_session():
    p, mod, _ = fresh(_running=False)
    p._running = True
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID)))
    asyncio.run(p._submit_digest(mod.DigestRequest(OTHER, 0, 1, "今天", True, GID2)))

    asyncio.run(p.cancel_digest(UMO))

    assert [req.umo for req in p._queue] == [OTHER], "别人（或别的会话）排队的不能被清掉"
    assert [e["gid"] for e in generating(p, OTHER)] == [GID2]


def test_cancel_really_cancels_the_running_task():
    p, mod, _ = fresh(_running=False)
    started = {"count": 0}

    async def slow(*_a, **_k):
        started["count"] += 1
        await asyncio.sleep(30)

    p._generate_and_send = slow

    async def scenario():
        await p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID))
        task = p._current_task
        assert task is not None and not task.done()
        text = await p.cancel_digest(UMO)
        await asyncio.sleep(0.05)
        return task, text

    task, text = asyncio.run(scenario())
    assert task.cancelled() or task.done(), "在跑的任务必须真的停掉"
    assert p._running is False, "取消之后执行位要放开，不然以后都提交不了"
    assert "在跑" in text and "取消" in text


def test_cancel_does_not_touch_the_daily_run():
    """定时档没有请求对象：取消不该把它掐了。"""
    p, mod, _ = fresh()

    async def slow_daily():
        await asyncio.sleep(30)

    p._run_daily_once = slow_daily

    async def scenario():
        task = p._spawn(p._execute(None))
        await asyncio.sleep(0)
        text = await p.cancel_digest(UMO)
        alive = not task.done()
        task.cancel()
        await asyncio.sleep(0.05)
        return text, alive

    text, alive = asyncio.run(scenario())
    assert alive, "定时档不该被用户的「取消」掐掉"
    assert text == "现在没有在跑或排队的生成"


class FakeEvent:
    def __init__(self, text="群聊情报取消", umo=UMO):
        self.unified_msg_origin = umo
        self.message_str = text
        self.extras = {}

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


def test_tool_cancel_replies_itself():
    p, mod, sent = fresh(_running=False)
    p._tool_allowed = lambda event: True
    p._running = True
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID)))

    out = asyncio.run(p.tool_cancel_digest(FakeEvent()))

    assert sent and "取消" in sent[0], sent
    # 回执由插件发（sent），工具结果里同时留下「取消成功」和「通知成功」两个事实，
    # 模型据此闭嘴；但事实不能被抹掉——下一轮它得知道刚才取消了什么。
    assert "受理结果=sent" in out, out
    assert "取消" in out, out
    assert "不要再输出" in out
    assert len(p._queue) == 0

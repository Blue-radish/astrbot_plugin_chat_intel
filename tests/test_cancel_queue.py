"""取消之后队列要接着跑；取消回执发不出去时要说出来。

审查者用隔离任务复现的漏洞：手动生成在跑、后面排着定时档，用户取消 → CancelledError 从
`_execute` 里 raise 出去，后面的取队列代码永远执行不到。定时档就卡在队列里，直到重启。
"""

import asyncio
import pathlib

import reports
from conftest import make_plugin
from interest import ChatInterest

UMO = "bot_a:FriendMessage:110001"
OTHER = "bot_a:FriendMessage:999"
GID = "220003"


def fresh(**attrs):
    sent = []

    async def send_text(umo, text):
        sent.append(text)
        return True

    p, mod = make_plugin(
        chats=[ChatInterest(GID, "bot_b", ("电脑",), (), "电脑互助群")],
        group_ids=[GID],
        group_name_map={GID: "电脑互助群"},
        owner_uids={"110001"},
        state={},
        reports_dir=pathlib.Path("unused"),
        **attrs,
    )
    p._save_state = lambda: None
    p._send_text = send_text
    p._running = False
    return p, mod, sent


def test_user_cancel_lets_the_scheduled_job_continue():
    """取消手动那份之后，队列里的定时档必须继续跑。"""

    async def scenario():
        p, mod, _sent = fresh()
        started, finished = [], []

        async def slow_generate(*_a, **_kw):
            started.append("manual")
            await asyncio.sleep(30)  # 这里会被用户取消

        async def daily():
            finished.append("daily")

        p._generate_and_send = slow_generate
        p._run_daily_once = daily
        p._spawn = lambda coro: asyncio.create_task(coro)
        p._tasks = set()

        await p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID))
        assert p._enqueue(None), "定时档要能排进去"
        await asyncio.sleep(0.05)

        message = await p.cancel_digest(UMO)
        assert "掐掉" in message
        await asyncio.sleep(0.05)

        assert finished == ["daily"], "取消手动那份后，定时档要继续跑，不能停在队列里"
        assert p._running is False, "跑完要放开执行位"
        assert p._current_req is None
        assert not p._queue

    asyncio.run(scenario())


def test_user_cancel_does_not_touch_other_sessions_queue():
    """别的会话排着的请求也不能被这次取消连坐。"""

    async def scenario():
        p, mod, _sent = fresh()

        async def slow_generate(*_a, **_kw):
            await asyncio.sleep(30)

        ran = []

        async def generate_other(*_a, **_kw):
            ran.append("other")

        p._generate_and_send = slow_generate
        p._run_daily_once = lambda: asyncio.sleep(0, result=None)
        p._spawn = lambda coro: asyncio.create_task(coro)
        p._tasks = set()

        await p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID))
        other = mod.DigestRequest(OTHER, 0, 1, "今天", True, GID)
        assert p._enqueue(other)
        await asyncio.sleep(0.05)

        await p.cancel_digest(UMO)
        p._generate_and_send = generate_other
        await asyncio.sleep(0.05)

        assert ran == ["other"], "队列里别的会话要接着跑"
        assert not p._queue

    asyncio.run(scenario())


def test_unload_still_stops_the_whole_loop():
    """卸载时不能「继续消费队列」：那会在卸载后继续发消息、写状态。"""

    async def scenario():
        p, mod, _sent = fresh()

        async def slow_generate(*_a, **_kw):
            await asyncio.sleep(30)

        consumed = []

        async def daily():
            consumed.append("daily")

        p._generate_and_send = slow_generate
        p._run_daily_once = daily
        p._spawn = lambda coro: asyncio.create_task(coro)
        p._tasks = set()

        await p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID))
        assert p._enqueue(None)
        await asyncio.sleep(0.05)

        p._unloading = True
        await p.terminate()
        await asyncio.sleep(0.05)

        assert consumed == [], "卸载后不许再跑队列"

    asyncio.run(scenario())


def test_tool_cancel_reports_when_the_receipt_failed_to_send():
    """回执发不出去时必须让模型转告，不能让它闭嘴。"""

    async def scenario():
        p, mod, sent = fresh()
        p._tool_allowed = lambda event: True

        async def failing_send(umo, text):
            sent.append(text)
            return False

        p._send_text = failing_send
        p._running = True
        await p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID))

        out = await p.tool_cancel_digest(FakeEvent())
        assert "没发出去" in out or "发送失败" in out, out
        assert "取消" in out, "取消这个事实要留在工具结果里"
        assert "不要再输出" not in out, "回执没发出去时不能让模型闭嘴"

    asyncio.run(scenario())


class FakeEvent:
    unified_msg_origin = "bot_a:FriendMessage:110001"

    def __init__(self, text=""):
        self.message_str = text

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return "110001"

    def get_platform_id(self):
        return "bot_a"

    def set_extra(self, key, value):
        pass

    def plain_result(self, text):
        from types import SimpleNamespace

        return SimpleNamespace(chain=[SimpleNamespace(text=text)])

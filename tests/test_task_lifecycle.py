"""后台任务的生命周期：谁起的任务谁负责收。

以前两处 `asyncio.create_task(...)` 都不留引用，插件卸载/重载时没人能取消它们，
孤儿任务还会继续写状态文件、发消息；另外还有一条「占位超过 1 小时就强放」的兜底，
大群跑得久会被误判成死在半路。
"""

import asyncio
import time

import reports
from conftest import make_plugin
from interest import ChatInterest


def _ready_plugin():
    p, mod = make_plugin()
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.group_name_map = {"1": "测试群"}
    p.smart_parse = False
    p._save_state = lambda: None

    async def gname(_gid):
        return "测试群"

    p._group_name = gname
    return p, mod


def _request(mod):
    return mod.DigestRequest(umo="u", since=0, until=1, label="今天", incremental=False)


def test_submitted_task_is_held_then_released():
    """起了任务就要持有引用；跑完自动摘掉，不留悬挂引用。"""
    p, mod = _ready_plugin()
    done = asyncio.Event()

    async def quick(*_a, **_k):
        done.set()

    p._generate_and_send = quick

    async def scenario():
        status, _msg = await p._submit_digest(_request(mod))
        assert status == "accepted"
        assert len(p._tasks) == 1, "提交后必须有一个被持有的任务"
        await asyncio.wait_for(done.wait(), 2)
        await asyncio.sleep(0)  # 让 done_callback 跑完
        assert not p._tasks, "任务结束后引用要自动丢掉"

    asyncio.run(scenario())


def test_terminate_cancels_inflight_generation():
    """卸载时把在跑的任务掐掉，并放开执行位。"""
    p, mod = _ready_plugin()
    running = asyncio.Event()

    async def slow(*_a, **_k):
        running.set()
        await asyncio.sleep(3600)

    p._generate_and_send = slow

    async def scenario():
        await p._submit_digest(_request(mod))
        await asyncio.wait_for(running.wait(), 2)
        assert p._running is True
        await p.terminate()
        assert not p._tasks, "取消后不能留下悬挂任务"
        assert p._running is False, "取消后执行位必须放开，否则下一份永远排队"
        assert p._claim() is True, "取消之后还能再抢"

    asyncio.run(scenario())


def test_cancelled_generation_is_marked_failed():
    """被取消的那份不能永远停在「正在生成」，否则「跑完了吗」永远答「还在跑」。"""
    p, mod = _ready_plugin()
    running = asyncio.Event()

    async def slow(*_a, **_k):
        running.set()
        await asyncio.sleep(3600)

    p._generate_and_send = slow

    async def scenario():
        await p._submit_digest(_request(mod))
        await asyncio.wait_for(running.wait(), 2)
        assert reports.status_of(reports.entries(p.state, "u")[-1]) == "generating"
        await p.terminate()
        entry = reports.entries(p.state, "u")[-1]
        assert reports.status_of(entry) == "failed"
        assert "取消" in entry.get("fail_reason", "")

    asyncio.run(scenario())


def test_tool_entry_task_is_held_too():
    """工具入口起的那条也要被持有：它同样会在卸载时变成孤儿。"""
    p, mod = _ready_plugin()
    p.owner_uids = {"110001"}
    p.owner_session = "bot_a:FriendMessage:110001"
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_execute(request):
        started.set()
        await release.wait()

    p._execute = fake_execute

    class Ev:
        message_str = "群聊情报今天群里聊了啥"
        unified_msg_origin = "bot_a:FriendMessage:110001"

        def get_sender_id(self):
            return "110001"

        def get_platform_id(self):
            return "bot_a"

        def get_group_id(self):
            return ""

        def stop_event(self):
            pass

        def is_stopped(self):
            return False

        def plain_result(self, text):
            from types import SimpleNamespace

            return SimpleNamespace(chain=[SimpleNamespace(text=text)])

    async def scenario():
        p._send_text = lambda *_a: asyncio.sleep(0, result=False)
        await p.tool_group_digest(Ev(), when="今天")
        await asyncio.wait_for(started.wait(), 2)
        assert len(p._tasks) == 1, "工具入口起的任务也要被持有"
        await p.terminate()
        assert not p._tasks

    asyncio.run(scenario())


def test_spawn_tracks_and_forgets():
    """集合的记账本身：起来就在里面，结束就摘掉（定时器走同一条路）。"""
    p, _ = make_plugin()

    async def scenario():
        task = p._spawn(asyncio.sleep(3600))
        assert task in p._tasks, "起完必须在集合里"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        assert task not in p._tasks, "结束后要自动摘掉"

    asyncio.run(scenario())

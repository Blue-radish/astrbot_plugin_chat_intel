"""任务占位的完整生命周期：受理才登记，成功/无结果/失败/取消/卸载都要收尾。

以前 `_submit_digest()` 在判断「跑不跑」之前就登记「正在生成」，于是：
- 队列满被拒 → 索引里留一条永远跑不完的「正在生成」
- 「全部群」占位用空群号，各群报告按真实群号保存，谁也认领不到它
- 跑完但没命中话题 → 执行结束，索引还写着生成中
- 卸载时排队的请求没人收尾
"""

import asyncio
import pathlib

import reports
from conftest import make_plugin
from interest import ChatInterest

UMO = "bot_a:FriendMessage:110001"
GID, GID2 = "220003", "220002"


def fresh(**attrs):
    from collections import deque

    p, mod = make_plugin(
        chats=[ChatInterest(GID, "bot_b", ("电脑",)), ChatInterest(GID2, "bot_b", ("电脑",))],
        group_ids=[GID, GID2],
        group_name_map={GID: "电脑互助群", GID2: "MeowShelter"},
        owner_uids={"110001"},
        state={},
        reports_dir=pathlib.Path(attrs.pop("reports_dir", None) or "unused"),
        **attrs,
    )
    p._save_state = lambda: None
    p._send_text = lambda *a, **k: asyncio.sleep(0)
    if attrs.pop("no_spawn", True):
        # 只关心索引里登记了什么：真起后台任务的话，循环一关任务就被取消，
        # _execute 的收尾会把占位结算掉，断言就测跑偏了
        p._spawn = lambda coro: (coro.close(), object())[1]
    return p, mod


def generating(p):
    return [e for e in reports.entries(p.state, UMO) if reports.status_of(e) == "generating"]


def test_rejected_request_leaves_no_placeholder():
    """队列满、请求被拒：不能留下「正在生成」。"""
    from collections import deque

    p, mod = fresh(_running=True, _queue=deque(maxlen=2))
    p._queue.append(mod.DigestRequest(UMO, 0, 1, "今天", False, GID))
    p._queue.append(mod.DigestRequest(UMO, 0, 1, "今天", False, GID2))

    status, msg = asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GID)))

    assert status == "rejected" and "排着" in msg
    assert generating(p) == [], "没受理就别登记"


def test_queued_request_is_still_shown_as_in_progress():
    """排队了但确实受理了：这时候登记「正在生成」是对的。"""
    p, mod = fresh(_running=True)
    status, msg = asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GID)))
    assert status == "queued" and "排队" in msg, "排队和拒绝必须能分开，不能都返回同一件事"
    assert [e["gid"] for e in generating(p)] == [GID]


def test_all_groups_registers_one_placeholder_per_group():
    """「全部群」要按真实群号各登记一条，否则永远没人认领。"""
    p, mod = fresh()
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "最近24小时", False, None)))
    rows = generating(p)
    assert sorted(e["gid"] for e in rows) == sorted([GID, GID2])
    assert {e["group"] for e in rows} == {"电脑互助群", "MeowShelter"}
    assert all(e["label"] == "最近24小时" for e in rows)


def test_each_group_result_replaces_its_own_placeholder(tmp_path):
    p, mod = fresh(reports_dir=tmp_path / "reports")
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "最近24小时", False, None)))
    for gid, name in ((GID, "电脑互助群"), (GID2, "MeowShelter")):
        record = reports.build_record(
            report_id=f"chat_intel_{gid}_20261109_000000_最近24小时",
            gid=gid,
            group=name,
            platform="bot_b",
            label="最近24小时",
            since=0,
            until=1,
            topics=[{"title": "话题"}],
            records=[],
            image_name="x.png",
            headline="",
        )
        reports.resolve(p.state, UMO, record)

    assert generating(p) == [], "每个群的报告都应该换掉自己那条占位"
    assert len(reports.entries(p.state, UMO)) == 2


def test_no_matching_topics_settles_the_placeholder():
    """跑完但一条都没命中：执行结束了，索引也得结束。"""
    p, mod = fresh()
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GID)))

    async def generate_all(since, until, label, incremental, only_gid, umo):
        return [(GID, None, "电脑互助群：今天没有命中标签的话题", False)]

    p.generate_all = generate_all
    asyncio.run(p._generate_and_send(UMO, 0, 1, "今天", False, GID, announce=True))

    rows = reports.entries(p.state, UMO)
    assert [reports.status_of(e) for e in rows] == ["failed"]
    assert "没有命中" in rows[0]["fail_reason"]


def test_failure_settles_every_target_of_the_batch():
    """整批抛异常时，别只把最后一条标失败、其他几条永远停在生成中。"""
    p, mod = fresh()
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, None)))

    async def boom(*_a, **_k):
        raise RuntimeError("模型炸了")

    p.generate_all = boom
    asyncio.run(p._generate_and_send(UMO, 0, 1, "今天", False, None, announce=True))

    assert generating(p) == []
    assert all("模型炸了" in e["fail_reason"] for e in reports.entries(p.state, UMO))


def test_terminate_settles_running_and_queued():
    """卸载：正在跑的和排队的一起收尾，别留下「永远在生成」。"""
    p, mod = fresh(_running=False, no_spawn=False)

    async def slow(*_a, **_k):
        await asyncio.sleep(30)

    p._generate_and_send = slow

    async def scenario():
        await p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GID))  # 真起任务
        p._running = True  # 第二份只能排队
        await p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GID2))
        assert len(generating(p)) == 2
        await p.terminate()

    asyncio.run(scenario())

    assert generating(p) == []
    assert all("卸载" in e["fail_reason"] for e in reports.entries(p.state, UMO))
    assert len(p._queue) == 0, "排队请求也要清掉，不能挂在队列里等下次"


def test_daily_run_does_not_touch_placeholders():
    """定时档没有占位（没人问），收尾逻辑不该凭空造出条目。"""
    p, _mod = fresh()

    async def generate_all(since, until, label, incremental, only_gid, umo):
        return [(GID, None, "电脑互助群：没有命中标签的话题", False)]

    p.generate_all = generate_all
    asyncio.run(p._generate_and_send(UMO, 0, 1, "今天", False, None, announce=False))

    assert reports.entries(p.state, UMO) == []

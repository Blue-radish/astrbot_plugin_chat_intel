"""还在跑的任务不能被「最近三份」的截断挤掉，也不能被同群的下一次请求顶掉。

「正在生成」的占位和已生成的报告以前共用一个列表，而 `_store()` 只留最后 3 条：
- 一次总结四个群 → 第一个群的占位被截掉，「还有哪份没跑完」漏报
- 同群同标签连提两次 → 第二次登记时把第一次的占位顶掉，第一份跑完后队列里
  还有一份在跑，索引却只显示已完成
"""

import asyncio
import pathlib

import reports
from conftest import make_plugin
from interest import ChatInterest

UMO = "bot_a:FriendMessage:110001"
GIDS = ["220003", "220002", "220001", "123456789"]


def fresh(**attrs):
    p, mod = make_plugin(
        chats=[ChatInterest(g, "bot_b", ("电脑",)) for g in GIDS],
        group_ids=list(GIDS),
        group_name_map={g: f"群{g}" for g in GIDS},
        owner_uids={"110001"},
        state={},
        reports_dir=pathlib.Path("unused"),
        **attrs,
    )
    p._save_state = lambda: None
    p._send_text = lambda *a, **k: asyncio.sleep(0)
    p._spawn = lambda coro: (coro.close(), object())[1]  # 只关心索引，不真起任务
    return p, mod


def generating(p):
    return [e for e in reports.entries(p.state, UMO) if reports.status_of(e) == "generating"]


def _record(gid, label="今天"):
    return reports.build_record(
        report_id=f"chat_intel_{gid}_20261109_010000_{label}",
        gid=gid,
        group=f"群{gid}",
        platform="bot_b",
        label=label,
        since=0,
        until=1,
        topics=[{"title": "话题"}],
        records=[],
        image_name="x.png",
        headline="",
    )


def test_four_group_batch_keeps_every_placeholder():
    p, mod = fresh()
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "最近24小时", False, None)))

    rows = generating(p)
    assert sorted(e["gid"] for e in rows) == sorted(GIDS), "四个群的占位一个都不能少"


def test_placeholders_are_not_squeezed_out_by_finished_reports():
    """五个群都跑完之后，新提交的那份占位仍在索引里。"""
    p, mod = fresh()
    for gid in GIDS:
        reports.resolve(p.state, UMO, _record(gid))
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 2, "最近24小时", False, GIDS[0])))

    rows = reports.entries(p.state, UMO)
    assert len([e for e in rows if e.get("id")]) == reports.INDEX_KEEP, "已生成的报告仍只留最近三份"
    assert len(generating(p)) == 1, "占位不能被截断规则吃掉"


def test_same_group_submitted_twice_keeps_two_placeholders():
    p, mod = fresh(_running=False)
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GIDS[0])))
    p._running = True  # 第二份只能排队
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GIDS[0])))

    assert len(generating(p)) == 2, "两份请求都要在索引里，否则「还有哪些没跑完」会漏报"
    assert len(p._queue) == 1


def test_first_finished_request_leaves_the_queued_one_visible():
    p, mod = fresh(_running=False)
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GIDS[0])))
    p._running = True
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", False, GIDS[0])))
    p._running = False

    reports.resolve(p.state, UMO, _record(GIDS[0]))

    statuses = [reports.status_of(e) for e in reports.entries(p.state, UMO)]
    assert statuses == ["generated", "generating"], f"排队那份不能跟着消失：{statuses}"
    assert len(p._queue) == 1

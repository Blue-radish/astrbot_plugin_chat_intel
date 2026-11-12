"""报告身份必须由模型明确给出，后端不许替它挑。

真实模型测出来的错法：用户说「刚发来的那份第二条」，模型省略了 report_id，
后端按「优先焦点」选到正在讨论的 A，实际该读的是刚投递的 B。
读取会更新焦点，所以猜错不只是答错一次，还把讨论对象换掉了。
"""

import asyncio

import reports
from conftest import make_plugin

GID = "220001"
UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"


class Ev:
    def __init__(self, text=""):
        self.message_str = text
        self.unified_msg_origin = UMO

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return OWNER

    def get_platform_id(self):
        return "bot_a"

    def set_extra(self, key, value):
        pass


def make(tmp_path):
    p, mod = make_plugin(
        reports_dir=tmp_path / "reports",
        group_ids=[GID],
        group_name_map={GID: "極東電子考古學結社"},
        owner_uids={OWNER},
        state={},
        keep_days=7,
    )
    dir_path = tmp_path / "reports"
    dir_path.mkdir(parents=True, exist_ok=True)
    p._save_state = lambda: None
    return p, mod, dir_path


def seed(p, root, rid, *, group="極東電子考古學結社", gid=GID):
    """造一份图文都在的报告，并把它记成「已投递过」。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{rid}.png").write_bytes(b"png")
    record = {
        "id": rid,
        "gid": gid,
        "group": group,
        "label": "今天",
        "generated_at": 1_800_000_000,
        "topics": [{"no": 1, "title": "刷机翻车", "summary": "板子烧了", "refs": []}],
    }
    reports.save(root, record)
    reports.append_delivery(p.state, UMO, report_id=rid, group=group, range_label="今天")
    return record


def test_blank_report_id_is_refused_with_candidates(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "reportB")
    out = asyncio.run(p.tool_get_group_report(Ev(), report_id=""))
    assert "没给 report_id" in out
    assert "reportB" in out, "要把候选列出来，让模型复制 ID 再调"
    assert "刷机翻车" not in out, "没确定身份就不许先读一份"


def test_blank_report_id_does_not_move_the_focus(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "storyA")
    reports.set_focus(p.state, UMO, "storyA", 2)
    asyncio.run(p.tool_get_group_report(Ev(), report_id=""))
    focus = reports.get_focus(p.state, UMO)
    assert focus.get("report_id") == "storyA", "拒绝读取时焦点必须原样不动"
    assert int(focus.get("topic_no") or 0) == 2


def test_explicit_id_wins_over_focus(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "storyA")
    seed(p, root, "freshB")
    reports.set_focus(p.state, UMO, "storyA", 1)
    out = asyncio.run(p.tool_get_group_report(Ev(), report_id="freshB"))
    assert "已读报告 freshB" in out, "要给回执式身份，下一轮不用猜"
    assert reports.get_focus(p.state, UMO)["report_id"] == "freshB"


def test_candidates_say_which_one_is_being_discussed(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "storyA")
    seed(p, root, "freshB")
    reports.set_focus(p.state, UMO, "storyA", 1)
    out = asyncio.run(p.tool_get_group_report(Ev(), report_id=""))
    assert "storyA｜" in out and "正在讨论中" in out
    # 实测模型会把「正在讨论的那份」错认成最新投递，所以候选行要写明哪种说法指哪一份
    assert "「正在讨论」「这条」「刚才聊的那份」指的是它" in out
    assert "「刚发来的」「刚才那张图」指的是它" in out


def test_resend_needs_an_id_too(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "reportB")
    out = asyncio.run(p.tool_resend_report(Ev(), report_id=""))
    assert "没给 report_id" in out and "reportB" in out


def test_resend_ships_the_named_report(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "reportB")
    sent = []

    async def fake_send(umo, text):
        sent.append(text)
        return True

    p._send_text = fake_send
    delivered = []

    async def fake_deliver(umo, path, **_kw):
        delivered.append(path)
        return True

    p._deliver_report = fake_deliver
    out = asyncio.run(p.tool_resend_report(Ev(), report_id="reportB"))
    assert delivered and delivered[0].name == "reportB.png"
    assert "受理结果=sent" in out


def test_resend_reports_send_failure(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "reportB")

    async def failing_deliver(umo, path, **_kw):
        return False

    p._deliver_report = failing_deliver
    out = asyncio.run(p.tool_resend_report(Ev(), report_id="reportB"))
    assert "没能发出去" in out
    assert "已重新发到" not in out, "发失败不许说成成功"


def test_resend_unknown_report_is_explained(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "reportB")
    out = asyncio.run(p.tool_resend_report(Ev(), report_id="nope"))
    assert "找不到报告 nope" in out
    assert "保留 7 天" in out


def test_status_reports_queue_and_running(tmp_path):
    p, mod, _root = make(tmp_path)
    p._status_lines = lambda: asyncio.sleep(0, result=["群 220001：今日 10 条"])
    p._current_req = mod.DigestRequest(UMO, 0, 1, "今天", False, None)
    p._queue.append(mod.DigestRequest(UMO, 0, 1, "昨天", False, None))
    out = asyncio.run(p.tool_digest_status(Ev()))
    assert "今日 10 条" in out
    assert "在跑：今天" in out
    assert "排队：1 份" in out


def test_status_says_nothing_is_running(tmp_path):
    p, _mod, _root = make(tmp_path)
    p._status_lines = lambda: asyncio.sleep(0, result=[])
    out = asyncio.run(p.tool_digest_status(Ev()))
    assert "在跑：无" in out and "排队：0 份" in out


def test_gid_alone_does_not_pick_a_report(tmp_path):
    """同一群两份报告、焦点是 A 时只给群名：不许替模型选一份，更不许切焦点。"""
    p, _mod, root = make(tmp_path)
    seed(p, root, "storyA")
    seed(p, root, "freshB")
    reports.set_focus(p.state, UMO, "storyA", 1)
    out = asyncio.run(p.tool_get_group_report(Ev(), gid="極東"))
    assert "没给 report_id" in out, "只给群名等于把选哪份的责任又交回后端"
    assert "storyA" in out and "freshB" in out
    assert reports.get_focus(p.state, UMO)["report_id"] == "storyA", "拒绝时焦点不许动"


def test_gid_only_works_as_a_filter(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "storyA")
    seed(p, root, "freshB", group="别的群", gid="220002")
    out = asyncio.run(p.tool_get_group_report(Ev(), gid="極東"))
    assert "storyA" in out
    assert "freshB" not in out, "gid 只该过滤候选"


def test_receipt_carries_group_and_range(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "storyA")
    out = asyncio.run(p.tool_get_group_report(Ev(), report_id="storyA"))
    assert "已读报告 storyA｜極東電子考古學結社｜今天" in out


def test_newest_delivery_marker_survives_dedupe(tmp_path):
    """焦点恰好等于最近投递时，旧报告不能被标成「刚发来的」。"""
    p, _mod, root = make(tmp_path)
    seed(p, root, "oldA")          # 先投递 A
    seed(p, root, "freshB")        # 再投递 B
    reports.set_focus(p.state, UMO, "freshB", 1)   # 随后读了 B
    out = asyncio.run(p.tool_get_group_report(Ev(), report_id=""))
    assert "freshB｜極東電子考古學結社｜今天｜正在讨论中·最近投递" in out
    assert "oldA" in out
    old_line = next(line for line in out.splitlines() if line.startswith("- oldA"))
    assert "最近投递" not in old_line, "A 比 B 旧，不能被标成刚发来的"
    assert "刚发来的" not in old_line


def test_group_report_mismatch_is_refused(tmp_path):
    p, _mod, root = make(tmp_path)
    seed(p, root, "storyA", group="極東電子考古學結社")
    out = asyncio.run(p.tool_get_group_report(Ev(), report_id="storyA", gid="别的群"))
    assert "不是你提到的那个群" in out or "没有「别的群」这个群" in out
    assert reports.get_focus(p.state, UMO).get("report_id") != "storyA"


def test_status_counts_scheduled_jobs(tmp_path):
    """定时档在队列里是 None，别把它滤掉说「无任务」。"""
    p, _mod, _root = make(tmp_path)
    p._status_lines = lambda: asyncio.sleep(0, result=[])
    p._running = True
    p._current_req = None
    p._queue.append(None)
    out = asyncio.run(p.tool_digest_status(Ev()))
    assert "在跑：定时任务" in out
    assert "排队：1 份（定时任务）" in out

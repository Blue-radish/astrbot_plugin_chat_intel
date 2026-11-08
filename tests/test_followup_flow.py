"""跨步骤验收：定时收到报告 → 问依据 → 读正文 → 查两天前原话+上下文 → 切旧报告 → 保持焦点。"""

import asyncio
import json
import time
from pathlib import Path

import reports
from conftest import PLUGIN_DIR, make_plugin
from interest import ChatInterest

GID = "220001"
UMO = "bot_a:FriendMessage:110001"


class Ev:
    def __init__(self, text="", gid="", sender="110001", umo=UMO):
        self.message_str = text
        self.unified_msg_origin = umo
        self._gid = gid
        self._sender = sender
        self.stopped = False
        self.extras = {}

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "bot_a"

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class Ctx:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append(umo)
        return self.ok


def make(tmp_path, **over):
    p, mod = make_plugin(
        reports_dir=tmp_path / "reports",
        store_dir=tmp_path / "messages",
        images_dir=tmp_path / "images",
        group_ids=[GID],
        group_name_map={GID: "極東電子考古學結社"},
        owner_uids={"110001"},
        state={},
        **over,
    )
    p._save_state = lambda: None

    async def no_hist(*_a, **_k):
        return []

    async def gname(_gid):
        return "極東電子考古學結社"

    p._fetch_history = no_hist
    p._group_name = gname
    p.context = Ctx()
    return p


def write_rows(tmp_path, rows, day=None):
    folder = tmp_path / "messages"
    folder.mkdir(parents=True, exist_ok=True)
    day = day or time.strftime("%Y-%m-%d")
    with (folder / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def rows_at(base, n, prefix="m", text="说话"):
    return [
        {
            "gid": GID,
            "mid": f"{prefix}{i}",
            "t": base + i * 60,
            "uid": f"u{i}",
            "name": f"人{i}",
            "text": f"{text}{i}",
        }
        for i in range(n)
    ]


def build_report(tmp_path, rid, refs, label, titles, since, until):
    record = reports.build_record(
        report_id=rid,
        gid=GID,
        group="極東電子考古學結社",
        platform="bot_a",
        label=label,
        since=since,
        until=until,
        topics=[
            {"title": t, "tags": [], "summary": f"{t} 的概括", "msg_nos": [], "count": 1} for t in titles
        ],
        records=[],
    )
    for topic, ref in zip(record["topics"], refs):
        topic["refs"] = [ref]
    reports.save(tmp_path / "reports", record)
    (tmp_path / "reports" / f"{rid}.png").write_bytes(b"png")
    return record


# ---------------------------------------------------------------- 路由


def test_followup_question_is_not_hijacked_into_a_new_report(tmp_path):
    p = make(tmp_path)
    went = []

    async def fake_handle(event, intent):
        went.append(intent)

    p._handle_intent = fake_handle
    ev = Ev("日报第二条有什么依据")
    asyncio.run(p.intercept_digest_intent(ev))
    assert ev.stopped is False, "追问不该被 stop_event 掉，要留给模型选读取工具"
    assert went == [], "追问不该进生成分支"


def test_plain_summary_request_still_goes_direct(tmp_path):
    p = make(tmp_path)
    went = []

    async def fake_handle(event, intent):
        went.append(intent)

    p._handle_intent = fake_handle
    ev = Ev("总结一下今天的日报")
    asyncio.run(p.intercept_digest_intent(ev))
    assert ev.stopped is True, "明确要新报告时仍然直接出图，不赌模型"
    assert went and went[0].kind == "digest"


def test_plain_bare_variants_are_still_intercepted(tmp_path):
    p = make(tmp_path)
    for text in ("群聊情报今天群里聊了啥", "日报跑一份", "情报来一份"):
        went = []

        async def fake_handle(event, intent):
            went.append(intent)

        p._handle_intent = fake_handle
        ev = Ev(text)
        asyncio.run(p.intercept_digest_intent(ev))
        assert ev.stopped is True, f"「{text}」是明确要新报告，应该照旧接管"


# ---------------------------------------------------------------- 定时 → 索引


def test_daily_push_registers_into_conversation_index(tmp_path):
    """定时推送成功后，收件会话的索引里要有它，否则「刚才那份」只能靠猜。"""
    p = make(tmp_path)
    p.push_umo = UMO
    record = build_report(
        tmp_path, "dailyA", [], "最近 24 小时", ["定时那份"], time.time() - 86400, time.time()
    )
    reports.resolve(p.state, UMO, record)  # generate_all 会登记成 generated
    seen = {}

    async def fake_all(since, until, label, incremental, only_gid=None, umo=""):
        seen["umo"] = umo
        return [(GID, tmp_path / "reports" / "dailyA.png", "ok", False)]

    p.generate_all = fake_all
    p._push_target_ok = lambda: True  # 收件目标校验另有专门测试
    asyncio.run(p._run_daily_once())
    assert seen["umo"] == UMO, "定时生成必须带上收件会话"
    entries = reports.entries(p.state, UMO)
    assert entries and entries[-1]["id"] == record["id"]
    assert reports.status_of(entries[-1]) == "delivered", "推送成功才算投递"


def test_daily_push_failure_does_not_claim_delivery(tmp_path):
    p = make(tmp_path)
    p.push_umo = UMO
    record = build_report(
        tmp_path, "dailyB", [], "最近 24 小时", ["定时那份"], time.time() - 86400, time.time()
    )
    reports.resolve(p.state, UMO, record)

    async def fake_all(*_a, **_k):
        return [(GID, tmp_path / "reports" / "dailyB.png", "ok", False)]

    p.generate_all = fake_all
    p._push_target_ok = lambda: True
    p.context = Ctx(ok=False)  # 发送接口返回失败
    asyncio.run(p._run_daily_once())
    entry = reports.entries(p.state, UMO)[-1]
    assert reports.status_of(entry) == "generated", "推送失败不许标已投递"
    assert "返回失败" in entry.get("send_error", "")


# ---------------------------------------------------------------- 读正文 → 查原话


def test_full_followup_chain(tmp_path):
    """定时收到 → 问第二条依据 → 读正文 → 查两天前的原话和上下文。"""
    p = make(tmp_path)
    now = int(time.time())
    old_base = now - 2 * 86400
    write_rows(tmp_path, rows_at(old_base, 6, prefix="o", text="两天前的第"), day=time.strftime("%Y-%m-%d", time.localtime(old_base)))
    write_rows(tmp_path, rows_at(now - 3600, 4, prefix="t", text="今天的第"))

    ref = reports.ref_of({"mid": "o3", "t": old_base + 180, "uid": "u3"})
    record = build_report(
        tmp_path, "chainA", [ref], "两天前", ["刷机翻车"], old_base, old_base + 600
    )
    reports.resolve(p.state, UMO, record)
    reports.mark_delivered(p.state, UMO, record["id"])

    # 1) 读正文
    out = asyncio.run(p.tool_get_group_report(Ev(), topic_no=1))
    assert "刷机翻车" in out
    assert "引用核对" in out, "要说清引用现在还能不能核对"
    assert "还能在本地消息库里查到原文" in out
    assert f'report_id="{record["id"]}"' in out, "要把 report_id 一起交出去，别让模型自己算日期"

    # 2) 按报告范围查原话：不传 when 也能查到两天前的
    out = asyncio.run(
        p.tool_query_records(Ev(), report_id=record["id"], msg_ids=ref)
    )
    assert "两天前的第3" in out
    assert f"id={ref}" in out
    assert "命中 1 条" in out

    # 3) 上下文：命中前后要有「·」行
    assert "·" in out, "命中消息要带上下文，否则答不了「后来改口了吗」"
    assert "两天前的第2" in out or "两天前的第4" in out


def test_query_matches_before_truncating(tmp_path):
    """早于最近 max_messages 条的消息，按引用也要能查到。"""
    p = make(tmp_path, max_messages=4)
    now = int(time.time())
    write_rows(tmp_path, rows_at(now - 3600, 10, prefix="m", text="第"))
    ref = reports.ref_of({"mid": "m0", "t": now - 3600, "uid": "u0"})
    out = asyncio.run(p.tool_query_records(Ev(), when="今天", msg_ids=ref))
    assert "第0" in out and f"id={ref}" in out


def test_query_expands_history_when_local_misses(tmp_path):
    """本地没有、平台还留着：引用查询要回平台补拉一次。"""
    p = make(tmp_path)
    now = int(time.time())
    write_rows(tmp_path, rows_at(now - 3600, 3, prefix="t", text="今天的第"))

    async def with_history(gid, since, until):
        return [
            {"gid": GID, "mid": "far1", "t": now - 5000, "uid": "u9", "name": "旧人", "text": "平台还留着"}
        ]

    p._fetch_history = with_history
    out = asyncio.run(p.tool_query_records(Ev(), when="今天", msg_ids="far1"))
    assert "平台还留着" in out, "本地没命中时要回平台补拉，不能直接说没有"


def test_unsupported_time_expression_returns_guidance(tmp_path):
    p = make(tmp_path)
    write_rows(tmp_path, rows_at(int(time.time()) - 3600, 3))
    out = asyncio.run(p.tool_query_records(Ev(), when="上个月", keyword="说"))
    assert "看不懂" in out and "只认" in out
    assert "TypeError" not in out


def test_browse_by_time_without_keyword(tmp_path):
    """只给时间范围也要能用：按时间倒着给最近一页。"""
    p = make(tmp_path)
    now = int(time.time())
    write_rows(tmp_path, rows_at(now - 3600, 25, prefix="m", text="第"))
    out = asyncio.run(p.tool_query_records(Ev(), when="今天"))
    assert "共 25 条" in out
    assert "第24" in out, "浏览模式默认给最近的消息"
    assert "offset=" in out


# ---------------------------------------------------------------- 焦点


def test_focus_survives_next_question(tmp_path):
    p = make(tmp_path)
    now = int(time.time())
    old = build_report(tmp_path, "aaa_old", [], "三天前", ["A 的话题"], now - 3 * 86400, now - 3 * 86400 + 600)
    time.sleep(0.02)
    new = build_report(tmp_path, "zzz_new", [], "今天", ["B 的话题"], now - 3600, now)
    reports.resolve(p.state, UMO, old)
    reports.resolve(p.state, UMO, new)

    first = asyncio.run(p.tool_get_group_report(Ev(), report_id="aaa_old", topic_no=1))
    assert "A 的话题" in first
    again = asyncio.run(p.tool_get_group_report(Ev(), topic_no=1))
    assert "A 的话题" in again, "省略 ID 要接上刚才那份，不能跳回最新"

    blurb = reports.blurb(p.state, UMO)
    assert "正在讨论" in blurb and "aaa_old" in blurb, "提示词里要带上当前焦点"


def test_unknown_group_is_reported_not_silently_swapped(tmp_path):
    p = make(tmp_path)
    build_report(tmp_path, "only", [], "今天", ["话题"], time.time() - 3600, time.time())
    out = asyncio.run(p.tool_get_group_report(Ev(), gid="不存在的群"))
    assert "没有「不存在的群」这个群" in out


# ---------------------------------------------------------------- 存档与投递状态


def test_generate_group_writes_json_and_records_real_range(tmp_path):
    """整条生成链路落盘：JSON 里有引用，range 记的是实际起点。"""
    p = make(tmp_path)
    p.chats = [ChatInterest(GID, "bot_a", ("甲",))]
    p.days = 1.0
    p.include_images = False
    p.max_images = 4
    p.ppi = 72
    p._load_records = lambda gid, since, until, limit=True: [
        {"gid": GID, "mid": "x1", "t": 100, "uid": "u1", "name": "甲", "text": "说点什么", "imgs": []}
    ]
    p._build_transcript = lambda recs, name_map=None: ("转录", len(recs), 1)
    p._split_chunks = lambda transcript: ["第1段"]

    async def fake_extract(*_a, **_k):
        return {"topics": [{"title": "话题", "tags": ["甲"], "msg_nos": [0], "summary": "说了点"}]}

    p._extract_chunk = fake_extract
    rendered = {}

    def fake_render(payload, gid, label):
        rendered.update(payload)
        return PLUGIN_DIR / "logo.png"

    p._render = fake_render
    p._prune_old_files = lambda: None
    p._group_state(GID)["last_ts"] = 50  # 增量游标比请求更早 → real_since=50

    async def no_images(*_a, **_k):
        return {}

    p._images_for_topics = no_images
    path, info, is_error = asyncio.run(p.generate_group(GID, 0, 200, "今天", True, UMO))
    assert path is not None, info
    rid = reports.report_id_from_image(path.name)
    record = reports.load(p.reports_dir, rid)
    assert record is not None, "报告正文必须真的落盘"
    assert record["topics"][0]["refs"] == ["x1"], "引用要能回查"
    assert record["range"]["since"] == 50, "要记实际用的起点（增量游标）"
    assert record["range"]["requested_since"] == 0
    assert record["range"]["incremental"] is True
    entries = reports.entries(p.state, UMO)
    assert entries and reports.status_of(entries[-1]) == "generated"


def test_archive_failure_marks_report_unreadable(tmp_path, monkeypatch):
    p = make(tmp_path)
    monkeypatch.setattr(reports, "save", lambda *_a, **_k: None)
    record = reports.build_record(
        report_id="broken", gid=GID, group="極東電子考古學結社", platform="bot_a",
        label="今天", since=0, until=200,
        topics=[{"title": "话题", "summary": "s", "msg_nos": [], "count": 1}], records=[],
    )
    reports.resolve(p.state, UMO, record, stored=False)
    entry = reports.entries(p.state, UMO)[-1]
    assert reports.status_of(entry) == "unreadable"
    assert entry["stored"] is False
    assert "读不了" in reports.blurb(p.state, UMO)


def test_manual_send_failure_keeps_generated_status(tmp_path):
    p = make(tmp_path)
    p.context = Ctx(ok=False)
    build_report(tmp_path, "sendfail", [], "今天", ["话题"], time.time() - 3600, time.time())
    reports.resolve(
        p.state,
        UMO,
        {"id": "sendfail", "gid": GID, "group": "群", "range": {"label": "今天"},
         "generated_at": int(time.time()), "topics": [], "image": "", "status": "complete"},
    )

    async def fake_all(*_a, **_k):
        return [(GID, tmp_path / "reports" / "sendfail.png", "ok", False)]

    p.generate_all = fake_all
    asyncio.run(p._generate_and_send(UMO, time.time() - 3600, time.time(), "今天", False, None))
    assert [reports.status_of(e) for e in reports.entries(p.state, UMO)] == ["generated"]


def test_expired_evidence_is_stated_in_the_report(tmp_path):
    p = make(tmp_path)
    record = build_report(
        tmp_path, "gone", ["老引用@1"], "十天前", ["话题"], time.time() - 10 * 86400, time.time() - 9 * 86400
    )
    reports.resolve(p.state, UMO, record)
    out = asyncio.run(p.tool_get_group_report(Ev(), topic_no=1))
    assert "一条都查不到" in out
    assert "只能讨论报告里的概括" in out

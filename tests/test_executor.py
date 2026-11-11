"""跨入口 / 跨状态的回归测试：执行器、工具参数一致性、结构校验、合并去重、群内静默。"""

import asyncio
import time
from collections import deque

from conftest import make_plugin
from interest import ChatInterest


class FakeEvent:
    def __init__(self, gid="", text="", sender="10001", umo="p:FriendMessage:10001"):
        self._gid = gid
        self.message_str = text
        self._sender = sender
        self.unified_msg_origin = umo
        self._stopped = False
        self.extras = {}

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


# ---------------------------------------------------------------- 执行器
def test_execute_drains_queue_serially():
    """定时 + 交互排在一起时要串行跑完，不留滞留项，也不并发。"""
    p, mod = make_plugin()
    p._queue = deque(
        [mod.DigestRequest(umo="umo", since=0, until=1, label="今天", incremental=False)],
        maxlen=2,
    )
    seen: list[tuple] = []
    running_flags: list[bool] = []

    async def fake_daily():
        seen.append(("daily",))
        running_flags.append(p._running)

    async def fake_gen(*args, **kwargs):
        seen.append(("digest",) + args)
        running_flags.append(p._running)

    p._run_daily_once = fake_daily
    p._generate_and_send = fake_gen
    assert p._claim() is True
    asyncio.run(p._execute(None))
    assert seen[0][0] == "daily"
    assert seen[1][0] == "digest"
    assert all(running_flags), "执行期间必须处于忙碌状态"
    assert not p._queue
    assert p._running is False


def test_execute_picks_up_request_queued_during_run():
    """跑定时期间塞进队列的交互请求，定时结束后要自动接着跑。"""
    p, mod = make_plugin()
    order: list[str] = []

    async def fake_daily():
        order.append("daily")
        p._enqueue(
            mod.DigestRequest(umo="umo", since=0, until=1, label="今天", incremental=False)
        )

    async def fake_gen(*args, **kwargs):
        order.append("digest")

    p._run_daily_once = fake_daily
    p._generate_and_send = fake_gen
    assert p._claim() is True
    asyncio.run(p._execute(None))
    assert order == ["daily", "digest"]
    assert not p._queue


def test_execute_clears_busy_even_if_task_crashes():
    p, _ = make_plugin()

    async def boom():
        raise RuntimeError("模型挂了")

    p._run_daily_once = boom
    assert p._claim() is True
    asyncio.run(p._execute(None))
    assert p._running is False


# ---------------------------------------------------------------- 工具入口：忙碌/空闲参数一致
def _prep_tool(p, running):
    p.group_ids = ["1", "2"]
    p.chats = [ChatInterest("1", "p", ("甲",)), ChatInterest("2", "p", ("甲",))]
    p.group_name_map = {"1": "MeowShelter", "2": "極東電子考古學結社"}
    p.smart_parse = False
    p._running = running
    p._queue = deque(maxlen=2)
    p._group_name = lambda gid: asyncio.sleep(0, result=p.group_name_map.get(gid, gid))
    created: list[tuple] = []

    async def fake_execute(request):
        created.append(request)

    p._execute = fake_execute
    return created


def test_tool_parameters_are_identical_when_busy_and_idle():
    for running in (False, True):
        p, _ = make_plugin(owner_uids=["10001"])
        created = _prep_tool(p, running)
        sent: list[str] = []
        p._send_text = lambda umo, text: sent.append(text) or asyncio.sleep(0)
        ev = FakeEvent(text="群聊情报把昨天的极东重跑一遍")
        asyncio.run(
            p.tool_group_digest(ev, when="昨天", group="极东", again=True)
        )
        request = created[0] if created else p._queue[0]
        assert request.gid == "2", f"running={running} 时群号解析不一致：{request}"
        assert request.incremental is False, f"running={running} 时 again 被忽略"
        assert request.label == "昨天"
        # 回执由插件自己发：「已排队」只在忙碌时出现，「重跑」标记两种状态都得有
        assert len(sent) == 1, sent
        assert ("已排队" in sent[0]) is running, sent[0]
        assert "重跑" in sent[0], sent[0]


def test_unknown_group_never_queued_when_busy():
    p, _ = make_plugin(owner_uids=["10001"])
    created = _prep_tool(p, True)
    ev = FakeEvent(text="群聊情报把今天的Slack群跑一份")
    out = asyncio.run(p.tool_group_digest(ev, when="今天", group="Slack"))
    assert "没找到「Slack」这个群" in out
    assert not p._queue and not created


# ---------------------------------------------------------------- 结构校验
def test_chunk_structure_validation():
    from conftest import load_main

    mod = load_main()
    ok, _ = mod.TechDigestPlugin._chunk_ok({"topics": []})
    assert ok is True
    ok, why = mod.TechDigestPlugin._chunk_ok({"error": "bad result"})
    assert ok is False and "topics" in why
    ok, _ = mod.TechDigestPlugin._chunk_ok(["不是对象"])
    assert ok is False
    ok, why = mod.TechDigestPlugin._chunk_ok({"topics": "应该是列表"})
    assert ok is False and "列表" in why


def test_extract_chunk_retries_when_model_returns_error_object():
    """模型第一次回 {"error": ...} 时不能算成功，要重试一次拿到真结果。"""
    from types import SimpleNamespace

    p, _ = make_plugin()
    p.config = {}
    answers = iter(['{"error": "bad result"}', '{"topics": []}'])

    class FakeProvider:
        async def text_chat(self, **_kwargs):
            return SimpleNamespace(completion_text=next(answers))

    p._get_provider = lambda: FakeProvider()
    out = asyncio.run(
        p._extract_chunk(ChatInterest("1", "p", ("甲",)), "测试群", "今天", "转录内容")
    )
    assert out == {"topics": []}


def test_extract_chunk_returns_none_when_structure_never_valid():
    from types import SimpleNamespace

    p, _ = make_plugin()
    p.config = {}
    answers = iter(['{"error": "bad"}', '["数组"]'])

    class FakeProvider:
        async def text_chat(self, **_kwargs):
            return SimpleNamespace(completion_text=next(answers))

    p._get_provider = lambda: FakeProvider()
    out = asyncio.run(
        p._extract_chunk(ChatInterest("1", "p", ("甲",)), "测试群", "今天", "转录内容")
    )
    assert out is None


def test_error_object_counts_as_failed_chunk():
    """整段链路：某段拿不到结构化结果时，报告标不完整且不推进游标。"""
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.state = {"groups": {}}
    p.days = 1.0
    p.max_images = 4
    p.include_images = False
    p.ppi = 72
    p.context = None
    p._load_records = lambda gid, since, until: [
        {"t": 100, "name": "甲", "text": "说点什么", "imgs": []}
    ]
    p._group_name = lambda gid: asyncio.sleep(0, result="测试群")
    p._build_transcript = lambda recs, name_map=None: ("转录内容", len(recs), 1)
    p._split_chunks = lambda transcript: ["第1段", "第2段"]
    answers = iter([
        {"error": "bad result"},
        {"topics": [{"title": "话题", "tags": ["甲"], "msg_nos": [1], "summary": "说了点"}]},
    ])
    calls = {"n": 0}

    async def fake_extract(*args, **kwargs):
        calls["n"] += 1
        return next(answers, None)

    p._extract_chunk = fake_extract
    rendered = {}

    def fake_render(payload, gid, label):
        rendered.update(payload)
        from pathlib import Path

        return Path(r"C:\Users\Ray\astrbot\data\plugins\astrbot_plugin_chat_intel\docs\logo.png")

    p._render = fake_render
    p._save_state = lambda: None
    p._prune_old_files = lambda: None
    p._images_for_topics = lambda *a, **k: asyncio.sleep(0, result={})
    path, info, is_error = asyncio.run(p.generate_group("1", 0, 200, "今天", True))
    # 第一段是错误对象：必须算不完整
    assert "不完整" in rendered.get("warn", ""), rendered.get("warn")
    assert not p.state["groups"]["1"].get("last_ts")


# ---------------------------------------------------------------- 合并去重
def test_merge_keeps_images_when_same_message_from_two_sources():
    p, _ = make_plugin()
    local = [{"mid": "m1", "t": 100, "uid": "u1", "text": "", "imgs": ["a.jpg"]}]
    history = [{"mid": "m1", "t": 100, "uid": "u1", "text": "", "imgs": ["b.jpg"]}]
    out = p._dedupe_records(local + history)
    assert len(out) == 1
    assert sorted(out[0]["imgs"]) == ["a.jpg", "b.jpg"]


def test_same_minute_different_messages_are_kept():
    p, _ = make_plugin()
    a = {"mid": "m1", "t": 100, "uid": "u1", "text": "第一句"}
    b = {"mid": "m2", "t": 130, "uid": "u1", "text": "第一句"}
    assert len(p._dedupe_records([a, b])) == 2


def test_old_records_without_mid_still_dedupe():
    p, _ = make_plugin()
    a = {"t": 100, "uid": "u1", "text": "同一句"}
    b = {"t": 100, "uid": "u1", "text": "同一句"}
    assert len(p._dedupe_records([a, b])) == 1


# ---------------------------------------------------------------- 群内一字不发
def test_group_message_gets_no_config_hint():
    p, _ = make_plugin(owner_uids=[])
    p._save_state = lambda: None
    sent: list[tuple] = []

    async def fake_send(umo, text):
        sent.append((umo, text))
        return True

    p._send_text = fake_send
    ev = FakeEvent(gid="220002", text="群聊情报跑一份", umo="p:GroupMessage:220002")
    asyncio.run(p.hint_missing_owner(ev))
    assert sent == [], "群里不许出现任何提示"
    assert not ev.is_stopped()


def test_private_message_still_gets_config_hint():
    p, _ = make_plugin(owner_uids=[])
    p.state = {}
    p._save_state = lambda: None
    sent: list[str] = []

    async def fake_send(umo, text):
        sent.append(text)
        return True

    p._send_text = fake_send
    ev = FakeEvent(gid="", text="群聊情报跑一份")
    asyncio.run(p.hint_missing_owner(ev))
    assert sent and "owner_uids" in sent[0]


def test_hint_time_is_only_recorded_after_a_successful_send():
    """发失败了不能把这一小时占掉，否则用户一小时里都看不到提示。"""
    p, _ = make_plugin(owner_uids=[])
    p.state = {}
    p._save_state = lambda: None

    async def failing_send(umo, text):
        return False

    p._send_text = failing_send
    ev = FakeEvent(gid="", text="群聊情报跑一份")
    asyncio.run(p.hint_missing_owner(ev))
    assert "owner_hint_ts" not in p.state, "没发出去就别记时间"

    async def ok_send(umo, text):
        return True

    p._send_text = ok_send
    asyncio.run(p.hint_missing_owner(ev))
    assert p.state.get("owner_hint_ts"), "发出去了才记时间"

    sent: list[str] = []

    async def counting_send(umo, text):
        sent.append(text)
        return True

    p._send_text = counting_send
    asyncio.run(p.hint_missing_owner(ev))
    assert sent == [], "一小时内不重复提示"


# ---------------------------------------------------------------- info 不为空
def test_info_is_filled_when_report_is_complete():
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.state = {"groups": {}}
    p.days = 1.0
    p.max_images = 4
    p.include_images = False
    p.ppi = 72
    p.context = None
    p._load_records = lambda gid, since, until: [
        {"t": 100, "name": "甲", "text": "说点什么", "imgs": []}
    ]
    p._group_name = lambda gid: asyncio.sleep(0, result="测试群")
    p._build_transcript = lambda recs, name_map=None: ("转录内容", len(recs), 1)
    p._split_chunks = lambda transcript: ["第1段"]

    async def fake_extract(*args, **kwargs):
        return {"topics": [{"title": "话题", "tags": ["甲"], "msg_nos": [1], "summary": "说了点"}]}

    p._extract_chunk = fake_extract

    def fake_render(payload, gid, label):
        from pathlib import Path

        return Path(r"C:\Users\Ray\astrbot\data\plugins\astrbot_plugin_chat_intel\docs\logo.png")

    p._render = fake_render
    p._save_state = lambda: None
    p._prune_old_files = lambda: None
    p._images_for_topics = lambda *a, **k: asyncio.sleep(0, result={})
    path, info, is_error = asyncio.run(p.generate_group("1", 0, 200, "今天", True))
    assert path is not None and is_error is False
    assert "测试群" in info and "话题" in info and "不完整" not in info


# ---------------------------------------------------------------- 推送目标清空语义
def test_clearing_visible_push_fields_disables_push():
    from conftest import load_main

    mod = load_main()
    umo, err = mod.TechDigestPlugin.build_push_target(
        {
            "push_platform": "",
            "push_uid": "",
            "push_umo": "bot_a:FriendMessage:110001",
        }
    )
    assert umo == "" and err == "", "界面上清空 = 明确不要推送，不能偷偷回落到旧值"


def test_legacy_only_config_still_works():
    """老配置靠一次性迁移救回来，不是靠 build_push_target 兜底。"""
    from conftest import load_main

    mod = load_main()
    cfg = {"push_umo": "bot_a:FriendMessage:1"}
    assert mod.TechDigestPlugin.migrate_push_target(cfg, {})[0] == "bot_a:FriendMessage:1"


# ---------------------------------------------------------------- 定时时间规范化
def test_daily_time_is_normalized():
    from conftest import load_main

    mod = load_main()
    assert mod.TechDigestPlugin.normalize_daily_time("9:00") == ("09:00", "")
    assert mod.TechDigestPlugin.normalize_daily_time("23：30") == ("23:30", "")
    assert mod.TechDigestPlugin.normalize_daily_time("") == ("", "")


def test_invalid_daily_time_is_reported():
    from conftest import load_main

    mod = load_main()
    value, err = mod.TechDigestPlugin.normalize_daily_time("明早九点")
    assert value == "" and err
    value, err = mod.TechDigestPlugin.normalize_daily_time("25:99")
    assert value == "" and err

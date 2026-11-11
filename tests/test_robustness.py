"""审查里指出的缺陷的回归测试：权限、空配置、静默、零值、分片、分页、队列、投递校验。"""

import asyncio
import json
import time
from collections import deque

import pytest

from conftest import make_plugin, run_command
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

    def plain_result(self, text):
        from types import SimpleNamespace

        return SimpleNamespace(chain=[SimpleNamespace(text=text)])


# ---------------------------------------------------------------- 权限
def test_owner_empty_denies_everything():
    p, _ = make_plugin(owner_uids=[])
    assert p._tool_allowed(FakeEvent(gid="")) is False


def test_owner_list_allows_only_listed_sender():
    p, _ = make_plugin(owner_uids=["10001"])
    assert p._tool_allowed(FakeEvent(gid="", sender="10001")) is True
    assert p._tool_allowed(FakeEvent(gid="", sender="20002")) is False


def test_group_is_always_denied():
    p, _ = make_plugin(owner_uids=["10001"])
    assert p._tool_allowed(FakeEvent(gid="220002", sender="10001")) is False


def test_no_owner_gets_a_hint_instead_of_a_report():
    """没配 owner 时要明说去配置，而不是默默什么都不做。"""
    p, _ = make_plugin(owner_uids=[])
    p.smart_parse = False
    p.state = {}
    saved = []
    p._save_state = lambda: saved.append(1)
    sent: list[str] = []

    async def fake_send(umo, text):
        sent.append(text)
        return True

    p._send_text = fake_send
    submitted = []

    async def fake_submit(req):
        submitted.append(req)
        return "accepted", "开始生成"

    p._submit_digest = fake_submit
    ev = FakeEvent(text="群聊情报今天的聊了啥")
    asyncio.run(p.hint_missing_owner(ev))
    assert ev.is_stopped()
    assert sent and "owner_uids" in sent[0]
    assert submitted == []  # 提示归提示，绝不顺手生成


# ---------------------------------------------------------------- 空配置
def test_apply_chats_survives_empty_inputs():
    for raw in (None, [], [{"gid": ""}]):
        p, _ = make_plugin()
        p.platform_id = "mybot"
        p.config = {}
        p._apply_chats(raw)
        assert p.chats == [] and p.group_ids == []


def test_legacy_group_ids_still_work():
    p, _ = make_plugin()
    p.config = {"group_ids": "220002, 220001"}
    p._apply_chats(None)
    assert p.group_ids == ["220002", "220001"]


# ---------------------------------------------------------------- 静默不能隐式继承
def test_group_ids_parse_ignores_garbage():
    p, _ = make_plugin()
    p.config = {"group_ids": "abc、123456"}
    assert p._parse_group_ids() == ["123456"]


def test_read_account_alone_does_not_silence():
    """每群的读取账号不会顺带静音任何账号。"""
    from conftest import load_main

    mod = load_main()
    assert mod.TechDigestPlugin._parse_silence({"platform_id": "mybot"}) == []


def test_explicit_silence_is_kept():
    from conftest import load_main

    mod = load_main()
    assert mod.TechDigestPlugin._parse_silence({"silence_platforms": "bot_b, other"}) == [
        "bot_b",
        "other",
    ]


# ---------------------------------------------------------------- 零值配置
def test_zero_max_images_is_honoured():
    p, _ = make_plugin()
    p.config = {"max_images": 0}
    assert p._num_cfg("max_images", 4, lo=0) == 0


def test_missing_and_blank_use_default():
    p, _ = make_plugin()
    p.config = {"max_images": "", "keep_days": None}
    assert p._num_cfg("max_images", 4, lo=0) == 4
    assert p._num_cfg("keep_days", 7, lo=1) == 7


def test_bad_type_falls_back_without_crashing():
    p, _ = make_plugin()
    p.config = {"max_messages": "很多"}
    assert p._num_cfg("max_messages", 10000, lo=50) == 10000


# ---------------------------------------------------------------- 分片失败
def test_failed_chunk_marks_report_incomplete_and_keeps_cursor():
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.state = {"groups": {}}
    p.data_dir = None
    p.days = 1.0
    p.max_images = 4
    p.include_images = False
    p.keep_days = 7
    p.ppi = 72
    p.context = None

    records = [{"t": 100 + i, "name": f"人{i}", "text": f"第{i}条消息", "imgs": []} for i in range(6)]

    p._load_records = lambda gid, since, until: records

    async def fake_name(gid):
        return "测试群"

    p._group_name = fake_name
    p._build_transcript = lambda recs, name_map=None: ("转录内容", len(recs), 2)
    p._split_chunks = lambda transcript: ["第1段", "第2段"]
    calls = {"n": 0}

    async def fake_extract(chat, group_name, window, transcript):
        calls["n"] += 1
        if calls["n"] == 2:
            return None  # 第二段失败
        return {"topics": [{"title": "话题", "tags": ["甲"], "msg_nos": [1], "summary": "讲了点东西"}]}

    p._extract_chunk = fake_extract
    rendered = {}

    def fake_render(payload, gid, label):
        rendered.update(payload)
        from pathlib import Path

        return Path(r"C:\Users\Ray\astrbot\data\plugins\astrbot_plugin_chat_intel\docs\logo.png")

    p._render = fake_render
    p._images_for_topics = lambda *a, **k: asyncio.sleep(0, result={})
    p._save_state = lambda: None
    p._prune_old_files = lambda: None
    p.max_messages = 10000

    path, info, is_error = asyncio.run(p.generate_group("1", 0, 200, "今天", True))
    assert path is not None and is_error is False
    assert "不完整" in rendered["warn"]
    # 增量游标不能推进，否则漏掉的那段永远补不回来
    assert not p.state["groups"]["1"].get("last_ts")
    assert p.state["groups"]["1"].get("last_incomplete")


# ---------------------------------------------------------------- 历史分页
class _FakeBot:
    """按 OneBot 语义翻页：message_seq=N 返回 seq < N 的更早消息（每页最多 5 条）。"""

    def __init__(self, messages, page_size=5):
        self.messages = sorted(messages, key=lambda m: m["message_seq"])
        self.page_size = page_size
        self.calls = 0

    async def call_action(self, action, **params):
        self.calls += 1
        anchor = params.get("message_seq")
        pool = [
            m
            for m in self.messages
            if anchor is None or m["message_seq"] < int(anchor)
        ]
        return {"messages": pool[-self.page_size :][::-1]}


def test_history_paging_continues_when_first_page_is_newer_than_window():
    """第一页全都晚于目标时间时，不能就此结束——目标时段在更早的页里。"""
    now = int(time.time())
    new_msgs = [
        {
            "message_id": 1000 + i,
            "time": now - i,
            "message_seq": 10 + i,
            "sender": {"user_id": 1, "nickname": "甲"},
            "message": [{"type": "text", "data": {"text": f"新{i}"}}],
        }
        for i in range(5)
    ]
    old_msgs = [
        {
            "message_id": 2000 + i,
            "time": now - 3 * 86400 + i,
            "message_seq": 1 + i,
            "sender": {"user_id": 2, "nickname": "乙"},
            "message": [{"type": "text", "data": {"text": f"旧{i}"}}],
        }
        for i in range(5)
    ]
    p, _ = make_plugin()
    p.data_dir = None
    p.max_messages = 100
    p.include_images = False
    bot = _FakeBot(new_msgs + old_msgs)
    p._get_bot = lambda gid=None: bot
    p._group_name = lambda gid: asyncio.sleep(0, result="群")

    since = now - 4 * 86400
    until = now - 2 * 86400
    out = asyncio.run(p._fetch_history("1", since, until))
    assert len(out) == 5  # 第二页那批旧消息拿到了
    assert bot.calls >= 2


# ---------------------------------------------------------------- 队列
def test_queue_is_bounded():
    p, _ = make_plugin()
    p._queue = deque(maxlen=2)
    assert p._enqueue(None) is True
    assert p._enqueue(None) is True
    assert p._enqueue(None) is False
    assert len(p._queue) == 2


def test_full_queue_tells_the_user():
    p, _ = make_plugin(owner_uids={"10001"})
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.smart_parse = False
    p._running = True
    p._queue = deque(maxlen=2)
    p._queue.extend([None, None])
    p._group_name = lambda gid: asyncio.sleep(0, result="群")
    p._save_state = lambda: None
    out = run_command(p, FakeEvent(text="/群总结 今天的聊了啥"), arg="今天的聊了啥")
    assert out and "排着" in out[0]


# ---------------------------------------------------------------- 未知群
def test_unknown_group_does_not_fall_back_to_all_groups():
    p, _ = make_plugin(owner_uids={"10001"})
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.group_name_map = {"1": "MeowShelter"}
    p.smart_parse = False
    p._running = False
    submitted = []

    async def fake_submit(req):
        submitted.append(req)
        return "accepted", "开始生成"

    p._submit_digest = fake_submit
    p._group_name = lambda gid: asyncio.sleep(0, result="MeowShelter")
    out = run_command(p, FakeEvent(text="/群总结 把今天的Slack群跑一份"), arg="把今天的Slack群跑一份")
    assert submitted == []  # 没有退回「全部群」去烧 token
    assert out and "没找到" in out[0]


def test_plain_request_still_targets_all_groups():
    p, _ = make_plugin(owner_uids={"10001"})
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.smart_parse = False
    submitted = []

    async def fake_submit(req):
        submitted.append(req)
        return "accepted", "开始生成"

    p._submit_digest = fake_submit
    p._group_name = lambda gid: asyncio.sleep(0, result="群")
    run_command(p, FakeEvent(text="/群总结 跑一份"), arg="跑一份")
    assert submitted and submitted[0].gid is None  # gid=None = 全部群


# ---------------------------------------------------------------- 投递校验
def test_push_target_rejects_junk_and_groups():
    p, _ = make_plugin(push_enabled=True, push_umo="junk")
    assert p._push_target_ok() is False
    p.push_umo = "mybot:Other:123"
    assert p._push_target_ok() is False
    p.push_umo = "mybot:GroupMessage:123456"
    assert p._push_target_ok() is False
    p.push_umo = "mybot:FriendMessage:abc"
    assert p._push_target_ok() is False
    p.push_umo = "bot_a:FriendMessage:110001"
    assert p._push_target_ok() is True


def test_push_disabled_is_not_ok():
    p, _ = make_plugin(push_enabled=False, push_umo="bot_a:FriendMessage:1")
    assert p._push_target_ok() is False


# ---------------------------------------------------------------- state 原子写
def test_state_save_is_atomic(tmp_path):
    p, _ = make_plugin()
    p.state_path = tmp_path / "state.json"
    p.state = {"groups": {"1": {"last_ts": 5}}}
    p._save_state()
    assert json.loads(p.state_path.read_text(encoding="utf-8"))["groups"]["1"]["last_ts"] == 5
    assert not (tmp_path / "state.json.tmp").exists()


# ---------------------------------------------------------------- 去重
def test_dedupe_merges_images_instead_of_dropping():
    p, _ = make_plugin()
    a = {"uid": "1", "t": 100, "text": "", "imgs": ["a.jpg"]}
    b = {"uid": "1", "t": 100, "text": "", "imgs": ["b.jpg"]}
    out = p._dedupe_records([a, b])
    assert len(out) == 1
    assert out[0]["imgs"] == ["a.jpg", "b.jpg"]


def test_all_chunks_failing_reports_error_and_keeps_cursor():
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.state = {"groups": {}}
    p.days = 1.0
    p.max_images = 4
    p.include_images = False
    p.ppi = 72
    p.context = None
    records = [{"t": 100, "name": "甲", "text": "说点什么", "imgs": []}]
    p._load_records = lambda gid, since, until: records
    p._group_name = lambda gid: asyncio.sleep(0, result="测试群")
    p._build_transcript = lambda recs, name_map=None: ("转录内容", len(recs), 1)
    p._split_chunks = lambda transcript: ["第1段"]

    async def always_none(*args, **kwargs):
        return None

    p._extract_chunk = always_none
    p._save_state = lambda: None
    path, info, is_error = asyncio.run(p.generate_group("1", 0, 200, "今天", True))
    assert path is None and is_error is True
    assert "没有给出可用" in info
    assert not p.state["groups"]["1"].get("last_ts")


# ---------------------------------------------------------------- 推送目标：两个框填出来
def test_push_target_built_from_two_simple_fields():
    from conftest import load_main

    mod = load_main()
    umo, err = mod.TechDigestPlugin.build_push_target(
        {"push_platform": "bot_a", "push_uid": "110001"}
    )
    assert umo == "bot_a:FriendMessage:110001"
    assert err == ""


def test_push_target_accepts_legacy_full_string():
    """老串不再由 build_push_target 兜底，而是迁移到两个可见字段（宿主会补默认值）。"""
    from conftest import load_main

    mod = load_main()
    cfg = {"push_umo": "bot_a:FriendMessage:110001"}
    assert mod.TechDigestPlugin.build_push_target(cfg)[0] == ""
    assert mod.TechDigestPlugin.migrate_push_target(cfg, {})[0] == "bot_a:FriendMessage:110001"
    assert mod.TechDigestPlugin.build_push_target(cfg)[0] == "bot_a:FriendMessage:110001"


def test_push_target_explains_what_is_missing():
    from conftest import load_main

    mod = load_main()
    umo, err = mod.TechDigestPlugin.build_push_target({"push_uid": "110001"})
    assert umo == "" and "用哪个机器人发" in err
    umo, err = mod.TechDigestPlugin.build_push_target({"push_platform": "bot_a"})
    assert umo == "" and "收件人" in err
    umo, err = mod.TechDigestPlugin.build_push_target(
        {"push_platform": "bot_a", "push_uid": "改天"}
    )
    assert umo == "" and "不是纯数字" in err


def test_push_target_only_takes_private_chat():
    p, _ = make_plugin()
    p.push_enabled = True
    p.push_umo = "bot_a:FriendMessage:110001"
    assert p._parse_umo(p.push_umo) == ("bot_a", "FriendMessage", "110001")


# ---------------------------------------------------------------- 缺读取账号就跳过
def test_group_without_read_account_is_skipped_with_hint():
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "", ("甲",))]
    p.group_ids = ["1"]
    p.state = {}
    p._save_state = lambda: None
    p._group_name = lambda gid: asyncio.sleep(0, result="空账号群")
    pushed: list[str] = []

    async def fake_push(text):
        pushed.append(text)
        return True

    p._push_text = fake_push

    async def fail_render(*a, **k):
        raise AssertionError("不该走到排版")

    p._render = fail_render
    path, info, is_error = asyncio.run(p.generate_group("1", 0, 1, "今天", False))
    assert path is None and is_error is False
    assert "没填「读取账号」" in info
    assert pushed and "读取账号" in pushed[0]
    # 同一个群只提醒一次
    asyncio.run(p.generate_group("1", 0, 1, "今天", False))
    assert len(pushed) == 1

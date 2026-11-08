"""评审提出的问题逐条落成测试（都是会真崩或真抢话的点）。"""

import asyncio
import pathlib
import re
from types import SimpleNamespace

from conftest import PLUGIN_DIR, make_plugin
import reports
from intent import detect_intent
from interest import ChatInterest, normalize_topics


# ---------------------------------------------------------------- 意图隔离


def test_plugin_word_alone_is_not_an_anchor():
    """「插件」不再算指向词：B站推送/TTS 也是插件，不能因为说了这个词就抢话。"""
    for text in ("B站推送插件什么情况", "TTS插件有什么毛病", "插件更新了吗"):
        assert detect_intent(text) is None, text


def test_generic_action_needs_strong_anchor():
    """「有什么」这种泛动作词只认强指向；弱指向（只说"群"）会被误判成要报告。"""
    assert detect_intent("群里今天有什么好玩的") is None
    got = detect_intent("群聊情报今天有什么")
    assert got is not None and got.kind == "digest"


def test_weak_digest_still_works_with_real_cues():
    for text in ("群里今天聊了啥", "群聊总结今天极东群聊了啥"):
        got = detect_intent(text)
        assert got is not None and got.kind == "digest", text


# ---------------------------------------------------------------- 每群账号


class FakeBot:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return {"messages": self.messages}


def _msg(mid, t, text):
    return {
        "message_id": mid,
        "time": t,
        "user_id": "u1",
        "sender": {"nickname": "甲"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_fetch_history_uses_chat_platform_and_respects_until():
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "bot_b", ("甲",))]
    p.group_ids = ["1"]
    p.max_messages = 100
    p.include_images = False
    fake = FakeBot([_msg("a", 100, "早"), _msg("b", 200, "中"), _msg("c", 300, "晚")])
    seen: dict = {}

    def picker(gid=None):
        seen["gid"] = gid
        return fake

    p._get_bot = picker
    got = asyncio.run(p._fetch_history("1", 150, 250))
    assert seen["gid"] == "1", "拉历史没有把群号传给 _get_bot，每群 platform 形同虚设"
    assert [r["text"] for r in got] == ["中"], "只有下界没有上界，会把后面的消息也拉进来"


# ---------------------------------------------------------------- 判图缓存


def test_prune_keeps_cache_for_surviving_images(tmp_path):
    p, _ = make_plugin(
        store_dir=tmp_path / "messages",
        images_dir=tmp_path / "images",
        reports_dir=tmp_path / "reports",
    )
    p.keep_days = 7
    today = pathlib.Path("2026-11-07")
    (tmp_path / "images" / today).mkdir(parents=True)
    (tmp_path / "images" / today / "a.jpg").write_bytes(b"x")
    p.state = {
        "images": {"fp1:a.jpg": {"keep": True}, "fp2:gone.jpg": {"keep": True}},
    }
    p._prune_old_files()
    assert set(p.state["images"]) == {"fp1:a.jpg"}, p.state["images"]


# ---------------------------------------------------------------- 落盘时间


class FakeEvent:
    def __init__(self, platform="bot_b", gid="1", text="显卡降价", uid="u1", raw_time=None):
        self._platform = platform
        self._gid = gid
        self._uid = uid
        self.message_str = text
        self.message_obj = SimpleNamespace(raw_message={"message": [], "time": raw_time})
        self.unified_msg_origin = "bot_a:FriendMessage:110001"
        self.stopped = False
        self.extras = {}

    def get_platform_id(self):
        return self._platform

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return "甲"

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def stop_event(self):
        self.stopped = True


def test_record_uses_onebot_event_time(tmp_path):
    """时间戳要用消息自己的时间：两个 bot 跨秒收到同一条消息时才能去重。"""
    p, _ = make_plugin(store_dir=tmp_path)
    p.group_ids = ["1"]
    p.silence_platforms = ["bot_b"]
    p._silenced = 0
    p._diag_count = 0
    ev = FakeEvent(raw_time=1794000000)
    asyncio.run(p.silence_events(ev))
    rows = [
        __import__("json").loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["t"] == 1794000000


# ---------------------------------------------------------------- 交互反馈


class FakeCtx:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append(umo)
        return True


def test_interactive_skip_tells_user_something():
    """人主动问的时候，即使没内容也得回一句，不能什么都不发。"""
    p, _ = make_plugin()
    p.context = FakeCtx()

    async def gen_all(*_a, **_k):
        return [("1", None, "某群：今天没有消息", False)]

    p.generate_all = gen_all
    asyncio.run(p._generate_and_send("umo", 0, 1, "今天", False, None, announce=True))
    assert len(p.context.sent) == 1


def test_scheduled_skip_stays_silent():
    p, _ = make_plugin()
    p.context = FakeCtx()

    async def gen_all(*_a, **_k):
        return [("1", None, "某群：今天没有消息", False)]

    p.generate_all = gen_all
    asyncio.run(p._generate_and_send("umo", 0, 1, "今天", False, None))
    assert p.context.sent == []


def test_push_refuses_group_target():
    """「永不发回群里」不能只靠配置自觉：目标会话是群就直接拒发。"""
    p, _ = make_plugin()
    p.push_enabled = True
    p.push_umo = "bot_b:GroupMessage:123"
    p.context = FakeCtx()
    assert asyncio.run(p._push_text("hi")) is False
    assert p.context.sent == []


# ---------------------------------------------------------------- 分节容错


def test_topics_tolerate_numbered_or_punctuated_tags():
    chat = ChatInterest("1", "p", ("复古硬件", "元件行情"), (), "", "")
    topics = [
        {"title": "电容代换", "tags": ["1. 复古硬件"], "msg_nos": [1], "summary": "a"},
        {"title": "行情", "tags": ["元件行情。"], "msg_nos": [2], "summary": "b"},
        {"title": "跑偏", "tags": ["复古硬件（老机器）"], "msg_nos": [3], "summary": "c"},
    ]
    out = normalize_topics(chat, topics)
    assert [t["tags"] for t in out] == [["复古硬件"], ["元件行情"]], out


# ---------------------------------------------------------------- 版本一致


def test_register_version_matches_metadata():
    meta = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    meta_version = re.search(r"^version:\s*(\S+)", meta, re.M).group(1)
    source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    registered = re.search(r'@register\(\s*PLUGIN_NAME,\s*"[^"]*",\s*"[^"]*",\s*"([^"]+)"', source)
    assert registered, "没找到 @register 的版本号"
    assert registered.group(1) == meta_version, (registered.group(1), meta_version)


# ---------------------------------------------------------------- 提示词与闸门


def test_digest_prompt_asks_for_headline():
    """模板里有头条区块，schema 不要求 headline 就永远渲染不出来。"""
    from interest import build_digest_prompt

    prompt = build_digest_prompt(ChatInterest("1", "p", ("甲",)), "群", "今天", "[00:00] #0 甲: hi")
    assert '"headline"' in prompt


def test_silence_llm_stops_the_event():
    """第二道闸返回 True 在 AstrBot 里不生效，必须真的停事件。"""
    p, _ = make_plugin()
    p.silence_platforms = ["bot_b"]
    p._llm_blocked = 0
    ev = FakeEvent(platform="bot_b")
    req = SimpleNamespace(system_prompt="")
    assert asyncio.run(p.silence_llm(ev, req)) is True
    assert ev.stopped is True


# ---------------------------------------------------------------- 工具兜底不能太急


def test_tool_refuses_chitchat_that_is_not_a_digest_request():
    """实测：『群里今天有什么好玩的』被模型当成要报告，直接跑了全部群。"""
    p, _ = make_plugin(owner_uids=["u1"])
    p.group_ids = ["1"]
    p.chats = [ChatInterest("1", "p", ("甲",))]
    started = []

    async def gen_all(*_a, **_k):
        started.append(1)
        return []

    p.generate_all = gen_all
    ev = FakeEvent(gid="", text="群里今天有什么好玩的")
    out = asyncio.run(p.tool_group_digest(ev, when="今天"))
    assert "不要调用" in out
    assert started == []


def test_tool_still_works_for_real_digest_request():
    p, _ = make_plugin(owner_uids=["u1"])
    p.group_ids = ["1"]
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p._running = False
    p.smart_parse = False
    p._get_bot = lambda gid=None: None
    p._group_name = _noop_name
    ev = FakeEvent(gid="", text="群聊情报今天的聊了啥")
    out = asyncio.run(p.tool_group_digest(ev, when="今天"))
    assert "已在后台开始生成" in out


async def _noop_name(gid):
    return "测试群"


# ---------------------------------------------------------------- 忙的时候要排队


def test_busy_request_is_queued_instead_of_dropped():
    """实测：用户要紧的请求被"上一份还在生成"直接丢掉，白问一次。"""
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.smart_parse = False
    p._running = True
    p._group_name = _noop_name
    sent: list[str] = []

    async def fake_send(umo, text):
        sent.append(text)

    p._send_text = fake_send
    ev = FakeEvent(gid="", text="群聊情报今天的聊了啥")
    asyncio.run(p._handle_intent(ev, detect_intent("群聊情报今天的聊了啥")))
    assert p._queue and len(p._queue) == 1, list(p._queue)
    assert p._queue[0].umo == ev.unified_msg_origin
    assert sent and "排队" in sent[0]


def test_natural_language_entry_registers_the_placeholder():
    """直通入口以前不登记「正在生成」：用户回头问「跑完了吗」根本查不到这条。"""
    p, _ = make_plugin()
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.group_name_map = {"1": "测试群"}
    p.smart_parse = False
    p._save_state = lambda: None
    p._group_name = _noop_name
    sent: list[str] = []

    async def fake_send(umo, text):
        sent.append(text)

    async def fake_execute(request):
        return None

    p._send_text = fake_send
    p._execute = fake_execute
    ev = FakeEvent(gid="", text="群聊情报今天的聊了啥")
    asyncio.run(p._handle_intent(ev, detect_intent("群聊情报今天的聊了啥")))

    entries = reports.entries(p.state, ev.unified_msg_origin)
    assert entries and reports.status_of(entries[-1]) == "generating", "两个入口都要登记占位"
    assert sent and "开始生成" in sent[0]


def test_executor_takes_the_queued_request():
    """排队的那份由执行器接走（原来测的 _drain_pending 早已没人调用，改成测真入口）。"""
    p, mod = make_plugin()
    p._queue.append(
        mod.DigestRequest(umo="umo", since=0, until=1, label="今天", incremental=False)
    )
    calls = []

    async def fake_daily():
        calls.append("daily")

    async def fake_gen(*args):
        calls.append(args)

    p._run_daily_once = fake_daily
    p._generate_and_send = fake_gen
    p._running = True  # 执行位已经占上，_execute 只负责吃队列
    asyncio.run(p._execute(None))
    assert not p._queue
    assert calls == ["daily", ("umo", 0, 1, "今天", False, None)]



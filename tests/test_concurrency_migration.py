"""并发提交、宿主配置迁移、合并排序、空话题游标的回归测试。"""

import asyncio
import json
import time
from collections import deque
from pathlib import Path

from conftest import PLUGIN_DIR, ASTRBOT_ROOT, load_main, make_plugin
from interest import ChatInterest

import sys

if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))


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


def _prep_tool(p):
    p.group_ids = ["1", "2"]
    p.chats = [ChatInterest("1", "p", ("甲",)), ChatInterest("2", "p", ("甲",))]
    p.group_name_map = {"1": "MeowShelter", "2": "極東電子考古學結社"}
    p.smart_parse = False
    p._running = False
    p._queue = deque(maxlen=2)
    p._group_name = lambda gid: asyncio.sleep(0, result=p.group_name_map.get(gid, gid))


# ---------------------------------------------------------------- 1. 并发窗口
def test_two_background_submissions_run_serially():
    p, _ = make_plugin(owner_uids=["10001"])
    _prep_tool(p)
    active = {"now": 0, "peak": 0}
    order: list[str] = []

    async def slow_gen(*args, **kwargs):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        order.append(args[0])
        await asyncio.sleep(0.05)
        active["now"] -= 1

    p._generate_and_send = slow_gen
    sent: list[str] = []
    p._send_text = lambda umo, text: sent.append(text) or asyncio.sleep(0)

    async def scenario():
        await p.tool_group_digest(FakeEvent(text="群聊情报跑一份"), when="今天")
        await p.tool_group_digest(FakeEvent(text="群聊情报跑一份"), when="今天")
        await asyncio.sleep(0.4)

    asyncio.run(scenario())
    assert active["peak"] == 1, f"同时跑了两份生成：{active}"
    assert len(order) == 2, f"排队的第二份没跑：{order}"
    # 回执由插件自己发：第一条「开始生成」、第二条「已排队」
    assert len(sent) == 2, sent
    assert sent[0].startswith("开始生成") and sent[1].startswith("已排队"), sent


def test_claim_is_exclusive():
    p, _ = make_plugin()
    assert p._claim() is True
    assert p._claim() is False, "已经占上了就不能再占"
    p._running = False
    assert p._claim() is True


def test_claim_released_when_task_crashes():
    p, mod = make_plugin()
    _prep_tool(p)

    async def boom(*args, **kwargs):
        raise RuntimeError("模型挂了")

    p._generate_and_send = boom
    assert p._claim() is True
    request = mod.DigestRequest(
        umo="umo", since=0, until=1, label="今天", incremental=False
    )
    asyncio.run(p._execute(request))
    assert p._running is False
    assert p._claim() is True, "崩过一次之后执行位必须能再抢"


def test_long_job_is_not_forcibly_released():
    """以前有「占位超过 1 小时就强放」的兜底：大群跑得久会被误判成死在半路，
    于是同一份被跑第二遍。现在执行位由任务自己在 finally 里放，不再按时长猜。"""
    p, _ = make_plugin()
    assert not hasattr(p, "_release_stale_claim"), "兜底已经删掉"
    assert p._claim() is True
    time.sleep(0.01)
    assert p._claim() is False, "跑得久也不许被别人抢"


# ---------------------------------------------------------------- 2. 宿主补默认值后的迁移
def _schema_defaults() -> dict:
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    out = {}
    for key, item in schema.items():
        if key.startswith("__"):
            continue
        out[key] = item.get("default", "")
    return out


def test_astrbot_loader_fills_new_keys_and_would_kill_legacy_target(tmp_path):
    """宿主加载老配置时会补上新字段的默认值，所以「字段存在」判断不了用户是否清空。"""
    from astrbot.core.config.astrbot_config import AstrBotConfig

    legacy = {"push_enabled": True, "push_umo": "bot_a:FriendMessage:110001"}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    cfg = AstrBotConfig(config_path=str(path), default_config=_schema_defaults())
    assert cfg.get("push_platform") == "" and cfg.get("push_uid") == ""

    mod = load_main()
    # 旧判断口径下这里会得到空串（= 自动投递静默失效），这就是要改的地方
    assert mod.TechDigestPlugin.build_push_target(cfg)[0] == ""

    state = {}
    umo, note = mod.TechDigestPlugin.migrate_push_target(cfg, state)
    assert umo == "bot_a:FriendMessage:110001" and note == ""
    assert cfg.get("push_platform") == "bot_a"
    assert cfg.get("push_uid") == "110001"
    # 标记不再由 migrate_push_target 写：得等配置真的落盘（见 test_migration_safety）


def test_after_migration_user_clearing_really_clears():
    mod = load_main()
    cfg = {"push_platform": "bot_a", "push_uid": "110001"}
    state = {}
    assert mod.TechDigestPlugin.migrate_push_target(cfg, state)[0] == "bot_a:FriendMessage:110001"
    cfg["push_platform"] = ""
    cfg["push_uid"] = ""
    assert mod.TechDigestPlugin.migrate_push_target(cfg, state)[0] == ""
    assert mod.TechDigestPlugin.build_push_target(cfg)[0] == ""


def test_migration_runs_only_once():
    mod = load_main()
    cfg = {"push_umo": "bot_a:FriendMessage:110001"}
    state = {"config_migrated_v224": True}
    assert mod.TechDigestPlugin.migrate_push_target(cfg, state)[0] == ""


# ---------------------------------------------------------------- 3. 合并排序
def test_merge_sorts_by_time_and_keeps_newest():
    p, _ = make_plugin()
    p.max_messages = 50
    local = [{"mid": "new", "t": 200, "uid": "u1", "text": "最新的本地消息"}]
    history = [{"mid": f"h{i}", "t": i, "uid": "u1", "text": f"历史{i}"} for i in range(50, 0, -1)]
    merged = p._merge_records(local, history)
    assert len(merged) == 50
    assert merged[-1]["t"] == 200, "最新的本地消息不能被截掉"
    assert [r["t"] for r in merged] == sorted(r["t"] for r in merged)


def test_merge_orders_interleaved_and_dedupes():
    p, _ = make_plugin()
    p.max_messages = 100
    local = [{"mid": "a", "t": 10}, {"mid": "c", "t": 30}]
    history = [{"mid": "b", "t": 20}, {"mid": "a", "t": 10, "imgs": ["x.jpg"]}]
    merged = p._merge_records(local, history)
    assert [r["t"] for r in merged] == [10, 20, 30]
    assert merged[0].get("imgs") == ["x.jpg"]


# ---------------------------------------------------------------- 4. 旧全局账号迁移
def test_astrbot_loader_keeps_deprecated_platform_id_for_migration(tmp_path):
    """宿主会把 schema 里没有的键丢掉，所以废弃的 platform_id 必须还在 schema 里（隐藏）。"""
    from astrbot.core.config.astrbot_config import AstrBotConfig

    legacy = {
        "platform_id": "bot_b",
        "chats": [
            {"gid": "1", "platform": "", "tags_pos": ["计算机"]},
            {"gid": "2", "platform": "bot_a", "tags_pos": ["计算机"]},
        ],
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    cfg = AstrBotConfig(config_path=str(path), default_config=_schema_defaults())
    assert cfg.get("platform_id") == "bot_b", "旧全局账号得能读到，否则升级时补不了群账号"

    mod = load_main()
    filled = mod.TechDigestPlugin.migrate_chat_platforms(cfg.get("chats"), cfg.get("platform_id"))
    assert filled == ["1"]
    assert [c["platform"] for c in cfg.get("chats")] == ["bot_b", "bot_a"]


def test_legacy_platform_id_fills_empty_rows():
    mod = load_main()
    rows = [
        {"gid": "1", "platform": ""},
        {"gid": "2", "platform": "bot_a"},
        {"gid": "", "platform": ""},
    ]
    filled = mod.TechDigestPlugin.migrate_chat_platforms(rows, "bot_b")
    assert filled == ["1"], "只补没填的行，已有单群设置保持优先"
    assert rows[0]["platform"] == "bot_b"
    assert rows[1]["platform"] == "bot_a"
    assert rows[2]["platform"] == ""


# ---------------------------------------------------------------- 5. 空话题也推进游标
def _stub_generate(p, chunk_result):
    p.chats = [ChatInterest("1", "p", ("甲",))]
    p.group_ids = ["1"]
    p.state = {"groups": {}}
    p.days = 1.0
    p.max_images = 4
    p.max_messages = 10000
    p.include_images = False
    p.ppi = 72
    p.context = None
    p._running = False
    p._load_records = lambda gid, since, until: [
        {"t": 100, "name": "甲", "text": "说点什么", "imgs": []}
    ]
    p._fetch_history = lambda *a, **k: asyncio.sleep(0, result=[])
    p._group_name = lambda gid: asyncio.sleep(0, result="测试群")
    p._images_for_topics = lambda *a, **k: asyncio.sleep(0, result={})
    p._save_state = lambda: None
    p._prune_old_files = lambda: None

    async def fake_extract(*args, **kwargs):
        return chunk_result

    p._extract_chunk = fake_extract


def test_empty_topics_advance_incremental_cursor():
    p, _ = make_plugin()
    _stub_generate(p, {"topics": []})
    path, info, is_error = asyncio.run(p.generate_group("1", 0, 200, "今天", True))
    assert path is None and is_error is False
    assert p.state["groups"]["1"].get("last_ts") == 200, "完整处理过的范围必须记游标"
    assert not p.state["groups"]["1"].get("last_report_ts"), "没出报告就不该记生成时间"


def test_empty_topics_with_failed_chunk_keeps_cursor():
    p, _ = make_plugin()
    p.max_messages = 10000
    _stub_generate(p, None)  # 直接判这一段失败
    path, info, is_error = asyncio.run(p.generate_group("1", 0, 200, "今天", True))
    assert not p.state.get("groups", {}).get("1", {}).get("last_ts")


# ---------------------------------------------------------------- 6. 状态与文案
def test_status_shows_last_report_time_not_cursor():
    p, _ = make_plugin()
    ts = int(time.mktime((2026, 11, 8, 0, 19, 0, 0, 0, -1)))
    p.state = {
        "groups": {
            "1": {
                "last_ts": int(ts) - 3600,
                "last_report_ts": ts,
                "last_range": "今天",
                "last_incomplete": False,
            }
        }
    }
    desc = p._last_report_desc("1")
    assert "上次生成 11-08 00:19" in desc
    assert "今天" in desc
    assert "不完整" not in desc


def test_status_marks_incomplete_report():
    p, _ = make_plugin()
    p.state = {"groups": {"1": {"last_report_ts": int(time.time()), "last_incomplete": True}}}
    assert "不完整" in p._last_report_desc("1")
    assert "还没生成过" in p._last_report_desc("2")


def test_daily_time_hint_matches_behavior():
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    item = schema["daily_time"]
    hint = item.get("hint", "") + item.get("description", "")
    assert "09:00" in hint
    assert "不识别" not in hint and "认不出" not in hint

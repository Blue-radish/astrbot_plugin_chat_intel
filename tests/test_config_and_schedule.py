"""配置简化与定时语义的回归测试。"""

import asyncio
import json
import time

from conftest import PLUGIN_DIR, make_plugin


# ---------------------------------------------------------------- 定时语义


def test_daily_window_is_last_24_hours():
    now = 1794000000.0
    since, until, label = make_plugin()[1].TechDigestPlugin._daily_window(now)
    assert until == now
    assert until - since == 24 * 3600
    assert label == "最近 24 小时"


def test_run_daily_once_covers_24h_and_is_not_incremental():
    p, _ = make_plugin()
    seen = []

    async def fake_generate_all(since, until, label, incremental, only_gid=None, umo=""):
        seen.append((since, until, label, incremental, only_gid, umo))
        return [("1", None, "某群：没有消息", False)]

    delivered = []

    async def fake_deliver(umo, path):
        delivered.append((umo, path))
        return True

    p.generate_all = fake_generate_all
    p._deliver_report = fake_deliver
    before = time.time()
    asyncio.run(p._run_daily_once())
    since, until, label, incremental, only_gid, umo = seen[0]
    assert incremental is False, "定时档不能是增量，否则漏跑一次就永远缺一块"
    assert only_gid is None
    assert abs((until - since) - 86400) < 1
    assert since >= before - 86400 - 2
    assert delivered == [], "没有报告时不许发任何东西"


def test_run_daily_once_pushes_report_image():
    p, _ = make_plugin()
    delivered = []

    async def fake_generate_all(*_a, **_k):
        return [("1", __import__("pathlib").Path("x.png"), "ok", False)]

    async def fake_deliver(umo, path):
        delivered.append(path)
        return True

    p.generate_all = fake_generate_all
    p._deliver_report = fake_deliver
    p._push_target_ok = lambda: True
    asyncio.run(p._run_daily_once())
    assert len(delivered) == 1


def test_run_daily_once_reports_errors_to_owner():
    p, _ = make_plugin()
    texts = []

    async def fake_generate_all(*_a, **_k):
        return [("1", None, "群 1 生成异常：boom", True)]

    async def fake_push_text(text):
        texts.append(text)

    p.generate_all = fake_generate_all
    p._push_text = fake_push_text
    asyncio.run(p._run_daily_once())
    assert texts and "boom" in texts[0]


# ---------------------------------------------------------------- 模型不用手填


class FakeProvider:
    def __init__(self, pid, modalities=None):
        self.provider_config = {"id": pid}
        if modalities is not None:
            self.provider_config["modalities"] = modalities


class FakeManager:
    def __init__(self, insts):
        self._insts = insts

    def get_insts(self):
        return {str(i): p for i, p in enumerate(self._insts)}


class FakeCtx2:
    def __init__(self, using, insts):
        self._using = using
        self.provider_manager = FakeManager(insts)

    def get_using_provider(self):
        return self._using


def test_get_provider_uses_default_not_config():
    p, _ = make_plugin()
    default = FakeProvider("wb2", ["text", "image"])
    p.context = FakeCtx2(default, [default])
    p.config = {"provider_id": "不存在的"}  # 旧配置残留也不该再被读
    assert p._get_provider() is default


def test_vision_provider_falls_back_to_image_capable_model():
    p, _ = make_plugin()
    text_only = FakeProvider("text", ["text"])
    vision = FakeProvider("vl", ["text", "image"])
    p.context = FakeCtx2(text_only, [text_only, vision])
    assert p._get_vision_provider() is vision


def test_vision_provider_none_when_nobody_reads_images():
    p, _ = make_plugin()
    text_only = FakeProvider("text", ["text"])
    p.context = FakeCtx2(text_only, [text_only])
    assert p._get_vision_provider() is None


def test_vision_capable_treats_unknown_modalities_as_ok():
    _p, mod = make_plugin()
    assert mod.TechDigestPlugin._vision_capable(FakeProvider("x")) is True
    assert mod.TechDigestPlugin._vision_capable(FakeProvider("x", ["text"])) is False


# ---------------------------------------------------------------- 字体目录


def test_font_dir_is_bundled_not_configurable():
    """字体代码只认插件数据目录下的 fonts/，不再读配置。"""
    source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    assert 'config.get("font_dir")' not in source
    assert 'self.font_dir = Path(' in source
    assert '(self.data_dir / "fonts")' in source


# ---------------------------------------------------------------- 配置分层
def test_fresh_install_does_not_generate_at_night_by_default():
    """默认不该自己半夜跑一份：定时时间留空，想要就自己填。"""
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    assert schema["daily_time"]["default"] == "", "默认还是 23:00，等于悄悄替你定了作息"
    assert "留空" in schema["daily_time"]["description"]


def test_schema_puts_everyday_items_before_tuning_knobs():
    """页面按「日常 → 进阶」排：常用项在前，调节项在后。

    一进设置页先看到 max_messages / render_ppi 这种没人动的旋钮，才是配置负担的来源。
    """
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    order = [key for key, item in schema.items() if item.get("invisible") is not True]
    assert order[0] == "chats", f"第一个该是要总结的群：{order}"
    everyday = {"include_images", "keep_days", "owner_uids", "daily_time"}
    assert everyday <= set(order[:10]), f"日常项被挤到后面了：{order}"
    for knob in ("max_messages", "max_image_mb", "render_ppi"):
        assert order.index(knob) >= 10, f"{knob} 太靠前：{order}"


# ---------------------------------------------------------------- 配置表里不该有的字段


def test_schema_drops_fields_users_cannot_fill():
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    for gone in ("provider_id", "vision_provider_id", "font_dir"):
        assert gone not in schema, f"{gone} 还留在配置表里"
    assert "24 小时" in schema["daily_time"]["description"]


def test_chats_template_declares_one_template():
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    chat = schema["chats"]
    assert chat["type"] == "template_list"
    assert "chat" in chat["templates"]
    assert {"gid", "tags_pos"} <= set(chat["templates"]["chat"]["items"])

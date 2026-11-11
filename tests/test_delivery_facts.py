"""投递事实：模型必须知道「我刚发过什么」。

三个边界都来自真实故障，不是假想：
- 一次总结四个群，索引只留最近三份 → 第一份图明明发出去了，`mark_delivered()` 却找不到，
  投递事实如果挂在索引上就跟着丢了；
- 后台发来新报告时把讨论焦点抢走 → 「第二条」指向了另一份，两块提示还都写着新的那份；
- 回执发送失败仍要求模型闭嘴 → 用户最后一个字都收不到。

投递记录（deliveries）只按条数封顶、单独于索引存在；渲染走只读路径，
不能因为「看一眼快照」就在空 state 上凭空建出会话数据。
"""

import asyncio
import copy
import time

import reports
from conftest import make_plugin

UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"


class Ctx:
    """假的发送接口：能返回 True / False / 抛异常三种结果。"""

    def __init__(self, result=True, boom=False):
        self.result = result
        self.boom = boom
        self.sent = []

    async def send_message(self, umo, chain):
        if self.boom:
            raise RuntimeError("连接断了")
        self.sent.append(umo)
        return self.result


class Event:
    """只提供插件真正会读的字段。"""

    def __init__(self, text="帮我总结一下今天", group_id=""):
        self.unified_msg_origin = UMO
        self.message_str = text
        self.extras = {}
        self._group_id = group_id

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return OWNER

    def get_platform_id(self):
        return "bot_a"

    def set_extra(self, key, value):
        self.extras[key] = value


def make(tmp_path, **attrs):
    p, mod = make_plugin(
        owner_uids={OWNER},
        group_ids=["g1"],
        group_name_map={"g1": "群1", "g9": "群9"},
        state=attrs.pop("state", {}),
        reports_dir=tmp_path / "reports",
        **attrs,
    )
    p._save_state = lambda: None
    p.context = Ctx(True)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
    return p, mod


def record(rid, *, gid="g1", group="群1", label="今天", titles=("话题A",)):
    return {
        "id": rid,
        "gid": gid,
        "group": group,
        "range": {"label": label},
        "generated_at": int(time.time()),
        "topics": [{"no": i + 1, "title": t} for i, t in enumerate(titles)],
        "image": f"{rid}.png",
        "status": "complete",
    }


def put(p, rid, **kw):
    """把报告写进归档并建出图片文件，模拟「生成完了」。"""
    rec = record(rid, **kw)
    reports.save(p.reports_dir, rec)
    (p.reports_dir / f"{rid}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return rec


# ---------------------------------------------------------------- 一次四群


def test_batch_of_four_keeps_every_delivery(tmp_path):
    """四个群各发一份：索引只剩最近三份，投递记录必须四份都在。

    这条就是 `mark_delivered()` 返回 False 的那条路径：投递事实不能挂在索引上。
    """
    p, _ = make(tmp_path)
    for index in range(4):
        put(p, f"r{index}", gid=f"g{index}", group=f"群{index}")
        reports.resolve(p.state, UMO, record(f"r{index}", gid=f"g{index}", group=f"群{index}"))

    ids = [e.get("id") for e in reports.entries(p.state, UMO)]
    assert "r0" not in ids and len(ids) == reports.INDEX_KEEP, "索引应当已经把第一份裁掉"

    for index in range(4):
        assert asyncio.run(
            p._deliver_report(
                UMO, p.reports_dir / f"r{index}.png", gid=f"g{index}", range_label="今天"
            )
        )

    shipped = [d["report_id"] for d in reports.deliveries(p.state, UMO)]
    assert shipped == ["r0", "r1", "r2", "r3"], "每份成功投递都要留下事实"

    text = reports.delivery_blurb(p.state, UMO, p.reports_dir)
    assert "群0" in text and "r0" in text, "被索引裁掉的也要出现在投递快照里"


def test_missing_archive_keeps_storage_unknown(tmp_path):
    """只有图没有正文：存档结果未知，不能写成「当时失败了」，话题数也不能写 0。"""
    p, _ = make(tmp_path)
    (p.reports_dir / "ghost.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    assert asyncio.run(
        p._deliver_report(UMO, p.reports_dir / "ghost.png", gid="g9", range_label="昨天")
    )

    shippped = reports.deliveries(p.state, UMO)[-1]
    assert shippped["report_id"] == "ghost"
    assert shippped["stored"] is None, "查不到存档结果就是未知"
    assert shippped["topic_count"] is None, "不知道就别写 0"
    assert shippped["group"] == "群9", "归档缺失时用群号兜底"
    assert shippped["range_label"] == "昨天"

    text = reports.delivery_blurb(p.state, UMO, p.reports_dir)
    assert "当时存档状态未知" in text


def test_delivery_keeps_no_focus_side_effect(tmp_path):
    """投递只记事实，不动焦点（焦点由读取建立）。"""
    p, _ = make(tmp_path)
    put(p, "one")
    reports.resolve(p.state, UMO, record("one"))
    asyncio.run(p._deliver_report(UMO, p.reports_dir / "one.png"))
    assert reports.get_focus(p.state, UMO) == {}
    assert [d["report_id"] for d in reports.deliveries(p.state, UMO)] == ["one"]


def test_snapshot_lists_newest_first(tmp_path):
    """先投递 A 再投递 B：快照第 1 条必须是 B，默认读取也要指向 B。

    列表本身按追加顺序存（老的在前），但提示词里「刚发来的那份」指最近一条。
    以前照原序渲染，模型被指去读第 1 条，读到的其实是最旧那份。
    """
    p, _ = make(tmp_path)
    put(p, "olderA")
    reports.resolve(p.state, UMO, record("olderA"))
    put(p, "newerB")
    reports.resolve(p.state, UMO, record("newerB"))

    asyncio.run(p._deliver_report(UMO, p.reports_dir / "olderA.png"))
    asyncio.run(p._deliver_report(UMO, p.reports_dir / "newerB.png"))

    assert [d["report_id"] for d in reports.deliveries(p.state, UMO)] == [
        "olderA",
        "newerB",
    ], "底层仍按追加顺序存"

    text = reports.delivery_blurb(p.state, UMO, p.reports_dir)
    first = next(line for line in text.splitlines() if line.startswith("1)"))
    assert "newerB" in first, f"「刚发来的」必须排在第 1 条：{first}"

    rid, _why = p._pick_report(UMO, "", "")
    assert rid == "newerB", "默认读取要和快照第 1 条指向同一份"


# ---------------------------------------------------------------- 正文可读性


def test_body_state_covers_four_cases(tmp_path):
    """文件在不在是当前事实，stored 是当时记录，四种组合都要说清。"""
    p, _ = make(tmp_path)
    reports.save(p.reports_dir, record("here"))
    (p.reports_dir / "here.png").write_bytes(b"x")
    reports.append_delivery(p.state, UMO, report_id="here", stored=True)
    reports.append_delivery(p.state, UMO, report_id="gone_ok", stored=True)
    reports.append_delivery(p.state, UMO, report_id="gone_bad", stored=False)
    reports.append_delivery(p.state, UMO, report_id="gone_unknown", stored=None)

    text = reports.delivery_blurb(p.state, UMO, p.reports_dir)
    assert "正文文件存在，可调用 get_group_report 读取" in text
    assert "当时存档成功，当前正文文件缺失" in text
    assert "当时存档失败，当前正文文件缺失" in text
    assert "当前正文文件缺失，当时存档状态未知" in text


def test_storage_unknown_is_filled_from_index_when_possible(tmp_path):
    """投递记录里不知道存档结果时，索引还在就先问索引，别直接报未知。"""
    p, _ = make(tmp_path)
    put(p, "known")
    reports.resolve(p.state, UMO, record("known"), stored=False)
    reports.append_delivery(p.state, UMO, report_id="known", stored=None)
    # 正文文件没了（只有图），才会走到「问索引要存档结果」这条分支
    (p.reports_dir / "known.json").unlink()

    text = reports.delivery_blurb(p.state, UMO, p.reports_dir)
    assert "当时存档失败" in text, "索引里写着没存下，就该照着说"


def test_unknown_delivery_time_is_stated(tmp_path):
    """没记到投递时间就说时间未知，不拿 ts / generated_at 冒充。"""
    p, _ = make(tmp_path)
    entry = reports.append_delivery(p.state, UMO, report_id="rid", stored=True)
    entry["delivered_at"] = 0
    assert "时间未知" in reports.delivery_blurb(p.state, UMO, p.reports_dir)


# ---------------------------------------------------------------- 记录本身


def test_delivery_log_caps_at_five_and_counts_seq(tmp_path):
    p, _ = make(tmp_path)
    for index in range(7):
        reports.append_delivery(p.state, UMO, report_id=f"r{index}")
    log = reports.deliveries(p.state, UMO)
    assert [d["report_id"] for d in log] == ["r2", "r3", "r4", "r5", "r6"]
    assert [d["seq"] for d in log] == [3, 4, 5, 6, 7], "重发同一份要靠 seq 分辨"


def test_rendering_is_read_only(tmp_path):
    """空 state 上渲染快照不能建出会话键，也不能改业务状态。"""
    p, _ = make(tmp_path)
    before = copy.deepcopy(p.state)
    assert reports.delivery_blurb(p.state, UMO, p.reports_dir) == ""
    assert reports.blurb(p.state, UMO) == ""
    assert p.state == before == {}


# ---------------------------------------------------------------- 回执真值


def receipt_plugin(tmp_path, status="accepted", receipt_ok=True):
    p, mod = make(tmp_path)

    async def fake_parse(umo, text, **_kw):
        return mod.DigestRequest(UMO, 0, 1, "今天", False, "g1"), ""

    async def fake_submit(_req):
        return status, "开始生成：今天｜1 个群：群1"

    async def fake_send(_umo, _text):
        return receipt_ok

    p._parse_digest = fake_parse
    p._submit_digest = fake_submit
    p._send_text = fake_send
    return p, mod


def test_receipt_failure_lets_the_model_speak(tmp_path):
    """回执没发出去就不能要求模型闭嘴，否则用户什么都收不到。"""
    for status in ("accepted", "queued", "rejected"):
        p, _ = receipt_plugin(tmp_path, status=status, receipt_ok=False)
        event = Event()
        reply = asyncio.run(p.tool_group_digest(event, when="今天"))
        assert "回执没能发给用户" in reply
        assert "开始生成：今天" in reply, "要把受理情况交给模型转述"
        assert "chat_intel_tool_sent" not in event.extras, "没回复成功就不能抑制模型"


def test_receipt_success_silences_model_for_all_three_results(tmp_path):
    """回执真的发出去了：三种受理结果都由插件自己说完，模型不再复读。"""
    for status in ("accepted", "queued", "rejected"):
        p, _ = receipt_plugin(tmp_path, status=status, receipt_ok=True)
        event = Event()
        reply = asyncio.run(p.tool_group_digest(event, when="今天"))
        assert "不要再输出任何文字" in reply
        assert event.extras.get("chat_intel_tool_sent") is True


def test_send_text_reports_real_result(tmp_path):
    p, _ = make(tmp_path)
    assert asyncio.run(p._send_text(UMO, "hi")) is True
    p.context = Ctx(result=False)
    assert asyncio.run(p._send_text(UMO, "hi")) is False
    p.context = Ctx(boom=True)
    assert asyncio.run(p._send_text(UMO, "hi")) is False, "抛异常也算没发出去"


# ---------------------------------------------------------------- 请求钩子


class Req:
    def __init__(self):
        self.system_prompt = ""
        self.contexts = []


def test_hook_snapshot_is_stable_and_side_effect_free(tmp_path):
    """连续三轮请求：快照只出现一份、req.contexts 不被写、state 不变。"""
    p, _ = make(tmp_path)
    put(p, "rid1", titles=("话题A", "话题B"))
    reports.append_delivery(
        p.state,
        UMO,
        report_id="rid1",
        group="群1",
        range_label="今天",
        topic_count=2,
        topic_titles=["话题A", "话题B"],
        stored=True,
    )
    before = copy.deepcopy(p.state)

    for _ in range(3):
        req = Req()
        asyncio.run(p.silence_llm(Event(), req))
        assert req.contexts == [], "本轮不往对话上下文里塞东西"
        assert req.system_prompt.count("[本会话最近的报告投递]") == 1
        assert "候选确定后再调用读取或重发工具" in req.system_prompt
        assert "不要先读一份再去确认" in req.system_prompt
        assert p.state == before, "渲染不能改业务状态"

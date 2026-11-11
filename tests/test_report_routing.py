"""路由：生成报告 vs 讨论报告 vs 查证原话，三种意图要分开。"""

import asyncio

import reports
from conftest import make_plugin

GID = "220001"
UMO = "bot_a:FriendMessage:110001"


class Req:
    def __init__(self):
        self.system_prompt = "你是助手"


class Ev:
    def __init__(self, text="", gid="", umo=UMO, sender="110001"):
        self.message_str = text
        self.unified_msg_origin = umo
        self._gid = gid
        self._sender = sender
        self.stopped = False

    def get_platform_id(self):
        return "bot_a"

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender

    def stop_event(self):
        self.stopped = True


def run_hint(p, event, req):
    return asyncio.run(p.silence_llm(event, req))


def plugin(tmp_path, **attrs):
    p, mod = make_plugin(
        reports_dir=tmp_path / "reports",
        group_ids=[GID],
        group_name_map={GID: "極東電子考古學結社"},
        owner_uids={"110001"},
        state={},
        store_dir=tmp_path / "messages",
        silence_platforms=["bot_b"],
        **attrs,
    )
    return p


def test_prompt_splits_generate_discuss_verify(tmp_path):
    p = plugin(tmp_path)
    req = Req()
    run_hint(p, Ev(), req)
    text = req.system_prompt
    assert "group_digest" in text
    assert "get_group_report" in text
    assert "query_group_records" in text
    assert "不要重新生成" in text, "讨论报告时不该再生成一份"
    assert "group_digest、cancel_digest、resend_report" in text
    assert "get_group_report、query_group_records、digest_status 返回后正常回答" in text
    assert "清空 msg_ids 并改用关键词" in text
    assert "显式传 when 扩大时间范围" in text
    assert "按结果给出的 offset" in text
    assert "不要取消" in text


def test_prompt_requires_explicit_report_id(tmp_path):
    """读取/重发必须自带报告 ID：省略 ID 由后端猜，猜错就把正在聊的焦点换掉。"""
    p = plugin(tmp_path)
    req = Req()
    run_hint(p, Ev(), req)
    text = req.system_prompt
    assert "必须传 report_id" in text
    assert "不要省略 ID 让后端替你挑" in text
    assert "resend_report" in text and "digest_status" in text
    # 「有投递记录」时才追加的歧义规则，由 test_delivery_facts 覆盖


def test_prompt_says_to_separate_summary_from_evidence(tmp_path):
    p = plugin(tmp_path)
    req = Req()
    run_hint(p, Ev(), req)
    assert "原始说法" in req.system_prompt
    assert "不要用推测填补" in req.system_prompt


def test_existing_reports_are_advertised(tmp_path):
    p = plugin(tmp_path)
    record = reports.build_record(
        report_id=f"chat_intel_{GID}_20261108_120000_今天",
        gid=GID,
        group="極東電子考古學結社",
        platform="bot_a",
        label="今天",
        since=1762500000,
        until=1762503600,
        topics=[{"title": "刷固件变砖", "summary": "刷机翻车", "msg_nos": [], "count": 2}],
        records=[],
    )
    reports.resolve(p.state, UMO, record)
    req = Req()
    run_hint(p, Ev(), req)
    assert "[已有报告]" in req.system_prompt
    assert record["id"] in req.system_prompt
    assert "刷固件变砖" in req.system_prompt


def test_no_blurb_when_no_report_yet(tmp_path):
    p = plugin(tmp_path)
    req = Req()
    run_hint(p, Ev(), req)
    assert "[已有报告]" not in req.system_prompt


def test_generating_placeholder_is_advertised(tmp_path):
    p = plugin(tmp_path)
    reports.mark_generating(p.state, UMO, gid=GID, group="考古群", label="今天")
    req = Req()
    run_hint(p, Ev(), req)
    assert "正在生成" in req.system_prompt


def test_silenced_platform_still_blocks(tmp_path):
    p = plugin(tmp_path)
    ev = Ev()
    ev.get_platform_id = lambda: "bot_b"
    req = Req()
    assert run_hint(p, ev, req) is True
    assert ev.stopped is True
    assert req.system_prompt == "你是助手", "被拦掉就不该再注入"

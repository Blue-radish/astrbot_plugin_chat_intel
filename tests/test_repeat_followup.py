"""真机验收反馈的两处修补：重复追问不许念占位句；概览要报引用条数。"""

import asyncio
import re
import time
import types

import reports
from conftest import make_plugin

GID = "220001"
UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"


class Ev:
    def __init__(self, text="", gid="", sender=OWNER):
        self.message_str = text
        self.unified_msg_origin = UMO
        self._gid = gid
        self._sender = sender
        self.extras = {}

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "bot_a"

    def stop_event(self):
        pass

    def is_stopped(self):
        return False

    def set_extra(self, k, v):
        self.extras[k] = v

    def get_extra(self, k, d=None):
        return self.extras.get(k, d)


def make(tmp_path):
    p, _ = make_plugin(
        reports_dir=tmp_path / "reports",
        store_dir=tmp_path / "messages",
        images_dir=tmp_path / "images",
        group_ids=[GID],
        group_name_map={GID: "極東電子考古學結社"},
        owner_uids={OWNER},
        state={},
    )
    p._save_state = lambda: None
    return p


def test_hint_forbids_placeholder_when_question_repeats(tmp_path):
    """真机翻车复现：上一轮刚念过原话，用户再问一句，模型只回「（我把原话念给你）」。"""
    p = make(tmp_path)
    req = types.SimpleNamespace(system_prompt="人格设定")
    asyncio.run(p.silence_llm(Ev(), req))
    hint = req.system_prompt
    assert "重复问刚答过的事" in hint
    assert "重新调一次工具" in hint
    assert "占位句" in hint
    assert "我把原话念给你" in hint, "要把它实际回出来的那句原样点名，否则模型不认账"
    # 原有三条不能因此丢掉
    assert "group_digest" in hint and "get_group_report" in hint and "query_group_records" in hint


def test_hint_still_asks_for_three_way_answer(tmp_path):
    p = make(tmp_path)
    req = types.SimpleNamespace(system_prompt="")
    asyncio.run(p.silence_llm(Ev(), req))
    assert "报告里的概括" in req.system_prompt
    assert "群友的原始说法（带时间和人）" in req.system_prompt


def test_outline_reports_ref_counts(tmp_path):
    """概览模式（不指定话题）也要说明每个话题有几条引用，省得逐话题重读。"""
    p = make(tmp_path)
    record = reports.build_record(
        report_id="outline",
        gid=GID,
        group="極東電子考古學結社",
        platform="bot_a",
        label="今天",
        since=time.time() - 3600,
        until=time.time(),
        topics=[
            {"title": "内存条", "summary": "在找条子", "msg_nos": [], "count": 3},
            {"title": "显卡", "summary": "问了一句", "msg_nos": [], "count": 1},
        ],
        records=[],
    )
    record["topics"][0]["refs"] = ["a1", "a2", "a3"]
    record["topics"][1]["refs"] = ["a4"]
    out = p._format_report(record)
    assert "来源引用 3 条" in out
    assert "来源引用 1 条" in out
    assert "共 4 条" in out, "结尾要告诉它一共几条引用"
    assert "按返回的 offset 翻页" in out
    assert "每页最多 20 条命中" in out
    assert "一次调用就能拿全" not in out


def test_outline_without_refs_says_so(tmp_path):
    p = make(tmp_path)
    record = reports.build_record(
        report_id="noref",
        gid=GID,
        group="群",
        platform="bot_a",
        label="今天",
        since=time.time() - 3600,
        until=time.time(),
        topics=[{"title": "话题", "summary": "s", "msg_nos": [], "count": 1}],
        records=[],
    )
    out = p._format_report(record)
    # 注意：「引用核对：…没记来源引用」里也有「来源引用」四个字，所以按行首的计数行判断
    assert not re.search(r"^\s*来源引用 \d+ 条$", out, re.M)
    assert "要查后续或原话，用 query_group_records" in out

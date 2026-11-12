"""`/群总结`：唯一一条「不会因理解误差而漏执行」的显式入口。

自然语言归模型，命令归确定性。校验顺序固定且在最前面：
私聊 → owner → 群配置 → 参数 → 队列；每一层的拒绝理由原样回给用户。
"""

import asyncio
from collections import deque

from conftest import make_plugin, run_command

UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"


class Ev:
    def __init__(self, gid="", sender=OWNER, text="/群总结"):
        self.message_str = text
        self.unified_msg_origin = UMO
        self._gid = gid
        self._sender = sender

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "bot_a"

    def plain_result(self, text):
        from types import SimpleNamespace

        return SimpleNamespace(chain=[SimpleNamespace(text=text)])


def make(mock_submit=True, **attrs):
    base = dict(
        owner_uids={OWNER},
        group_ids=["1"],
        group_name_map={"1": "测试群"},
        state={},
    )
    base.update(attrs)
    p, _mod = make_plugin(**base)
    p._save_state = lambda: None
    p._group_name = lambda gid: asyncio.sleep(0, result="测试群")
    submitted = []
    if mock_submit:

        async def fake_submit(req):
            submitted.append(req)
            return "accepted", "开始生成：今天｜测试群"

        p._submit_digest = fake_submit
    else:

        async def fake_execute(request):
            return None

        p._execute = fake_execute
        p._spawn = lambda coro: (coro.close(), object())[1]
    return p, submitted


def test_group_chat_is_refused():
    p, submitted = make()
    out = run_command(p, Ev(gid="1"), arg="今天")
    assert out == ["群总结只在私聊里用，群里不发报告。"]
    assert submitted == []


def test_missing_owner_points_at_the_setting():
    p, submitted = make(owner_uids=set())
    out = run_command(p, Ev(), arg="今天")
    assert "owner_uids" in out[0]
    assert submitted == []


def test_non_owner_is_refused():
    p, submitted = make()
    out = run_command(p, Ev(sender="999"), arg="今天")
    assert out == ["这个命令只有配置里的主人能用。"]
    assert submitted == []


def test_no_groups_configured_is_refused():
    p, submitted = make(group_ids=[])
    out = run_command(p, Ev(), arg="今天")
    assert "要总结的群" in out[0]
    assert submitted == []


def test_unparsable_argument_is_explained_not_guessed():
    """群名对不上时不许退回「全部群」白烧一轮。"""
    p, submitted = make()
    out = run_command(p, Ev(), arg="今天的Slack群")
    assert submitted == []
    assert "没找到" in out[0]


def test_accepted_reports_the_real_result():
    p, submitted = make()
    out = run_command(p, Ev(), arg="今天")
    assert submitted and submitted[0].label == "今天"
    assert out == ["开始生成：今天｜测试群"]


def test_queued_when_busy():
    p, _submitted = make(mock_submit=False, _running=True)
    out = run_command(p, Ev(), arg="今天")
    assert len(p._queue) == 1, "忙的时候要排队，不能丢"
    assert "排队" in out[0]


def test_rejected_when_queue_is_full():
    p, _submitted = make(mock_submit=False, _running=True, _queue=deque([None, None], maxlen=2))
    out = run_command(p, Ev(), arg="今天")
    assert "排着" in out[0]
    assert len(p._queue) == 2


def test_invalid_date_is_refused_not_silently_incremental():
    """`2026-02-30` 认不出来就说认不出来，别悄悄按「补一次最新」跑。"""
    p, submitted = make(mock_submit=False)
    out = run_command(p, Ev(), arg="2026-02-30 测试群")
    assert submitted == []
    assert "没认出时间" in out[0]
    assert "今天 / 昨天" in out[0], "要给出能用的写法"
    assert not p._queue, "拒绝时不许提交"


def test_unknown_month_word_is_refused():
    p, submitted = make(mock_submit=False)
    out = run_command(p, Ev(), arg="上个月 测试群")
    assert "没认出时间" in out[0]
    assert not p._queue


def test_plain_chatter_after_time_is_not_an_error():
    """「今天的聊了啥」剩下的「聊了啥」是闲聊，不该让命令失败。"""
    p, submitted = make()
    out = run_command(p, Ev(), arg="今天的聊了啥")
    assert submitted and submitted[0].label == "今天"


def test_no_argument_means_incremental():
    p, submitted = make()
    run_command(p, Ev(), arg="")
    assert submitted and submitted[0].incremental is True

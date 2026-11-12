"""受理回执由插件自己发，格式统一、说清跑什么。

以前这句回执是工具返回值，模型爱念就念、不念就没了：单群时用户看到
「已在后台开始生成（范围：今天，目标：电脑互助群）」，说「全部群总结」时却什么都没收到。
同一个动作两种体验，全靠模型当下心情。
"""

import asyncio
import pathlib

from conftest import make_plugin
from interest import ChatInterest

UMO = "bot_a:FriendMessage:110001"
GID, GID2, GID3 = "220003", "220002", "220001"


def fresh(**attrs):
    sent = []

    async def send_text(umo, text):
        sent.append(text)
        return True  # 真实 _send_text 现在会回报发送结果，这里模拟「发出去了」

    p, mod = make_plugin(
        chats=[
            ChatInterest(GID, "bot_b", ("电脑",), (), "电脑互助群"),
            ChatInterest(GID2, "bot_b", ("电脑",), (), "MeowShelter"),
            ChatInterest(GID3, "bot_b", ("电脑",), (), "極東電子考古學結社"),
        ],
        group_ids=[GID, GID2, GID3],
        group_name_map={GID: "电脑互助群", GID2: "MeowShelter", GID3: "極東電子考古學結社"},
        owner_uids={"110001"},
        state={},
        reports_dir=pathlib.Path("unused"),
        **attrs,
    )
    p._save_state = lambda: None
    p._send_text = send_text
    p._spawn = lambda coro: (coro.close(), object())[1]
    return p, mod, sent


def test_ack_for_single_group_names_the_group():
    p, mod, sent = fresh()
    asyncio.run(p._submit_digest(mod.DigestRequest(UMO, 0, 1, "今天", True, GID)))
    assert sent == [], "提交本身不发言，回执由调用方发（工具/直通各一处）"
    assert p._submit_ack(mod.DigestRequest(UMO, 0, 1, "今天", True, GID), True) == "开始生成：今天｜电脑互助群"


def test_ack_for_all_groups_lists_every_group():
    p, mod, _ = fresh()
    text = p._submit_ack(mod.DigestRequest(UMO, 0, 1, "今天", True, None), True)
    assert text == "开始生成：今天｜3 个群：电脑互助群、MeowShelter、極東電子考古學結社"


def test_ack_marks_rerun():
    p, mod, _ = fresh()
    text = p._submit_ack(mod.DigestRequest(UMO, 0, 1, "今天", False, GID, again=True), True)
    assert text == "开始生成：今天｜电脑互助群（重跑）"


def test_ack_marks_queued_request():
    p, mod, _ = fresh()
    text = p._submit_ack(mod.DigestRequest(UMO, 0, 1, "今天", True, GID), False)
    assert text == "已排队：今天｜电脑互助群（等前面跑完）"


def test_ack_marks_rerun_and_queue_together():
    p, mod, _ = fresh()
    text = p._submit_ack(mod.DigestRequest(UMO, 0, 1, "今天", False, GID, again=True), False)
    assert text == "已排队：今天｜电脑互助群（重跑、等前面跑完）"


class FakeEvent:
    def __init__(self, text="全部总结", umo=UMO):
        self.unified_msg_origin = umo
        self.message_str = text
        self.extras = {}

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return "110001"

    def plain_result(self, text):
        from types import SimpleNamespace

        return SimpleNamespace(chain=[SimpleNamespace(text=text)])


def test_tool_sends_the_ack_itself_and_tells_the_model_to_stay_quiet():
    """工具路径必须自己发言：模型念不念这行，用户都得看到同样一句话。"""
    p, mod, sent = fresh()
    p._tool_allowed = lambda event: True

    async def parse_digest(umo, text, *, again=False, named_group=""):
        return mod.DigestRequest(UMO, 0, 1, "今天", True, None), ""

    p._parse_digest = parse_digest
    event = FakeEvent()

    out = asyncio.run(p.tool_group_digest(event, when="今天", group=""))

    assert len(sent) == 1 and sent[0].startswith("开始生成：今天｜3 个群"), sent
    # 受理事实要留在工具结果里（排队还是开跑，下一轮得知道）；沉默由 hook 保证，不靠这句话
    assert "受理结果=accepted" in out, out
    assert "开始生成：今天" in out
    assert "不要" in out or "沉默" in out
    assert event.get_extra("chat_intel_tool_sent") is True


def test_tool_entry_sends_the_same_ack_text():
    """生成工具应向用户发送完整的受理范围。"""
    from conftest import run_digest_tool

    p, mod, _sent = fresh()
    p._save_state = lambda: None
    async def fake_execute(request):
        return None

    p._execute = fake_execute
    p._spawn = lambda coro: (coro.close(), object())[1]

    out = run_digest_tool(p, FakeEvent(text="群聊情报， 今天"), arg="今天")
    assert out == ["开始生成：今天｜3 个群：电脑互助群、MeowShelter、極東電子考古學結社"]

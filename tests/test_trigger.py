from conftest import make_plugin
from intent import detect_intent


def _plugin():
    p, _ = make_plugin()
    p.smart_parse = True
    p.group_name_map = {"220001": "極東電子考古學結社"}
    p.group_ids = ["220001"]
    p._running = False
    return p


def test_weak_digest_requires_time_or_group():
    p = _plugin()
    with_time = detect_intent("群里今天聊了啥")
    without = detect_intent("群里聊了啥")
    assert p._should_act("群里今天聊了啥", with_time) is True
    assert p._should_act("群里聊了啥", without) is False


def test_weak_digest_accepts_group_name():
    p = _plugin()
    text = "极东群聊了啥"
    assert p._should_act(text, detect_intent(text)) is True


def test_status_and_resend_need_strong_anchor():
    p = _plugin()
    assert p._should_act("群聊情报系统现在什么状态", detect_intent("群聊情报系统现在什么状态")) is True
    assert p._should_act("现在什么情况", detect_intent("现在什么情况")) is False
    assert p._should_act("再发我一遍", detect_intent("再发我一遍")) is False


def test_unrelated_text_does_nothing():
    p = _plugin()
    for text in ("今天天气怎么样", "总结一下这段代码", "帮我看下这个 bug"):
        assert p._should_act(text, detect_intent(text)) is False


def test_command_entry_is_gone():
    _p, mod = make_plugin()
    assert not hasattr(mod.TechDigestPlugin, "cmd_digest")

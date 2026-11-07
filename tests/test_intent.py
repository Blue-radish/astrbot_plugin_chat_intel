from intent import detect_intent


def test_status_requires_strong_anchor():
    got = detect_intent("群聊情报系统现在什么状态")
    assert got is not None and got.kind == "status" and got.strong is True


def test_bare_status_phrase_does_not_fire():
    assert detect_intent("现在什么情况") is None
    assert detect_intent("再发我一遍") is None


def test_code_summary_does_not_fire():
    assert detect_intent("总结一下这段代码") is None


def test_digest_with_plugin_anchor():
    got = detect_intent("群聊总结今天极东群聊了啥")
    assert got is not None and got.kind == "digest"


def test_digest_weak_anchor_is_flagged():
    got = detect_intent("群里今天聊了啥")
    assert got is not None and got.kind == "digest" and got.strong is False


def test_rerun_and_resend():
    assert detect_intent("群聊情报今天的重跑一遍").kind == "rerun"
    assert detect_intent("群聊情报把上一份再发我").kind == "resend"


def test_unrelated_text():
    assert detect_intent("今天天气怎么样") is None

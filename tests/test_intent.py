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


def test_digest_natural_phrasings():
    """"跑一份""来一份""出一份"都是要报告，不是别的功能。"""
    for text in (
        "群聊情报把今天的MeowShelter跑一份",
        "群聊情报来一份今天的",
        "情报系统出一份昨天的",
    ):
        got = detect_intent(text)
        assert got is not None and got.kind == "digest", text


def test_unrelated_text():
    assert detect_intent("今天天气怎么样") is None

# ---------------------------------------------------------------- 取消
def test_cancel_with_strong_anchor():
    got = detect_intent("群聊情报取消")
    assert got is not None and got.kind == "cancel"


def test_cancel_variants():
    for text in ("群聊情报别跑了", "日报停下", "情报不看了"):
        got = detect_intent(text)
        assert got is not None and got.kind == "cancel", text


def test_bare_cancel_does_not_fire():
    """「取消」两个字满世界都有（取消订阅、取消订单），没有指向词不许抢话。"""
    assert detect_intent("取消") is None
    assert detect_intent("帮我取消一下这个订单") is None


def test_cancel_beats_digest_in_same_sentence():
    got = detect_intent("群聊情报取消刚才那份总结")
    assert got is not None and got.kind == "cancel"

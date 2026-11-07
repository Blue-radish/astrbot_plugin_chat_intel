from interest import (
    ChatInterest,
    build_digest_prompt,
    group_sections,
    interest_fingerprint,
    parse_chats,
)

RAW = [
    {
        "gid": "220001",
        "platform": "bot_b",
        "want": "复古硬件\n元件行情\n\n复古硬件",
        "not_want": "手机开箱",
        "keywords": "PCB, 电容",
    },
    {"gid": "220001", "platform": "bot_a", "want": "重复行应被丢弃"},
    {"gid": "220002", "want": "AI 工具", "not_want": ""},
    {"gid": "", "want": "空群号应被跳过"},
]


def test_parse_chats_trims_and_dedupes():
    chats, skipped = parse_chats(RAW, fallback_platform="bot_b")
    assert [c.gid for c in chats] == ["220001", "220002"]
    assert chats[0].want == ("复古硬件", "元件行情")
    assert chats[0].keywords == ("PCB", "电容")
    assert chats[0].platform == "bot_b"
    assert chats[1].platform == "bot_b"  # 回退到 fallback
    assert chats[0].not_want == ("手机开箱",)
    assert chats[1].not_want == ()
    assert skipped == [""]


def test_fingerprint_ignores_order_but_not_content():
    a = ChatInterest("1", "p", ("甲", "乙"), (), (), "", "")
    b = ChatInterest("1", "p", ("乙", "甲"), (), (), "", "")
    c = ChatInterest("1", "p", ("甲", "乙", "丙"), (), (), "", "")
    assert interest_fingerprint(a) == interest_fingerprint(b)
    assert interest_fingerprint(a) != interest_fingerprint(c)
    assert len(interest_fingerprint(a)) == 12


def test_prompt_puts_rules_after_transcript():
    chat = ChatInterest("1", "p", ("复古硬件",), ("手机开箱",), ("PCB",), "", "")
    prompt = build_digest_prompt(chat, "古董群", "今天", "#1 甲: 我有块 486")
    assert prompt.index("#1 甲: 我有块 486") < prompt.index("复古硬件")
    assert "手机开箱" in prompt
    assert "want_item" in prompt
    assert "quotes" in prompt


def test_sections_follow_want_order_and_skip_empty():
    chat = ChatInterest("1", "p", ("甲主题", "乙主题"), (), (), "", "")
    items = [
        {"want_item": "乙主题", "topic": "只属于乙", "detail": "", "kind": "讨论"},
        {"want_item": "甲主题", "topic": "属于甲", "detail": "", "kind": "讨论"},
        {"want_item": "甲主题", "topic": "也属于甲", "detail": "", "kind": "讨论"},
    ]
    sections, truncated = group_sections(chat, items)
    assert [title for title, _ in sections] == ["甲主题", "乙主题"]
    assert [len(bucket) for _, bucket in sections] == [2, 1]
    assert truncated == 0


def test_section_cap_counts_truncation():
    chat = ChatInterest("1", "p", ("甲主题",), (), (), "", "")
    items = [{"want_item": "甲主题", "topic": f"t{i}", "detail": "", "kind": "讨论"} for i in range(14)]
    sections, truncated = group_sections(chat, items)
    assert len(sections) == 1
    assert len(sections[0][1]) == 12
    assert truncated == 2


def test_unmatched_items_go_to_other_section():
    chat = ChatInterest("1", "p", ("甲主题",), (), (), "", "")
    items = [{"want_item": "不相干", "topic": "x", "detail": "", "kind": "讨论"}]
    sections, _ = group_sections(chat, items)
    assert [title for title, _ in sections] == ["其他"]
    assert len(sections[0][1]) == 1


def test_empty_want_puts_everything_in_other():
    chat = ChatInterest("1", "p", (), (), (), "", "")
    items = [{"want_item": "随便", "topic": "y", "detail": "", "kind": "讨论"}]
    sections, truncated = group_sections(chat, items)
    assert [title for title, _ in sections] == ["其他"]
    assert len(sections[0][1]) == 1
    assert truncated == 0

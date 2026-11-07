from interest import (
    ChatInterest,
    build_digest_prompt,
    build_vision_prompt,
    interest_fingerprint,
    merge_topics,
    normalize_topics,
    parse_chats,
    split_names,
    topic_members,
    topic_time_range,
)

RAW = [
    {
        "gid": "220001",
        "platform": "bot_b",
        "tags_pos": "复古硬件\n元件行情\n\n复古硬件",
        "tags_neg": "手机开箱",
    },
    {"gid": "220001", "platform": "bot_a", "tags_pos": "重复行应被丢弃"},
    {"gid": "220002", "tags_pos": "AI 工具", "tags_neg": ""},
    {"gid": "", "tags_pos": "空群号应被跳过"},
]


def test_parse_chats_trims_and_dedupes():
    chats, skipped = parse_chats(RAW, fallback_platform="bot_b")
    assert [c.gid for c in chats] == ["220001", "220002"]
    assert chats[0].tags_pos == ("复古硬件", "元件行情")
    assert chats[0].platform == "bot_b"
    assert chats[1].platform == "bot_b"  # 回退到 fallback
    assert chats[0].tags_neg == ("手机开箱",)
    assert chats[1].tags_neg == ()
    assert skipped == [""]


def test_parse_chats_accepts_comma_and_legacy_want():
    """顿号、逗号都能分；旧配置里的 want 长句也能直接当标签用。"""
    chats, _ = parse_chats(
        [{"gid": "1", "tags_pos": "电脑、动漫,科研"}, {"gid": "2", "want": "复古硬件\n维修"}]
    )
    assert chats[0].tags_pos == ("电脑", "动漫", "科研")
    assert chats[1].tags_pos == ("复古硬件", "维修")


def test_fingerprint_ignores_order_but_not_content():
    a = ChatInterest("1", "p", ("甲", "乙"), (), "", "")
    b = ChatInterest("1", "p", ("乙", "甲"), (), "", "")
    c = ChatInterest("1", "p", ("甲", "乙", "丙"), (), "", "")
    assert interest_fingerprint(a) == interest_fingerprint(b)
    assert interest_fingerprint(a) != interest_fingerprint(c)
    assert len(interest_fingerprint(a)) == 12


def test_prompt_puts_rules_after_transcript():
    chat = ChatInterest("1", "p", ("复古硬件",), ("手机开箱",), "", "")
    prompt = build_digest_prompt(chat, "古董群", "今天", "#1 甲: 我有块 486")
    assert prompt.index("#1 甲: 我有块 486") < prompt.index("复古硬件")
    assert "手机开箱" in prompt
    assert '"topics"' in prompt
    assert '"msg_nos"' in prompt
    assert "quotes" in prompt


def test_prompt_asks_for_complete_narrative():
    """话题要讲完整：prompt 必须点明按事件分话题、长话题写细。"""
    chat = ChatInterest("1", "p", ("复古硬件",), (), "", "")
    prompt = build_digest_prompt(chat, "古董群", "今天", "#1 甲: hi")
    assert "一个话题 = 群友围绕同一件事来回聊的一段" in prompt
    assert "300-600 字" in prompt
    assert "80-150 字" in prompt


def test_vision_prompt_uses_tags():
    chat = ChatInterest("1", "p", ("复古硬件",), ("手机开箱",), "", "")
    prompt = build_vision_prompt(chat, "古董群", "上文", "看图", "下文")
    assert "复古硬件" in prompt and "手机开箱" in prompt
    assert "这个群关心的正向标签" in prompt


def test_topics_keep_only_configured_tags():
    chat = ChatInterest("1", "p", ("电脑", "科研"), (), "", "")
    topics = [
        {"title": "本地跑大模型", "tags": ["电脑"], "msg_nos": [1, 2], "summary": "讲了很久。"},
        {"title": "乱入的八卦", "tags": ["八卦"], "msg_nos": [3], "summary": "不在标签里。"},
        {"title": "没打标签", "tags": [], "msg_nos": [4], "summary": "没有标签就不算。"},
    ]
    out = normalize_topics(chat, topics)
    assert [t["title"] for t in out] == ["本地跑大模型"]
    assert out[0]["tags"] == ["电脑"]


def test_topics_sorted_by_message_count_and_deduped():
    chat = ChatInterest("1", "p", ("电脑",), (), "", "")
    topics = [
        {"title": "小事", "tags": ["电脑"], "msg_nos": [1], "summary": "短。"},
        {"title": "大事", "tags": ["电脑"], "msg_nos": [10, 11, 12], "summary": "长。"},
        {"title": "1. 大事", "tags": ["电脑"], "msg_nos": [13], "summary": "长的更长的版本。"},
    ]
    out = normalize_topics(chat, topics)
    assert [t["title"] for t in out] == ["大事", "小事"]
    assert out[0]["msg_nos"] == [10, 11, 12, 13]


def test_topics_drop_entries_without_numbers_or_summary():
    chat = ChatInterest("1", "p", ("电脑",), (), "", "")
    topics = [
        {"title": "没编号", "tags": ["电脑"], "msg_nos": [], "summary": "x"},
        {"title": "没总结", "tags": ["电脑"], "msg_nos": [1], "summary": ""},
    ]
    assert normalize_topics(chat, topics) == []


def test_merge_topics_joins_same_story_across_chunks():
    chat = ChatInterest("1", "p", ("电脑",), (), "", "")
    chunk1 = [{"title": "装黑苹果翻车", "tags": ["电脑"], "msg_nos": [1, 2, 3], "summary": "前半段。"}]
    chunk2 = [{"title": "装黑苹果翻车", "tags": ["电脑"], "msg_nos": [4, 5], "summary": "前半段，补上后半段。"}]
    merged = merge_topics(chat, [chunk1, chunk2])
    assert len(merged) == 1
    assert merged[0]["msg_nos"] == [1, 2, 3, 4, 5]
    assert merged[0]["summary"] == "前半段，补上后半段。"


def test_topic_time_range():
    import time

    base = time.mktime(time.strptime("2026-11-07 16:23", "%Y-%m-%d %H:%M"))
    assert topic_time_range([base, base + 11 * 60]) == "16:23-16:34"
    assert topic_time_range([base, base + 5]) == "16:23"
    assert topic_time_range([]) == ""


def test_topic_members_caps_and_dedupes():
    assert topic_members(["阿越", "阿越", "", "摩顺卡"]) == "阿越、摩顺卡"
    assert topic_members(["a", "b", "c", "d", "e"]) == "a、b、c、d 等 5 人"
    assert topic_members([]) == ""


def test_prompt_demands_rich_detail_and_real_nicknames():
    """正文要贴着原文写、直接点名，不要「有群友提到」这种模糊说法。"""
    chat = ChatInterest("1", "p", ("复古硬件",), (), "", "")
    prompt = build_digest_prompt(chat, "古董群", "今天", "#1 甲: hi")
    assert "300-600" in prompt
    assert "有群友提到" in prompt  # 作为反例出现在规则里
    assert "群友昵称" in prompt


def test_split_names_marks_nicknames_inside_sentence():
    """正文里出现的昵称要单独切出来，模板好把它排得浅一点。"""
    parts = split_names("摩顺卡 先提问，阿越 回了句「16G 真不够」。", ["摩顺卡", "阿越"])
    assert [(p["t"], p["n"]) for p in parts] == [
        ("摩顺卡", True),
        (" 先提问，", False),
        ("阿越", True),
        (" 回了句「16G 真不够」。", False),
    ]


def test_split_names_prefers_longest_and_ignores_one_char():
    parts = split_names("阿越咪说天不错", ["阿越", "阿越咪", "天"])
    assert [(p["t"], p["n"]) for p in parts] == [
        ("阿越咪", True),
        ("说天不错", False),  # 单字昵称不参与高亮，免得满屏都是
    ]


def test_split_names_without_match_is_one_plain_part():
    assert split_names("没有任何昵称的句子", ["甲"]) == [
        {"t": "没有任何昵称的句子", "n": False}
    ]
    assert split_names("", ["甲"]) == []

"""意图判定：指向词 + 动作词，双重命中才算。

这个机器人还有别的功能（人设闲聊、B站推送、TTS），所以光有动作词（"什么情况""再发我"）
不能抢话——必须同时出现指向本插件的词（情报 / 群聊情报 / 日报 …）。

不依赖 AstrBot，纯字符串判定，方便单测。
"""

from dataclasses import dataclass

# 明确指向本插件的词。注意不含"插件"：B站推送、TTS 也都是插件，
# 光说"某某插件什么情况"不能算在找群聊情报。
ANCHOR_STRONG = ("群聊情报", "情报系统", "情报", "日报")
# 泛指群聊，只在有动作词 + 时间词/群名时才算（由调用方补判）
ANCHOR_WEAK = ("群聊总结", "群聊", "群", "聊天记录")

ACTIONS = {
    "status": ("状态", "情况", "正常吗", "采集", "怎么回事"),
    "resend": ("重发", "再发", "上一份", "刚才那份"),
    "rerun": ("重跑", "重新", "再看一遍", "再来一遍"),
    "cancel": ("取消", "别跑了", "停下", "不跑了", "不看了", "终止"),
    "digest": (
        "总结",
        "聊了啥",
        "聊了什么",
        "聊了些啥",
        "讨论了什么",
        "有什么",
        "跑一份",
        "来一份",
        "出一份",
        "生成一份",
    ),
}

# 这几个必须强指向，否则很容易抢别的功能的话：「取消」满世界都有（取消订单、取消订阅）
REQUIRE_STRONG = ("status", "resend", "cancel")

# 弱指向（只说"群 / 群聊"）时只认这些明确的"要报告"动作。
# "有什么""情况"这类太泛，必须配合强指向词，否则「群里今天有什么好玩的」也会触发。
WEAK_ACTIONS = (
    "总结",
    "聊了啥",
    "聊了什么",
    "聊了些啥",
    "讨论了什么",
    "跑一份",
    "来一份",
    "出一份",
    "生成一份",
)

# 具体优先：取消最先（「取消刚才那份总结」不能当成又要一份），再说"看状态"，最后才是重跑/总结
_ORDER = ("cancel", "status", "resend", "rerun", "digest")


@dataclass(frozen=True)
class Intent:
    kind: str
    anchor: str
    action: str
    strong: bool


def _first_hit(text: str, words: tuple[str, ...]) -> str:
    for word in words:
        if word in text:
            return word
    return ""


# 「总结得真好」「做得真好，这个群总结」是在夸，不是在要东西。
# 动词后面接「得 + 程度词」是补语；句子到动词就收尾则是名词短语。两种都不是请求。
_DEGREE = ("真", "很", "太", "非常", "挺", "好", "不错", "棒", "漂亮", "完美")
# 只有「总结」会被当名词/补语用错：「跑一份」「聊了啥」这类自带句式，不存在歧义
_AMBIGUOUS_ACTIONS = ("总结",)


def _digest_is_request(text: str) -> bool:
    """「要报告」至少要有一个动作词落在请求位置上。

    夸奖句同样会被动作词「总结」命中，但用法不对：`总结得真好` 是补语，
    `这个群总结` 是名词短语。放过这两种，否则一句夸奖就白跑一次生成——
    用户夸完紧接着又收到一份新报告，比不回应更糟。
    """
    for action in ACTIONS["digest"]:
        if action not in text:
            continue
        if action not in _AMBIGUOUS_ACTIONS:
            return True
        start = text.find(action)
        while start >= 0:
            before = text[:start]
            after = text[start + len(action) :]
            trimmed = after.strip(" \t　！。，,、？?～~!…")
            evaluative = after.startswith("得") and after[1:2] in _DEGREE
            attributive = after.startswith("的")
            # 「做得真好，这个群总结」（句末，前面是名词中心语）
            noun_at_end = not trimmed and before.rstrip(" \t　")[-1:] in ("群", "个", "份", "条")
            if not (evaluative or attributive or noun_at_end):
                return True
            start = text.find(action, start + 1)
    return False


def detect_intent(text: str) -> Intent | None:
    """返回命中的意图，没有就返回 None。"""
    if not text:
        return None
    text = text.strip()
    if not text:
        return None

    strong_anchor = _first_hit(text, ANCHOR_STRONG)
    weak_anchor = _first_hit(text, ANCHOR_WEAK)
    anchor = strong_anchor or weak_anchor
    if not anchor:
        return None
    strong = bool(strong_anchor)

    for kind in _ORDER:
        action = _first_hit(text, ACTIONS[kind])
        if not action:
            continue
        if kind in REQUIRE_STRONG and not strong:
            continue
        if not strong and action not in WEAK_ACTIONS:
            continue
        if kind == "digest" and not _digest_is_request(text):
            # 只是在夸「上一份总结」：交给模型正常回话（要不要读报告由回应决定），
            # 不能在这里就停掉事件并起一次生成。
            continue
        return Intent(kind=kind, anchor=anchor, action=action, strong=strong)
    return None

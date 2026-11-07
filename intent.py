"""意图判定：指向词 + 动作词，双重命中才算。

这个机器人还有别的功能（人设闲聊、B站推送、TTS），所以光有动作词（"什么情况""再发我"）
不能抢话——必须同时出现指向本插件的词（情报 / 群聊情报 / 日报 …）。

不依赖 AstrBot，纯字符串判定，方便单测。
"""

from dataclasses import dataclass

# 明确指向本插件的词
ANCHOR_STRONG = ("群聊情报", "情报系统", "情报", "日报", "插件")
# 泛指群聊，只在有动作词 + 时间词/群名时才算（由调用方补判）
ANCHOR_WEAK = ("群聊总结", "群聊", "群", "聊天记录")

ACTIONS = {
    "status": ("状态", "情况", "正常吗", "采集", "怎么回事"),
    "resend": ("重发", "再发", "上一份", "刚才那份"),
    "rerun": ("重跑", "重新", "再看一遍", "再来一遍"),
    "digest": ("总结", "聊了啥", "聊了什么", "聊了些啥", "讨论了什么", "有什么"),
}

# 这四个里，状态与重发必须强指向，否则很容易抢别的功能的话
REQUIRE_STRONG = ("status", "resend")

# 具体优先：先说"看状态"，再说"重跑/总结"，避免一句话里同时命中时判错
_ORDER = ("status", "resend", "rerun", "digest")


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
        return Intent(kind=kind, anchor=anchor, action=action, strong=strong)
    return None

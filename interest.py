"""每群一份兴趣配置：解析、指纹、提示词、分节归并。

不依赖 AstrBot，纯函数，方便单测。设计约束见
docs/superpowers/specs/2026-11-07-interest-digest-design.md：
- 指令必须写在 user 消息里，且**放在聊天记录之后**（该 provider 只认最后一条 user 消息）；
- 兴趣点就是这个群报告的节标题；归不进去的条目落到「其他」。
"""

from dataclasses import dataclass, field

# 单节最多保留的条目数，超出部分计入"上限截断"
SECTION_CAP = 12

OTHER_SECTION = "其他"


@dataclass(frozen=True)
class ChatInterest:
    gid: str
    platform: str
    want: tuple[str, ...] = ()
    not_want: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    title: str = ""
    name: str = ""


def _split(value) -> tuple[str, ...]:
    """把多行文本 / 列表统一切成去重后的元组。逗号与换行都当分隔符。"""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        pieces = [str(v) for v in value]
    else:
        pieces = str(value).replace("，", ",").replace("\r", "\n").split("\n")
        expanded: list[str] = []
        for line in pieces:
            expanded.extend(line.split(","))
        pieces = expanded
    out: list[str] = []
    for piece in pieces:
        item = piece.strip()
        if item and item not in out:
            out.append(item)
    return tuple(out)


def parse_chats(raw, fallback_platform: str = "") -> tuple[list[ChatInterest], list[str]]:
    """解析配置里的 chats；返回 (有效行, 被跳过的群号)。同群号只留第一行。"""
    if not raw:
        return [], []
    rows = raw.values() if isinstance(raw, dict) else raw
    chats: list[ChatInterest] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        gid = str(row.get("gid") or "").strip()
        if not gid:
            skipped.append(gid)
            continue
        if gid in seen:
            continue
        seen.add(gid)
        chats.append(
            ChatInterest(
                gid=gid,
                platform=str(row.get("platform") or "").strip() or fallback_platform,
                want=_split(row.get("want")),
                not_want=_split(row.get("not_want")),
                keywords=_split(row.get("keywords")),
                title=str(row.get("title") or "").strip(),
                name=str(row.get("name") or "").strip(),
            )
        )
    return chats, skipped


def interest_fingerprint(chat: ChatInterest) -> str:
    """兴趣指纹：只由 want / not_want 决定，顺序无关。用于缓存隔离。"""
    import hashlib

    joined = "\n".join(sorted(chat.want)) + "|" + "\n".join(sorted(chat.not_want))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _numbered(items: tuple[str, ...]) -> str:
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, 1))


_SCHEMA = """{
  "items": [
    {"msg_no": 12, "want_item": "上面某条兴趣点的原文", "topic": "不超过 12 字",
     "detail": "2-4 句：谁遇到了什么、具体细节（库名/版本/报错原文/命令/方案/链接）、结论",
     "kind": "讨论"}
  ],
  "quotes": [
    {"msg_no": 20, "content": "原文，可截取，不超过 80 字", "sender": "昵称", "reason": "一句话点评为什么值得留"}
  ]
}"""

_RULES = """硬性规则：
1. 每条的 msg_no 必须是上面记录里真实出现的 #编号，不许编造；给不出编号的条目直接不要。
2. want_item 必须原样抄写「想要的主题」里的某一条（去掉序号、不要改写）；实在归不进去就写「其他」。
3. 只在「想要的主题」范围内挑内容；「不要的主题」里提到的东西一条都不要。
4. 与主题无关的闲聊、灌水、表情包、纯情绪吐槽不要成条，除非里面有具体信息（报错、版本、命令、价格、链接、结论）。
5. 记录里没有的东西绝对不要编造；链接只能照抄记录里出现过的。
6. detail 里如果对应消息带了 `[图]`，点一句「（有配图）」，方便读者找原图。
7. quotes 只留真的有意思、有信息量或足够好笑的原话，宁少勿多，没有就返回空数组。
8. 只输出 JSON 本体，不要 markdown 代码块，不要任何解释文字。"""


def build_digest_prompt(chat: ChatInterest, group_name: str, window: str, transcript: str) -> str:
    """拼一次总结用的 user 消息：记录在前，规则在后。"""
    want_block = _numbered(chat.want) if chat.want else "（未配置，只做概览：把这段时间最有信息量的内容按话题归类）"
    not_want_block = _numbered(chat.not_want) if chat.not_want else "（无）"
    keyword_block = "、".join(chat.keywords) if chat.keywords else "（无）"

    head = (
        f"下面是群「{group_name}」在 {window} 的聊天记录，每行格式为 `[HH:MM] #编号 昵称: 内容`，"
        f"`[图]` 表示这条带了图片。\n\n聊天记录：\n{transcript}\n"
    )
    tail = (
        "\n———— 以上是记录，以下是要你做的事 ————\n\n"
        f"读完上面这段记录，只挑出与「这个群想要的主题」相关的内容，整理成条目。\n\n"
        f"这个群想要的主题：\n{want_block}\n\n"
        f"这个群不要的主题（出现也不要）：\n{not_want_block}\n\n"
        f"群友常用的关键词（只作提示，不做硬匹配）：\n{keyword_block}\n\n"
        f"输出严格 JSON：\n{_SCHEMA}\n\n{_RULES}\n"
    )
    return head + tail


_VISION_SCHEMA = """{"keep": true 或 false, "kind": "截图/电路图/设备照/表情包/照片/聊天截图 等不超过 6 字", "text": "图里的关键文字，最多 24 字，没有就空字符串"}"""


def build_vision_prompt(chat: ChatInterest, group_name: str, prev: str, cur: str, nxt: str) -> str:
    """判图用的 user 消息：先给上下文，再给这个群的兴趣点，最后要 JSON。"""
    want_block = _numbered(chat.want) if chat.want else "（未配置：按「有信息量、能看懂内容」的尺度判断）"
    not_want_block = _numbered(chat.not_want) if chat.not_want else "（无）"
    return (
        f"这是群「{group_name}」里的一张图，发送时前后文如下。\n\n"
        f"上文：{prev or '（无）'}\n"
        f"本条文字：{cur or '（无文字，纯图片）'}\n"
        f"下文：{nxt or '（无）'}\n\n"
        "———— 以下是要你做的事 ————\n\n"
        f"看图，判断这张图是否与这个群想要的主题有关。\n\n"
        f"这个群想要的主题：\n{want_block}\n\n"
        f"这个群不要的主题：\n{not_want_block}\n\n"
        "判断标准：\n"
        "1. 留下能提供信息的图：报错/代码/终端截图、电路图、原理图、器件或设备照片、"
        "数据图表、网页截图、说明书扫描件。\n"
        "2. 表情包、纯人像自拍、宠物、吃喝、风景、无信息量的糊图，一律不留。\n"
        "3. 看不清内容、拿不准的时候留 false。\n\n"
        f"只输出 JSON 本体，不要代码块：\n{_VISION_SCHEMA}"
    )


def group_sections(chat: ChatInterest, items: list[dict]) -> tuple[list[tuple[str, list[dict]]], int]:
    """按 want 顺序分节；空节不出现；归不进去的进「其他」；返回 (节, 截断数)。"""
    buckets: dict[str, list[dict]] = {text: [] for text in chat.want}
    others: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("want_item") or "").strip()
        if key in buckets:
            buckets[key].append(item)
        else:
            others.append(item)

    sections: list[tuple[str, list[dict]]] = []
    truncated = 0
    for text in chat.want:
        bucket = buckets[text]
        if not bucket:
            continue
        if len(bucket) > SECTION_CAP:
            truncated += len(bucket) - SECTION_CAP
            bucket = bucket[:SECTION_CAP]
        sections.append((text, bucket))

    if others:
        if len(others) > SECTION_CAP:
            truncated += len(others) - SECTION_CAP
            others = others[:SECTION_CAP]
        sections.append((OTHER_SECTION, others))

    return sections, truncated

"""每群一组兴趣标签：解析、指纹、提示词、话题整理。

不依赖 AstrBot，纯函数，方便单测。设计要点见
docs/superpowers/specs/2026-11-07-interest-digest-design.md：
- 报告的一节 = 群友自己聊出来的一个「话题」，不是配置里的某个主题；
- 标签只负责筛（正向保留、负向剔除），不负责分节；
- 指令必须写在 user 消息里，且**放在聊天记录之后**（该 provider 只认最后一条 user 消息）。
"""

import hashlib
import re
from dataclasses import dataclass

# 话题条数上限；0 表示不限
TOPIC_CAP = 0

# 一张话题卡里最多列几个参与人（多了用「等 N 人」带过）
MEMBER_CAP = 4


@dataclass(frozen=True)
class ChatInterest:
    gid: str
    platform: str
    tags_pos: tuple[str, ...] = ()
    tags_neg: tuple[str, ...] = ()
    title: str = ""
    name: str = ""


def _split(value) -> tuple[str, ...]:
    """把多行文本 / 列表统一切成去重后的元组。逗号、顿号与换行都当分隔符。"""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        pieces = [str(v) for v in value]
    else:
        pieces = (
            str(value)
            .replace("，", ",")
            .replace("、", ",")
            .replace("；", ",")
            .replace(";", ",")
            .replace("\r", "\n")
            .split("\n")
        )
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
        # 兼容旧配置：以前叫 want / not_want，长句写着也能当标签用
        tags_pos = _split(row.get("tags_pos")) or _split(row.get("want"))
        tags_neg = _split(row.get("tags_neg")) or _split(row.get("not_want"))
        chats.append(
            ChatInterest(
                gid=gid,
                platform=str(row.get("platform") or "").strip() or fallback_platform,
                tags_pos=tags_pos,
                tags_neg=tags_neg,
                title=str(row.get("title") or "").strip(),
                name=str(row.get("name") or "").strip(),
            )
        )
    return chats, skipped


def interest_fingerprint(chat: ChatInterest) -> str:
    """标签指纹：只由正向 / 负向标签决定，顺序无关。用于缓存隔离。"""
    joined = "\n".join(sorted(chat.tags_pos)) + "|" + "\n".join(sorted(chat.tags_neg))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {text}" for text in items)


_SCHEMA = """{
  "headline": "一句话概括这段时间群里主要在聊什么（不超过 40 字，没有就空字符串）",
  "topics": [
    {"msg_nos": [14, 15, 18],
     "title": "能看出是什么事的标题，带型号/数字/对比更好，不超过 22 字",
     "tags": ["从上面「正向标签」里原样挑 1-3 个"],
     "summary": "贴着聊天记录把这件事讲完整：谁先提起、为什么聊起来、中间的关键细节（型号/版本/报错原文/命令/价格/链接/原话），分歧与最后结论。人名直接写记录里的昵称。"}
  ]
}"""

_RULES = """硬性规则：
1. 一个话题 = 群友围绕同一件事来回聊的一段。同一件事的连续消息（中间有人插话也算）归到同一个话题，
   不许拆成好几条；不同的事分开列。时间范围和参与人由程序按 msg_nos 自动算出，你不要写时间也不要写人名栏。
2. msg_nos 必须是记录里真实出现的 #编号，至少给 1 个，不许编造。
3. tags 只能从「正向标签」里原样挑，一个话题给 1-3 个；挑不出任何一个正向标签的话题，直接不要。
4. 只列与正向标签有关的话题；与「负向标签」有关的一条都不要，哪怕聊得再热闹。
5. 纯寒暄、灌水、表情、没有信息量的情绪不单独成话题；但里面若含具体信息（报错、型号、价格、结论），
   并进相关话题里。
6. 话题按信息量与讨论热度从高到低排。不限条数，但要覆盖全部有价值的话题，别漏。
7. summary 要贴着原文写，别做摘要式压缩：把重要的话、数字、型号、命令、报错、链接照抄进去，
   长话题（多人来回、有细节）写 300-600 字，短话题 80-150 字；宁可细一点，也不要写成两句概括。
   有分歧就写清两边观点和最后共识。对应消息带了 `[图]` 的话点一句「（有配图）」。
8. 正文里直接写群友昵称（从记录里照抄，例如「沂水' Yi 说早入职只能填 16 日」），
   不要写「有群友提到」「有人」「大家」这类模糊说法；确实看不出是谁在说，才用「群里」。
9. 记录里没有的东西绝对不要编造；链接只能照抄记录里出现过的。
10. 只输出 JSON 本体，不要 markdown 代码块，不要任何解释文字。"""


def build_digest_prompt(chat: ChatInterest, group_name: str, window: str, transcript: str) -> str:
    """拼一次总结用的 user 消息：记录在前，规则在后。"""
    pos_block = _bullets(chat.tags_pos) if chat.tags_pos else "（未配置）"
    neg_block = _bullets(chat.tags_neg) if chat.tags_neg else "（无）"

    head = (
        f"下面是群「{group_name}」在 {window} 的聊天记录，每行格式为 `[HH:MM] #编号 昵称: 内容`，"
        f"`[图]` 表示这条带了图片。\n\n聊天记录：\n{transcript}\n"
    )
    tail = (
        "\n———— 以上是记录，以下是要你做的事 ————\n\n"
        "读完整段记录，把群友这段时间**实际聊出来的话题**一个个整理出来（不是按主题分类，"
        "而是按他们真实聊过的事件/讨论来分），再按下面标签筛一遍。\n\n"
        f"我关心的正向标签：\n{pos_block}\n\n"
        f"我不要的负向标签：\n{neg_block}\n\n"
        f"输出严格 JSON：\n{_SCHEMA}\n\n{_RULES}\n"
    )
    return head + tail


_VISION_SCHEMA = """{"keep": true 或 false, "kind": "截图/电路图/设备照/表情包/照片/聊天截图 等不超过 6 字", "text": "图里的关键文字，最多 24 字，没有就空字符串"}"""

def build_vision_prompt(
    chat: ChatInterest, group_name: str, prev: str, cur: str, nxt: str, topic: str = ""
) -> str:
    """判图用的 user 消息：先给上下文，再给这个群的标签，最后要 JSON。

    `topic` 是这张图所在话题的标题：有它，模型才知道这张图是配着哪件事发出来的。
    """
    pos_block = _bullets(chat.tags_pos) if chat.tags_pos else "（未配置：按「有信息量、能看懂内容」的尺度判断）"
    neg_block = _bullets(chat.tags_neg) if chat.tags_neg else "（无）"
    topic_line = f"这张图出现在话题「{topic}」里。\n" if topic else ""
    return (
        f"这是群「{group_name}」里的一张图，发送时前后文如下。\n"
        f"{topic_line}\n"
        f"上文：{prev or '（无）'}\n"
        f"本条文字：{cur or '（无文字，纯图片）'}\n"
        f"下文：{nxt or '（无）'}\n\n"
        "———— 以下是要你做的事 ————\n\n"
        "看图，判断这张图是否值得放进这个群的报告里。\n\n"
        f"这个群关心的正向标签：\n{pos_block}\n\n"
        f"这个群不要的负向标签：\n{neg_block}\n\n"
        "判断标准：\n"
        "1. 留下能提供信息的图：报错/代码/终端截图、电路图、原理图、器件或设备照片、"
        "数据图表、网页截图、说明书扫描件。\n"
        "2. 表情包、纯人像自拍、宠物、吃喝、风景、无信息量的糊图，一律不留。\n"
        "3. 看不清内容、拿不准的时候留 false。\n\n"
        f"只输出 JSON 本体，不要代码块：\n{_VISION_SCHEMA}"
    )


_PUNCT = re.compile(r"^[\s\d]+[.、,，)）:：]?\s*|[\s。，,、；;：:!！?？~～·…\"'“”‘’()（）\[\]【】]+")


def _norm(text: str) -> str:
    """去掉序号、标点、空白、大小写差异，用来判断两个标题是不是同一件事。"""
    return _PUNCT.sub("", str(text or "")).lower()


def _as_int_list(value) -> list[int]:
    out: list[int] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            try:
                num = int(str(item).strip())
            except (TypeError, ValueError):
                continue
            if num not in out:
                out.append(num)
    return out


def normalize_topics(chat: ChatInterest, topics) -> list[dict]:
    """把模型给的话题清洗成干净结构：编号去重、标签只留配置里的、标题去重、按热度排序。"""
    allowed = {_norm(tag): tag for tag in chat.tags_pos}
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for raw in topics or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        summary = str(raw.get("summary") or "").strip()
        msg_nos = _as_int_list(raw.get("msg_nos"))
        if not title or not summary or not msg_nos:
            continue
        tags: list[str] = []
        for tag in raw.get("tags") or []:
            key = _norm(tag)
            real = allowed.get(key)
            if real and real not in tags:
                tags.append(real)
        # 没打上任何正向标签的话题不算数（标签就是筛子）
        if chat.tags_pos and not tags:
            continue
        topic = {
            "title": title[:40],
            "tags": tags,
            "msg_nos": msg_nos,
            "summary": summary,
        }
        key = _norm(title)
        if key in seen:
            kept = seen[key]
            for num in msg_nos:
                if num not in kept["msg_nos"]:
                    kept["msg_nos"].append(num)
            if len(summary) > len(kept["summary"]):
                kept["summary"] = summary
            continue
        seen[key] = topic
        out.append(topic)
    out.sort(key=lambda t: len(t["msg_nos"]), reverse=True)
    if TOPIC_CAP:
        out = out[:TOPIC_CAP]
    return out


def merge_topics(chat: ChatInterest, groups: list[list[dict]]) -> list[dict]:
    """合并多个分片（或多次生成）的话题：标题相同或编号重叠的并成一个。"""
    merged: list[dict] = []
    for chunk in groups:
        for topic in normalize_topics(chat, chunk):
            target = None
            key = _norm(topic["title"])
            for kept in merged:
                if _norm(kept["title"]) == key:
                    target = kept
                    break
                overlap = set(kept["msg_nos"]) & set(topic["msg_nos"])
                if len(overlap) >= 2:
                    target = kept
                    break
            if target is None:
                merged.append(topic)
                continue
            for num in topic["msg_nos"]:
                if num not in target["msg_nos"]:
                    target["msg_nos"].append(num)
            for tag in topic["tags"]:
                if tag not in target["tags"]:
                    target["tags"].append(tag)
            if len(topic["summary"]) > len(target["summary"]):
                target["summary"] = topic["summary"]
    merged.sort(key=lambda t: len(t["msg_nos"]), reverse=True)
    if TOPIC_CAP:
        merged = merged[:TOPIC_CAP]
    return merged


def topic_time_range(times: list[int]) -> str:
    """按消息时间算出「16:23-16:34」；同一分钟只写一个时间。时间是消息自带的时间戳。"""
    from datetime import datetime

    stamps = sorted(int(t) for t in times if t)
    if not stamps:
        return ""
    first = datetime.fromtimestamp(stamps[0]).strftime("%H:%M")
    last = datetime.fromtimestamp(stamps[-1]).strftime("%H:%M")
    return first if first == last else f"{first}-{last}"


def topic_members(names: list[str], cap: int = MEMBER_CAP) -> str:
    """参与人：去空、去重、按出现顺序，超过 cap 个就写「等 N 人」。"""
    out: list[str] = []
    for name in names:
        clean = str(name or "").strip()
        if clean and clean not in out:
            out.append(clean)
    if not out:
        return ""
    if len(out) <= cap:
        return "、".join(out)
    return "、".join(out[:cap]) + f" 等 {len(out)} 人"


def split_names(text: str, names) -> list[dict]:
    """把正文里出现的群友昵称单独切成一段，便于排版时把它们排得浅一点。

    返回 [{"t": 片段, "n": 是不是昵称}, ...]。单字昵称不参与匹配（否则满屏都是高亮），
    同长度时按字母序稳定匹配，且优先匹配更长的昵称（「阿越咪」不会被「阿越」抢先吃掉）。
    """
    body = str(text or "")
    if not body:
        return []
    cands = sorted(
        {str(n).strip() for n in names or () if len(str(n).strip()) >= 2},
        key=lambda n: (-len(n), n),
    )
    if not cands:
        return [{"t": body, "n": False}]
    out: list[dict] = []
    plain: list[str] = []
    i = 0
    while i < len(body):
        hit = ""
        for name in cands:
            if body.startswith(name, i):
                hit = name
                break
        if hit:
            if plain:
                out.append({"t": "".join(plain), "n": False})
                plain = []
            out.append({"t": hit, "n": True})
            i += len(hit)
        else:
            plain.append(body[i])
            i += 1
    if plain:
        out.append({"t": "".join(plain), "n": False})
    return out

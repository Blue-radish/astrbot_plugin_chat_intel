"""报告存档：结构化 JSON、会话索引、读取与清理。

以前报告正文只活在渲染用的临时目录里（出图后 rmtree 删掉），所以「刚才那份报告第二条讲什么」
这种追问没有任何依据，模型只能重新生成一份或者含糊其辞。现在每份报告额外存一份 JSON
（id 与图片同名），并维护「这个会话最近收到哪些报告」的索引。

落盘的是稳定标识（消息 id，老数据用「发送者@秒级时间」），不是转录里的数组下标——
下标每次生成都会变，存下来就对不上号了。

报告里**不存原文**：需要原话就让模型用 query_group_records 去消息库里按引用取。
两边都只活 keep_days 天，所以引用在有效期内一定能查到；抄一份进来反而会出现
「截断的快照」被当成完整原话的风险。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 2          # 2：去掉原文快照，只留引用
INDEX_KEEP = 3              # 每个会话记住最近几份报告
DELIVERY_KEEP = 5           # 每个会话记住最近几次「成功投递」

# 投递记录只按条数封顶，**不加时间 TTL**：归档还留着、投递事实却先过期，
# 会让用户隔天问「上周那份」时模型无从判断「我到底发没发过」。条数已经够控体积。


def report_id_from_image(image_name: str) -> str:
    return Path(str(image_name)).stem


def _folder(report_dir) -> Path | None:
    """空的报告目录当「没配」处理：Path("") 是当前目录，写进去就成垃圾了。"""
    text = str(report_dir or "").strip()
    return Path(text) if text else None


def path_for(report_dir, report_id: str) -> Path | None:
    folder = _folder(report_dir)
    if folder is None or not report_id:
        return None
    return folder / f"{report_id}.json"


def ref_of(rec: dict) -> str:
    """一条消息的稳定引用：优先平台消息 id，老数据用「发送者@秒级时间」回落。

    真实消息库里有一大半旧记录没有 mid（不同平台/不同版本采集的），
    只记 mid 的话这些消息就永远查不回来了。
    """
    mid = str(rec.get("mid") or "")
    if mid:
        return mid
    uid = str(rec.get("uid") or "?")
    ts = int(rec.get("t") or 0)
    return f"{uid}@{ts}" if ts else ""


def _fmt(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def build_record(
    *,
    report_id: str,
    gid: str,
    group: str,
    platform: str,
    label: str,
    since,
    until,
    topics: list[dict],
    records: list[dict],
    image_name: str = "",
    images: list[dict] | None = None,
    headline: str = "",
    incomplete: bool = False,
    failed_chunks: list[int] | None = None,
    chunk_total: int = 1,
    truncated: bool = False,
    requested_since=None,
    incremental: bool = False,
) -> dict:
    """把一次生成的业务数据整理成可长期读取的报告记录。"""
    out_topics: list[dict] = []
    for index, topic in enumerate(topics or [], 1):
        nums = [n for n in (topic.get("msg_nos") or []) if 0 <= n < len(records)]
        picked = [records[n] for n in nums]
        refs: list[str] = []
        for rec in picked:
            ref = ref_of(rec)
            if ref and ref not in refs:
                refs.append(ref)
        out_topics.append(
            {
                "no": index,
                "title": str(topic.get("title") or ""),
                "tags": list(topic.get("tags") or []),
                "summary": str(topic.get("summary") or ""),
                "time": str(topic.get("time") or ""),
                "members": str(topic.get("members") or ""),
                "count": int(topic.get("count") or len(picked)),
                "refs": refs,
            }
        )

    first = int(records[0].get("t") or 0) if records else 0
    last = int(records[-1].get("t") or 0) if records else 0
    now = int(time.time())
    return {
        "schema": SCHEMA_VERSION,
        "id": report_id,
        "gid": str(gid or ""),
        "group": str(group or ""),
        "platform": str(platform or ""),
        "range": {
            "label": str(label or ""),
            # since 是**实际**用来取数的起点（增量时可能被游标往前推），请求值另存
            "since": int(since or 0),
            "until": int(until or 0),
            "requested_since": int(requested_since if requested_since is not None else (since or 0)),
            "incremental": bool(incremental),
        },
        "covered": {
            "first": first,
            "last": last,
            "first_time": _fmt(first),
            "last_time": _fmt(last),
        },
        "generated_at": now,
        "generated_time": _fmt(now),
        "status": "incomplete" if incomplete else "complete",
        "chunks": {"total": int(chunk_total or 1), "failed": list(failed_chunks or [])},
        "truncated": bool(truncated),
        "msg_count": len(records),
        "headline": str(headline or ""),
        "image": str(image_name or ""),
        "images": list(images or []),
        "topics": out_topics,
    }


def save(report_dir, record: dict) -> Path | None:
    """原子写：先写临时文件再替换，别把已经存好的报告写坏。"""
    try:
        folder = _folder(report_dir)
        if folder is None:
            return None
        target = path_for(folder, str(record.get("id") or ""))
        if target is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return target
    except Exception:
        return None


def load(report_dir, report_id: str) -> dict | None:
    path = path_for(report_dir, report_id)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def image_of(report_dir, report_id: str) -> str:
    """这张报告的图片还在不在（返回文件名，不在就返回空串）。"""
    folder = _folder(report_dir)
    if folder is None:
        return ""
    for suffix in (".png", ".jpg", ".jpeg"):
        candidate = folder / f"{report_id}{suffix}"
        if candidate.is_file():
            return candidate.name
    return ""


def newest(report_dir, gid: str | None = None, limit: int = 1) -> list[str]:
    """按生成时间倒序列出报告 id；给了 gid 就只看那个群的。"""
    folder = _folder(report_dir)
    if folder is None or not folder.is_dir():
        return []
    found: list[tuple[float, str]] = []
    for path in folder.glob("*.json"):
        rid = path.stem
        if gid and f"_{gid}_" not in rid:
            continue
        try:
            found.append((path.stat().st_mtime, rid))
        except Exception:
            continue
    found.sort(reverse=True)
    return [rid for _mtime, rid in found[:limit]]


def prune(report_dir, keep_days: int, protect: set[str] | None = None) -> tuple[int, int]:
    """删掉过期报告（JSON 与图片）。protect 里的 id 永远保留（每组最新一份）。

    报告和原文同寿：原文清了、报告还留着的话，「他原话怎么说的」就成了空话，
    不如一起过期，让工具能明确回答「证据已过期」。
    """
    folder = _folder(report_dir)
    if folder is None or not folder.is_dir() or keep_days <= 0:
        return (0, 0)
    keep = set(protect or ())
    cutoff = (datetime.now() - timedelta(days=keep_days)).timestamp()
    removed_json = removed_image = 0
    for path in list(folder.glob("*.json")) + list(folder.glob("*.png")):
        if path.stem in keep:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except Exception:
            continue
        if path.suffix == ".json":
            removed_json += 1
        else:
            removed_image += 1
    return removed_json, removed_image


# ---------------------------------------------------------------- 会话索引


def _conv(state: dict, umo: str) -> dict:
    """取（必要时创建）会话节点。**只给写路径用**。"""
    conversations = state.setdefault("conversations", {})
    return conversations.setdefault(umo or "", {})


def _conv_ro(state: dict, umo: str) -> dict:
    """只读取会话节点，不创建任何键。

    渲染提示词走的是只读路径：以前 `_conv()` 用 `setdefault`，于是「看一眼快照」
    就会在空 state 上凭空建出 `conversations[umo]`，把纯渲染变成写操作。
    """
    return ((state or {}).get("conversations") or {}).get(umo or "") or {}


def entries(state: dict, umo: str) -> list[dict]:
    return list(_conv_ro(state, umo).get("reports") or [])


def _store(state: dict, umo: str, items: list[dict]) -> None:
    """「只留最近三份」只约束**已生成**的报告。

    还在跑的占位不算在名额里：一次总结四个群，四个占位要是被截掉一个，
    「还有哪份没跑完」就会漏报。顺序保持原样，别让占位全跑到前面去。
    """
    done = [entry for entry in items if _phase(entry) != "generating"]
    keep = {id(entry) for entry in done[-INDEX_KEEP:]}
    _conv(state, umo)["reports"] = [
        entry for entry in items if _phase(entry) == "generating" or id(entry) in keep
    ]


def _phase(entry: dict) -> str:
    """任务执行状态：generating / ready / failed / expired。

    老数据只有 status 一个字段，这里兼容读；不需要写迁移函数。
    """
    phase = str(entry.get("phase") or "")
    if phase:
        return phase
    legacy = str(entry.get("status") or "")
    if legacy in ("generating", "failed", "expired"):
        return legacy
    return "ready"


def _facts(entry: dict) -> tuple[bool, bool]:
    """读出（正文存下来了没, 发出去了没）。

    新数据直接读字段；老数据只有 status，从它反推——升级后第一次读到的就是老条目，
    反推错了等于升级把人骗一次。
    """
    if "stored" in entry or "delivered" in entry:
        return bool(entry.get("stored", True)), bool(entry.get("delivered"))
    legacy = str(entry.get("status") or "")
    if legacy == "unreadable":
        return False, True
    if legacy == "delivered":
        return True, True
    return True, False


def status_of(entry: dict) -> str:
    """把几件互不冲突的事实合成一个可读状态（派生出来的，不落盘）。

    以前只有一个 status 字段兼职四件事，于是「正文存档失败」会被「图片发送成功」
    直接覆盖——图发出去之后索引还宣称这份能讨论，其实正文根本读不回来。
    现在分开记：phase（跑没跑完）、stored（正文存没存下）、delivered（发没发出去）。
    """
    phase = _phase(entry)
    if phase != "ready":
        return phase
    stored, delivered = _facts(entry)
    if not stored:
        return "unreadable"
    return "delivered" if delivered else "generated"


def mark_generating(
    state: dict, umo: str, *, gid: str, group: str, label: str, now=None, token: float = 0.0
) -> dict:
    """「我让你跑的那份」：先把占位记下，跑完再换成真报告。

    token 是这次提交的标识。同群同标签的两份请求是两件事（一份在跑、一份排队），
    不能互相顶掉——以前按「同群同标签」去重，第二次登记会把第一次的占位删掉，
    于是第一份跑完后排队那份在索引里看不见了。
    """
    entry = {
        "id": "",
        "gid": str(gid or ""),
        "group": str(group or ""),
        "label": str(label or ""),
        "at": float(token or 0.0),
        "ts": int(now or time.time()),
        "phase": "generating",
        "topics": [],
    }
    items = [
        old
        for old in entries(state, umo)
        if not (
            _phase(old) == "generating"
            and old.get("gid") == entry["gid"]
            and old.get("label") == entry["label"]
            and old.get("at") == entry["at"]
        )
    ]
    items.append(entry)
    _store(state, umo, items)
    return entry


def resolve(state: dict, umo: str, record: dict, stored: bool = True) -> dict:
    """生成好了：用真报告替换掉同群同范围的占位（没有占位就追加）。

    stored=False 表示正文 JSON 没写成功：图也许还发得出去，但读不回来。
    这里只记事实，不判「状态」——发送成功也不会抹掉这个事实。
    """
    entry = {
        "id": str(record.get("id") or ""),
        "gid": str(record.get("gid") or ""),
        "group": str(record.get("group") or ""),
        "label": str((record.get("range") or {}).get("label") or ""),
        "ts": int(record.get("generated_at") or time.time()),
        "phase": "ready",
        "stored": bool(stored),
        "delivered": False,
        "send_error": "",
        "fail_reason": "",
        "topics": [str(topic.get("title") or "") for topic in record.get("topics") or []],
        "image": str(record.get("image") or ""),
        "incomplete": record.get("status") == "incomplete",
    }
    items = entries(state, umo)
    # 从最早的那条占位认领：队列是先进先出，刚跑完的是最先提交的那份
    for index, old in enumerate(items):
        if (
            _phase(old) == "generating"
            and old.get("gid") == entry["gid"]
            and old.get("label") == entry["label"]
        ):
            items[index] = entry
            _store(state, umo, items)
            return entry
    items.append(entry)
    _store(state, umo, items)
    return entry


def mark_delivered(state: dict, umo: str, report_id: str) -> bool:
    """图片真的发出去了。只动投递位，不碰正文是否可读。

    写之前先过 `_facts()`：老数据只有 status，直接写 delivered 会把「正文不可读」
    这个事实覆盖掉，于是图发出去之后索引又宣称这份能讨论。
    """
    items = entries(state, umo)
    hit = False
    for entry in items:
        if entry.get("id") == report_id:
            stored, _delivered = _facts(entry)
            entry["stored"] = bool(stored)
            entry["delivered"] = True
            entry["send_error"] = ""
            hit = True
    if hit:
        _store(state, umo, items)
    return hit


def mark_send_failed(state: dict, umo: str, report_id: str, reason: str = "") -> bool:
    """没发出去：投递位保持 False，原因单独记，不污染正文可读性。"""
    items = entries(state, umo)
    hit = False
    for entry in items:
        if entry.get("id") == report_id:
            # 之前发成功过就保持「已投递」：用户手里确实有这份，重发失败不该改历史。
            # 老数据的 delivered 藏在 status 里，得先让 _facts() 还原出来再写。
            stored, delivered = _facts(entry)
            entry["stored"] = bool(stored)
            entry["delivered"] = bool(delivered)
            entry["send_error"] = str(reason or "发送失败")
            hit = True
    if hit:
        _store(state, umo, items)
    return hit


def settle(state: dict, umo: str, *, gid: str = "", label: str = "", reason: str = "") -> int:
    """把还在「生成中」的占位收尾，返回收尾了几条。

    成功由 `resolve()` 换掉，其余结局（一条都没命中、抛异常、被取消、卸载）都走这里。
    以前只有一个 `mark_failed`，而且找到第一条就 break：整批（全部群）出错时，
    剩下的目标会永远停在「正在生成」，用户问「跑完了吗」永远被告知还在跑。
    所以这里不加 break，gid 为空就表示这个会话里在跑的全算。
    """
    items = entries(state, umo)
    hit = 0
    for entry in items:
        if _phase(entry) != "generating":
            continue
        if gid and entry.get("gid") not in ("", gid):
            continue
        if label and entry.get("label") != label:
            continue
        entry["phase"] = "failed"
        entry["fail_reason"] = str(reason or "")
        hit += 1
    if hit:
        _store(state, umo, items)
    return hit


def settle_all(state: dict, *, reason: str = "") -> int:
    """所有会话里还在「生成中」的占位一起收尾（卸载 / 重载时用）。"""
    conversations = state.get("conversations")
    if not isinstance(conversations, dict):
        return 0
    return sum(settle(state, umo, reason=reason) for umo in list(conversations.keys()))


def mark_failed(state: dict, umo: str, *, gid: str = "", label: str = "", reason: str = "") -> None:
    """兼容旧调用：等同 `settle()`。"""
    settle(state, umo, gid=gid, label=label, reason=reason)


def set_focus(state: dict, umo: str, report_id: str, topic_no: int = 0) -> None:
    """记下「现在在讨论哪份报告的哪个话题」，让下一轮省略 ID 时接得上。"""
    if not umo or not report_id:
        return
    _conv(state, umo)["focus"] = {
        "report_id": str(report_id),
        "topic_no": int(topic_no or 0),
        "ts": int(time.time()),
    }


def get_focus(state: dict, umo: str) -> dict:
    focus = _conv_ro(state, umo).get("focus")
    return focus if isinstance(focus, dict) else {}


def drop_expired(state: dict, report_dir) -> int:
    """文件被清理掉的索引项直接丢掉：留着只会指向一片空白。

    以前是给它盖一个 status="expired"，于是「过期」又得挤进那个状态字段。
    文件在不在，读的时候就能判断，不必落盘。
    """
    removed = 0
    for _umo, conv in (state.get("conversations") or {}).items():
        if not isinstance(conv, dict):
            continue
        kept: list[dict] = []
        for entry in conv.get("reports") or []:
            report_id = str(entry.get("id") or "")
            if not report_id or _phase(entry) == "generating":
                kept.append(entry)
                continue
            path = path_for(report_dir, report_id)
            if path is not None and path.is_file():
                kept.append(entry)
            else:
                removed += 1
        if len(kept) != len(conv.get("reports") or []):
            conv["reports"] = kept
        focus = conv.get("focus")
        if isinstance(focus, dict) and focus.get("report_id"):
            focus_path = path_for(report_dir, str(focus["report_id"]))
            if focus_path is None or not focus_path.is_file():
                conv.pop("focus", None)
    return removed


def blurb(state: dict, umo: str, limit: int = INDEX_KEEP) -> str:
    """注入提示词的一小段：只说标题和定位信息，正文按需用工具读。"""
    items = [e for e in entries(state, umo) if status_of(e) != "expired"]
    focus_line = ""
    focus = get_focus(state, umo)
    if focus.get("report_id"):
        focus_line = f"[正在讨论] 报告 {focus['report_id']}"
        if focus.get("topic_no"):
            focus_line += f" 第 {focus['topic_no']} 个话题"
        focus_line += "；用户说「这条」「第二条」就是它，省略 ID 也先读它。\n"
    if not items:
        return focus_line.rstrip()
    lines: list[str] = []
    for index, entry in enumerate(items[-limit:], 1):
        group = entry.get("group") or (f"群 {entry['gid']}" if entry.get("gid") else "（所有群）")
        label = entry.get("label") or ""
        status = status_of(entry)
        if status == "generating":
            lines.append(f"{index}) 正在生成：{group}｜{label or '范围未定'}")
            continue
        if status == "failed":
            why = entry.get("fail_reason") or ""
            lines.append(f"{index}) 生成失败：{group}｜{label}" + (f"（{why}）" if why else ""))
            continue
        if status == "unreadable":
            lines.append(f"{index}) 有图但正文没存下来（读不了，只能重发图）：{group}｜{label}")
            continue
        topics = [t for t in (entry.get("topics") or []) if t]
        titles = "、".join(topics[:4])
        lines.append(
            f"{index}) 报告 ID={entry.get('id')}｜{group}｜{label}｜"
            f"{len(topics)} 个话题（{titles}）"
        )
    text = "[已有报告] " + "；".join(lines)
    return (focus_line + text) if focus_line else text


# ---------------------------------------------------------------- 投递记录
#
# 「有哪些报告」和「我发过哪几份」是两件事：
# - 索引（`reports`）按 INDEX_KEEP 裁，服务于「找报告」；
# - 投递记录（`deliveries`）按 DELIVERY_KEEP 裁，服务于「我刚才发了什么」。
#
# 分开的理由很实在：四群一次生成时，第一份生成完就已经被挤出最近三份，
# `mark_delivered()` 找不到它，可图片是真发出去了。投递事实若挂在索引上就会丢。
# 而模型一旦没有「我一分钟前发过这份」的依据，就会把用户的夸奖当成新请求重跑一遍。


def deliveries(state: dict, umo: str) -> list[dict]:
    """本会话的成功投递记录（只读，不创建会话键）。"""
    return [d for d in (_conv_ro(state, umo).get("deliveries") or []) if isinstance(d, dict)]


def append_delivery(
    state: dict,
    umo: str,
    *,
    report_id: str,
    group: str = "",
    range_label: str = "",
    topic_count: int | None = None,
    topic_titles=None,
    stored: bool | None = None,
    delivered_at: float | None = None,
) -> dict:
    """记一次「图真的发出去了」。

    `stored` 是**投递当时**的存档结果，三态：True 存下了 / False 没存下 / None 不知道。
    别把 None 写成 False：那等于把「我没查到」当成「当时失败了」，是凭空造事实。
    `topic_count` 同理，不知道就留 None，不要写 0。
    """
    log = _conv(state, umo).setdefault("deliveries", [])
    if not isinstance(log, list):
        log = []
        _conv(state, umo)["deliveries"] = log
    seq = (int(log[-1].get("seq") or 0) + 1) if log and isinstance(log[-1], dict) else 1
    record = {
        "seq": seq,
        "report_id": str(report_id or ""),
        "delivered_at": int(delivered_at if delivered_at is not None else time.time()),
        "group": str(group or ""),
        "range_label": str(range_label or ""),
        "topic_count": int(topic_count) if isinstance(topic_count, int) else None,
        "topic_titles": [str(t) for t in (topic_titles or []) if t],
        "stored": stored if isinstance(stored, bool) else None,
    }
    log.append(record)
    if len(log) > DELIVERY_KEEP:
        del log[:-DELIVERY_KEEP]
    return record


def stored_from_index(state: dict, umo: str, report_id: str) -> bool | None:
    """索引里还留着这份时，补出「当时存档结果」；只剩老数据就返回 None。

    不做 `_facts()` 的默认值推断：老条目没有 `stored` 字段时，`_facts()` 会默认 True，
    拿它当真就是把「不知道」说成「存档成功」。
    """
    if not report_id:
        return None
    for entry in entries(state, umo):
        if str(entry.get("id") or "") != str(report_id):
            continue
        if "stored" in entry:
            return bool(entry.get("stored"))
        return None
    return None


def _fmt_delivered_at(ts) -> str:
    """本地时间 + 时区标识；拿不到时间就明说不知道，不拿别的字段顶替。"""
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return "时间未知"
    if value <= 0:
        return "时间未知"
    moment = datetime.fromtimestamp(value)
    offset = moment.astimezone().utcoffset() or timedelta(0)
    hours = int(offset.total_seconds() // 3600)
    sign = "+" if hours >= 0 else "-"
    return f"{moment:%Y-%m-%d %H:%M}（UTC{sign}{abs(hours)}）"


def _body_state(state: dict, umo: str, record: dict, report_dir) -> str:
    """正文现在能不能读：文件在不在是**当前**事实，`stored` 是**当时**的记录，分开说。

    `path.exists()` 不够（可能是目录），`is_file()` 只是「可尝试读取」，
    真正能不能解析由 `load()` 说了算——所以这里的措辞只到「文件存在，可调用工具读取」。
    """
    report_id = str(record.get("report_id") or "")
    path = path_for(report_dir, report_id)
    if path is not None and path.is_file():
        return "正文文件存在，可调用 get_group_report 读取"
    stored = record.get("stored")
    if not isinstance(stored, bool):
        stored = stored_from_index(state, umo, report_id)
    if stored is True:
        return "当时存档成功，当前正文文件缺失"
    if stored is False:
        return "当时存档失败，当前正文文件缺失"
    return "当前正文文件缺失，当时存档状态未知"


def delivery_blurb(
    state: dict, umo: str, report_dir, limit: int = DELIVERY_KEEP
) -> str:
    """注入提示词的一段事实快照：这个会话最近**真的收到过**哪些报告图、什么时候发的。

    和 `blurb()` 分工：`blurb()` 说「有哪些报告、正在讨论哪份」，这里说「我发过哪几份」。
    两者会同时出现，所以措辞上必须能分开——否则同一个「这条」会有两个指向。

    纯只读：不建会话键、不改 state，模型每轮请求都会调它。

    **倒序展示**：列表按追加顺序存（老的在前），但提示词里「刚发来的那份」指的是最近一条，
    所以第 1 条必须是新的。以前照原序渲染，提示词让模型读第 1 条，读到的却是最旧那份。
    """
    items = list(reversed(deliveries(state, umo)[-limit:]))
    if not items:
        return ""
    lines = [
        "[本会话最近的报告投递]",
        "以下是**你自己**通过群聊情报插件发到本会话的报告图（不是用户发的），"
        "只说明「已发送到本会话」，不代表用户已读。",
    ]
    for index, record in enumerate(items, 1):
        report_id = str(record.get("report_id") or "")
        group = str(record.get("group") or "") or "（群名未知）"
        label = str(record.get("range_label") or "") or "（范围未知）"
        count = record.get("topic_count")
        titles = [str(t) for t in (record.get("topic_titles") or []) if t]
        if isinstance(count, int):
            topic_text = f"共 {count} 个话题"
            if titles:
                topic_text += f"（{'、'.join(titles[:4])}）"
        else:
            topic_text = "话题数未知"
        lines.append(
            f"{index}) {_fmt_delivered_at(record.get('delivered_at'))}｜{group}｜{label}｜"
            f"报告 ID={report_id or '（未知）'}｜{topic_text}｜"
            f"{_body_state(state, umo, record, report_dir)}"
        )
    return "\n".join(lines)

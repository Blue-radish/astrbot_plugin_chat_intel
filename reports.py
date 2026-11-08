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
    conversations = state.setdefault("conversations", {})
    return conversations.setdefault(umo or "", {})


def entries(state: dict, umo: str) -> list[dict]:
    return list(_conv(state, umo).get("reports") or [])


def _store(state: dict, umo: str, items: list[dict]) -> None:
    _conv(state, umo)["reports"] = items[-INDEX_KEEP:]


def mark_generating(state: dict, umo: str, *, gid: str, group: str, label: str, now=None) -> dict:
    """「我让你跑的那份」：先把占位记下，跑完再换成真报告。"""
    entry = {
        "id": "",
        "gid": str(gid or ""),
        "group": str(group or ""),
        "label": str(label or ""),
        "ts": int(now or time.time()),
        "status": "generating",
        "topics": [],
    }
    items = [
        old
        for old in entries(state, umo)
        if not (
            old.get("status") == "generating"
            and old.get("gid") == entry["gid"]
            and old.get("label") == entry["label"]
        )
    ]
    items.append(entry)
    _store(state, umo, items)
    return entry


def resolve(
    state: dict, umo: str, record: dict, status: str = "generated", stored: bool = True
) -> dict:
    """生成好了：用真报告替换掉同群同范围的占位（没有占位就追加）。

    stored=False 表示正文 JSON 没写成功：图可能发出去了，但读不回来，
    状态必须如实标成 unreadable，别让人以为还能讨论。
    """
    entry = {
        "id": str(record.get("id") or ""),
        "gid": str(record.get("gid") or ""),
        "group": str(record.get("group") or ""),
        "label": str((record.get("range") or {}).get("label") or ""),
        "ts": int(record.get("generated_at") or time.time()),
        "status": "unreadable" if not stored else status,
        "reason": "" if stored else "正文存档失败，只能重发图、读不了内容",
        "topics": [str(topic.get("title") or "") for topic in record.get("topics") or []],
        "image": str(record.get("image") or ""),
        "incomplete": record.get("status") == "incomplete",
    }
    items = entries(state, umo)
    for index in range(len(items) - 1, -1, -1):
        old = items[index]
        if (
            old.get("status") == "generating"
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
    items = entries(state, umo)
    hit = False
    for entry in items:
        if entry.get("id") == report_id:
            entry["status"] = "delivered"
            hit = True
    if hit:
        _store(state, umo, items)
    return hit


def mark_send_failed(state: dict, umo: str, report_id: str, reason: str = "") -> bool:
    """生成好了但没发出去：保留 generated，别谎报 delivered。"""
    items = entries(state, umo)
    hit = False
    for entry in items:
        if entry.get("id") == report_id:
            if entry.get("status") in ("generated", "delivered"):
                entry["status"] = "generated"
                entry["reason"] = reason or "发送失败"
            hit = True
    if hit:
        _store(state, umo, items)
    return hit


def mark_failed(state: dict, umo: str, *, gid: str = "", label: str = "", reason: str = "") -> None:
    items = entries(state, umo)
    for index in range(len(items) - 1, -1, -1):
        entry = items[index]
        if entry.get("status") != "generating":
            continue
        if gid and entry.get("gid") not in ("", gid):
            continue
        if label and entry.get("label") != label:
            continue
        entry["status"] = "failed"
        entry["reason"] = reason
        break
    _store(state, umo, items)


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
    focus = _conv(state, umo).get("focus")
    return focus if isinstance(focus, dict) else {}


def drop_expired(state: dict, report_dir) -> None:
    """文件被清理掉的索引条目标成 expired，别让模型以为还读得到。"""
    for umo, conv in (state.get("conversations") or {}).items():
        if not isinstance(conv, dict):
            continue
        for entry in conv.get("reports") or []:
            report_id = str(entry.get("id") or "")
            if not report_id or entry.get("status") == "generating":
                continue
            path = path_for(report_dir, report_id)
            if path is None or not path.is_file():
                entry["status"] = "expired"
                entry["id"] = report_id  # 留着 id，回答时能说清是哪份过期了
        focus = conv.get("focus")
        if isinstance(focus, dict) and focus.get("report_id"):
            focus_path = path_for(report_dir, str(focus["report_id"]))
            if focus_path is None or not focus_path.is_file():
                conv.pop("focus", None)


def blurb(state: dict, umo: str, limit: int = INDEX_KEEP) -> str:
    """注入提示词的一小段：只说标题和定位信息，正文按需用工具读。"""
    items = [e for e in entries(state, umo) if e.get("status") != "expired"]
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
        status = entry.get("status") or ""
        if status == "generating":
            lines.append(f"{index}) 正在生成：{group}｜{label or '范围未定'}")
            continue
        if status == "failed":
            lines.append(f"{index}) 生成失败：{group}｜{label}")
            continue
        if status == "unreadable":
            lines.append(f"{index}) 有图但正文没存下来（读不了）：{group}｜{label}")
            continue
        topics = [t for t in (entry.get("topics") or []) if t]
        titles = "、".join(topics[:4])
        lines.append(
            f"{index}) 报告 ID={entry.get('id')}｜{group}｜{label}｜"
            f"{len(topics)} 个话题（{titles}）"
        )
    text = "[已有报告] " + "；".join(lines)
    return (focus_line + text) if focus_line else text

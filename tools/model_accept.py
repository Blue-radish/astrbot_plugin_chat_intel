"""真实模型验收：自然语言 → 工具调用 → 后端执行，逐条对标注。

用法：
    python tools/model_accept.py           # 全套标注样本，各跑一次
    python tools/model_accept.py 3         # 只跑身份敏感样本，重复 3 次看稳定性

依赖本机 AstrBot 配置里启用的 chat completion provider（默认 wb2-deepseek-v41-flash）。
system prompt 由插件自己的 `on_llm_request` 现场生成（含投递快照与焦点），
工具定义从插件真实签名 + docstring 生成（与 AstrBot 的 `add_func` 同构：没有 required）。

判定规则（上一版放松过，这里收紧）：
  * 检查**这一轮全部的 tool_calls**，不只看第一个；
  * 每个样本写明期望：工具名 + 关键参数（群、时间、again、report_id、topic_no）；
  * 参数真的喂给后端执行，再核对执行结果里的身份（已读报告 X / 受理结果 / 投递的文件）；
  * 第二轮（后端拒绝并给出候选后）同样核对工具名、参数和执行结果，
    不再「补了个 ID 就算过」。
"""

import asyncio
import collections
import importlib.util
import inspect
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

PLUGIN = pathlib.Path(__file__).resolve().parent.parent
ASTRBOT_HOME = pathlib.Path(os.environ.get("ASTRBOT_HOME", r"C:\Users\Ray\astrbot"))
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(ASTRBOT_HOME))

import httpx  # noqa: E402

import reports  # noqa: E402

CFG = json.loads((ASTRBOT_HOME / "data" / "cmd_config.json").read_text(encoding="utf-8-sig"))
SOURCES = {s["id"]: s for s in CFG["provider_sources"]}
PROV = next(p for p in CFG["provider"] if p["id"] == "wb2-deepseek-v41-flash")
_SOURCE = SOURCES[PROV["provider_source_id"]]
API_KEY = _SOURCE["key"][0] if isinstance(_SOURCE["key"], list) else _SOURCE["key"]
API_BASE = _SOURCE["api_base"].rstrip("/")
MODEL = PROV["model"]

UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"
GID_A, GID_B = "220001", "220002"
NAME_A, NAME_B = "电脑互助群", "MeowShelter"
A_ID = "chatA_yesterday_pc"
B_ID = "chatB_today_meow"

TYPE_MAP = {
    "string": "string",
    "str": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
}


# ------------------------------------------------------------------ 插件加载


def load_plugin():
    spec = importlib.util.spec_from_file_location("chat_intel_main", PLUGIN / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chat_intel_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_docstring(doc):
    doc = doc or ""
    head, _, tail = doc.partition("Args:")
    desc = " ".join(line.strip() for line in head.strip().splitlines())
    args = []
    for line in tail.splitlines():
        m = re.match(r"\s*(\w+)\s*\((\w+)\)\s*:\s*(.*)", line)
        if not m:
            continue
        name, jtype, text = m.groups()
        args.append(
            {"type": TYPE_MAP.get(jtype, "string"), "name": name, "description": text.strip()}
        )
    return desc, args


def tool_names():
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    pairs = re.findall(r'@filter\.llm_tool\(name="([\w]+)"\)\s*\n\s*async def (tool_\w+)', src)
    return {method: name for name, method in pairs}


def tool_schemas(mod):
    out = []
    for method, name in tool_names().items():
        desc, args = parse_docstring(inspect.getdoc(getattr(mod.TechDigestPlugin, method)))
        props = {a["name"]: {"type": a["type"], "description": a["description"]} for a in args}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {"type": "object", "properties": props},
                },
            }
        )
    out.sort(key=lambda t: t["function"]["name"])
    return out


# ------------------------------------------------------------------ 假会话


class Ev:
    def __init__(self, text=""):
        self.message_str = text
        self.unified_msg_origin = UMO

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return OWNER

    def get_platform_id(self):
        return "bot_a"

    def set_extra(self, key, value):
        pass

    def get_extra(self, key, default=None):
        return default


class FakeContext:
    """只承接发送，不联网。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append(chain)
        return True


def build_plugin(mod, root):
    p = object.__new__(mod.TechDigestPlugin)
    p.context = FakeContext()
    p.state = {}
    p.reports_dir = root
    p.owner_uids = {OWNER}
    p.group_ids = [GID_A, GID_B]
    p.group_name_map = {GID_A: NAME_A, GID_B: NAME_B}
    p.chats = []
    p.silence_platforms = []
    p.keep_days = 7
    p.smart_parse = False
    p.store_dir = root / "messages"
    p._running = False
    p._queue = collections.deque(maxlen=2)
    p._tasks = set()
    p._current_req = None
    p._current_task = None
    p._llm_blocked = 0
    p._result_blocked = 0
    p._silenced = 0
    p._diag_count = 0
    p.push_umo = ""
    p._push_cfg_error = ""
    p._save_state = lambda: None
    p._status_lines = lambda: asyncio.sleep(0, result=[f"{NAME_A}（{GID_A}）：今日 12 条"])
    # 生成请求不进真跑：只记录插件解析出来的请求，参数对错由此判定
    p.submitted = []
    p.delivered = []

    async def fake_submit(req):
        p.submitted.append(req)
        return "accepted", p._submit_ack(req, True)

    async def fake_deliver(umo, path, **_kw):
        p.delivered.append(pathlib.Path(path).name)
        return True

    async def fake_send(umo, text):
        return True

    p._submit_digest = fake_submit
    p._deliver_report = fake_deliver
    p._send_text = fake_send
    return p


# ------------------------------------------------------------------ 数据


def write_report(root, rid, gid, group, label, overview, topics, generated_at):
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "id": rid,
        "gid": gid,
        "group": group,
        "label": label,
        "generated_at": generated_at,
        "overview": overview,
        "topics": [
            {"no": i + 1, "title": t, "summary": s, "refs": []} for i, (t, s) in enumerate(topics)
        ],
    }
    reports.save(root, record)
    (root / f"{rid}.png").write_bytes(b"png")
    return record


def dataset(mod, name, root):
    """三套数据：基础 / 同一群两份 / 焦点恰好是最近投递。"""
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    p = build_plugin(mod, root)
    if name == "base":
        write_report(
            root, A_ID, GID_A, NAME_A, "昨天", "刷机翻车成了主线",
            [("板子烧了", "老张的板子烧了"), ("换一片", "老王建议换一片")], 1_800_000_000,
        )
        write_report(
            root, B_ID, GID_B, NAME_B, "今天", "猫舍今天聊了喂食机",
            [("喂食机卡住", "喂食机卡住了"), ("改手动", "改回手动喂")], 1_800_100_000,
        )
        reports.set_focus(p.state, UMO, A_ID, 1)
        reports.append_delivery(
            p.state, UMO, report_id=B_ID, group=NAME_B, range_label="今天", stored=True
        )
        return p
    if name == "same_group":
        # 同一个群两份报告：只给群名绝不能替模型选一份
        write_report(
            root, "pc_old", GID_A, NAME_A, "昨天", "昨天的旧账",
            [("板子烧了", "老张的板子烧了"), ("换一片", "老王建议换一片")], 1_800_000_000,
        )
        write_report(
            root, "pc_new", GID_A, NAME_A, "今天", "今天的新账",
            [("又烧了", "小李也烧了"), ("买保险", "有没有保修")], 1_800_100_000,
        )
        reports.set_focus(p.state, UMO, "pc_old", 1)
        reports.append_delivery(
            p.state, UMO, report_id="pc_new", group=NAME_A, range_label="今天", stored=True
        )
        return p
    if name == "focus_is_newest":
        write_report(
            root, "pc_old", GID_A, NAME_A, "昨天", "昨天的旧账",
            [("板子烧了", "老张的板子烧了"), ("换一片", "老王建议换一片")], 1_800_000_000,
        )
        write_report(
            root, "pc_new", GID_A, NAME_A, "今天", "今天的新账",
            [("又烧了", "小李也烧了"), ("买保险", "有没有保修")], 1_800_100_000,
        )
        reports.append_delivery(
            p.state, UMO, report_id="pc_old", group=NAME_A, range_label="昨天", stored=True
        )
        reports.append_delivery(
            p.state, UMO, report_id="pc_new", group=NAME_A, range_label="今天", stored=True
        )
        reports.set_focus(p.state, UMO, "pc_new", 1)  # 刚读过最近投递那份
        return p
    raise SystemExit(f"未知数据集 {name}")


# ------------------------------------------------------------------ 模型


def build_prompt(p):
    from astrbot.api.provider import ProviderRequest

    req = ProviderRequest()
    req.system_prompt = "你是助手"
    req.contexts = []
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(p.silence_llm(Ev(), req))
    finally:
        loop.close()
    return req.system_prompt


def post(messages, tools):
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0,
    }
    with httpx.Client(timeout=180) as c:
        r = c.post(
            f"{API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    msg = data["choices"][0]["message"]
    calls = []
    for call in msg.get("tool_calls") or []:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"__unparsed__": call["function"].get("arguments")}
        calls.append(
            {"id": call.get("id"), "name": call["function"]["name"], "args": args, "raw": call}
        )
    return calls, (msg.get("content") or "").strip(), msg


# ------------------------------------------------------------------ 后端执行


def run_tool(p, call, text):
    """把模型给的参数真的交给插件执行，返回 (结果文本, 执行痕迹)。"""
    name, args = call["name"], call["args"]
    trace = {}
    if name == "get_group_report":
        out = asyncio.run(
            p.tool_get_group_report(
                Ev(text),
                report_id=str(args.get("report_id") or ""),
                topic_no=int(args.get("topic_no") or 0),
                gid=str(args.get("gid") or ""),
            )
        )
    elif name == "resend_report":
        out = asyncio.run(
            p.tool_resend_report(Ev(text), report_id=str(args.get("report_id") or ""))
        )
        trace["delivered"] = list(p.delivered)
        p.delivered.clear()
    elif name == "digest_status":
        out = asyncio.run(p.tool_digest_status(Ev(text)))
    elif name == "cancel_digest":
        out = asyncio.run(p.tool_cancel_digest(Ev(text)))
    elif name == "group_digest":
        out = asyncio.run(
            p.tool_group_digest(
                Ev(text),
                when=str(args.get("when") or ""),
                group=str(args.get("group") or ""),
                again=bool(args.get("again") or False),
            )
        )
        trace["request"] = p.submitted[-1] if p.submitted else None
    else:
        out = "（本验收脚本不执行该工具）"
    return out, trace


# ------------------------------------------------------------------ 标注样本


def case(kind, text, dataset_name, expect, *, allow_extra=(), soft=False):
    """soft=True：这句话本身有两种合理解释，结果照实打印但不计入通过率。"""
    return {
        "kind": kind,
        "text": text,
        "dataset": dataset_name,
        "expect": expect,
        "allow_extra": set(allow_extra),
        "soft": soft,
    }


CASES = [
    # 明确请求：群、时间都要对上
    case("明确请求", "生成今天电脑互助群报告", "base",
         {"tool": "group_digest", "gid": GID_A, "label": "今天", "again": False}),
    case("明确请求", "重新生成今天电脑互助群的日报", "base",
         {"tool": "group_digest", "gid": GID_A, "again": True}),
    case("明确请求", "把两个群最新消息跑一份", "base",
         {"tool": "group_digest", "gid": None, "incremental": True}),
    # 不该动手
    case("夸奖", "这份群聊情报总结得不错", "base", {}, allow_extra={"get_group_report"}),
    case("否定", "不要总结，我只是夸你", "base", {}),
    case("歧义", "取消吧", "base", {}, allow_extra={"digest_status"}),
    # 讨论报告：身份必须对
    case("讨论焦点", "总结得真好，有什么遗漏吗", "base",
         {"tool": "get_group_report", "report_id": A_ID}),
    case("讨论焦点", "继续讲第二条", "base",
         {"tool": "get_group_report", "report_id": A_ID, "topic_no": 2}),
    # 「刚才」既可能指正在讨论的，也可能指刚发来的：两种都不算错，不计入通过率
    case("歧义指代", "刚才那份第二条", "base", {}, allow_extra={"get_group_report"}, soft=True),
    case("指最近投递", "刚发来的那份第二条", "base",
         {"tool": "get_group_report", "report_id": B_ID, "topic_no": 2}),
    case("讨论焦点", "这份靠谱吗", "base", {"tool": "get_group_report", "report_id": A_ID}),
    # 重发
    case("重发", "把刚发来的那份再发我一次", "base",
         {"tool": "resend_report", "report_id": B_ID}),
    case("重发焦点", "把正在讨论的那份再发我一次", "base",
         {"tool": "resend_report", "report_id": A_ID}),
    # 状态与叫停
    case("状态", "跑到哪了", "base", {"tool": "digest_status"}),
    case("叫停", "取消这次总结", "base", {"tool": "cancel_digest"}),
    # 同一群两份报告：靠「刚发来的 / 刚才」区分
    case("同群-最近投递", "刚发来的那份第二条", "same_group",
         {"tool": "get_group_report", "report_id": "pc_new", "topic_no": 2}),
    case("同群-焦点", "接着聊第二条", "same_group",
         {"tool": "get_group_report", "report_id": "pc_old", "topic_no": 2}),
    # 焦点恰好等于最近投递
    case("焦点=最近投递", "把刚发来的那份再发我一次", "focus_is_newest",
         {"tool": "resend_report", "report_id": "pc_new"}),
    case("焦点=最近投递", "把昨天那份再发我一次", "focus_is_newest",
         {"tool": "resend_report", "report_id": "pc_old"}),
]

IDENTITY_KINDS = {
    "讨论焦点",
    "指最近投递",
    "重发",
    "重发焦点",
    "同群-最近投递",
    "同群-焦点",
    "焦点=最近投递",
}


# ------------------------------------------------------------------ 判定


def check_call(call, expect, p, text):
    """单次调用是否符合期望：工具名 + 关键参数 + 后端执行结果。"""
    problems = []
    if call["name"] != expect["tool"]:
        return [f"工具 {call['name']} ≠ {expect['tool']}"], ""
    out, trace = run_tool(p, call, text)
    args = call["args"]
    if "report_id" in expect:
        got = str(args.get("report_id") or "")
        if got != expect["report_id"]:
            problems.append(f"report_id={got or '(空)'} ≠ {expect['report_id']}")
        elif f"已读报告 {expect['report_id']}" not in out and "受理结果=sent" not in out:
            problems.append("后端没有真的读到/发出那一份")
        delivered = trace.get("delivered")
        if delivered:
            if delivered[0] != f"{expect['report_id']}.png":
                problems.append(f"实际发出的是 {delivered[0]}")
    if "topic_no" in expect:
        got = int(args.get("topic_no") or 0)
        if got != expect["topic_no"]:
            problems.append(f"topic_no={got} ≠ {expect['topic_no']}")
        elif expect["topic_no"] > 1 and f"话题 {expect['topic_no']}" not in out:
            problems.append("结果里没有那个话题")
    if expect["tool"] == "group_digest":
        req = trace.get("request")
        if req is None:
            problems.append("没有真的提交请求")
        else:
            if "gid" in expect and req.gid != expect["gid"]:
                problems.append(f"gid={req.gid} ≠ {expect['gid']}")
            if "label" in expect and req.label != expect["label"]:
                problems.append(f"label={req.label} ≠ {expect['label']}")
            if "again" in expect and bool(req.again) != expect["again"]:
                problems.append(f"again={req.again} ≠ {expect['again']}")
            if "incremental" in expect and bool(req.incremental) != expect["incremental"]:
                problems.append(f"incremental={req.incremental} ≠ {expect['incremental']}")
    return problems, out


def judge_round(p, text, calls, expect, allow_extra):
    """一轮的全部调用一起判：不许越权动手，必须命中期望的那一个。"""
    if not expect:
        side_effect = {"group_digest", "resend_report", "cancel_digest"}
        extra = [c["name"] for c in calls if c["name"] in side_effect and c["name"] not in allow_extra]
        if extra:
            return [f"不该动手却调了 {extra}"], ""
        return [], ""
    hits = [c for c in calls if c["name"] == expect["tool"]]
    if not hits:
        return [f"没有调用 {expect['tool']}（实际 {[c['name'] for c in calls] or '无'}）"], ""
    problems = []
    for call in calls:
        if call is hits[0]:
            continue
        if call["name"] in {"group_digest", "resend_report", "cancel_digest"}:
            problems.append(f"附带调用了 {call['name']}")
    sub_problems, out = check_call(hits[0], expect, p, text)
    problems.extend(sub_problems)
    return problems, out


def run_case(mod, spec, tools):
    root = pathlib.Path(tempfile.mkdtemp(prefix="model_accept_"))
    try:
        p = dataset(mod, spec["dataset"], root)
        prompt = build_prompt(p)
        base = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": spec["text"]},
        ]
        calls, _content, msg = post(base, tools)
        problems, out = judge_round(p, spec["text"], calls, spec["expect"], spec["allow_extra"])
        notes = []
        if calls:
            first = calls[0]
            shown = first["name"] + (
                f"({first['args'].get('report_id')})" if first["args"].get("report_id") else ""
            )
        else:
            shown = "（无工具调用）"
            notes.append("模型直接说话")

        expect = spec["expect"]
        # 后端因为「身份没确定」拒绝时，给模型一轮改正机会（这也是真实链路里会发生的事）：
        # 空 ID、整句话当 ID 传来的、ID 不存在的，都算。
        refused = any(
            marker in out for marker in ("没给 report_id", "找不到报告", "不是你提到的那个群")
        )
        missing_id = (
            calls
            and calls[0]["name"] in ("get_group_report", "resend_report")
            and (
                not str(calls[0]["args"].get("report_id") or "")
                or refused
            )
        )
        if expect and expect.get("report_id") and missing_id:
            msgs = base + [
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": [calls[0]["raw"]],
                },
                {"role": "tool", "tool_call_id": calls[0]["id"], "content": out},
            ]
            calls2, content2, _m2 = post(msgs, tools)
            r2_problems, _out2 = judge_round(
                p, spec["text"], calls2, expect, spec["allow_extra"]
            )
            if calls2:
                c2 = calls2[0]
                extra = ", ".join(f"{k}={v}" for k, v in c2["args"].items() if k != "report_id")
                shown += " →轮2 " + c2["name"] + "(" + str(
                    c2["args"].get("report_id") or "无 ID"
                ) + (f"; {extra}" if extra else "") + ")"
            else:
                shown += " →轮2 无工具调用"
                notes.append("轮2 反问：" + (content2 or "")[:24])
            if r2_problems:
                notes.append("轮2 " + "；".join(r2_problems))
                if not problems:
                    problems = ["轮1 缺 ID 且轮2 未补齐"]
            else:
                problems = []  # 第二轮补齐也算过
                notes.append("轮2 补齐")
        return spec, shown, problems, notes
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    mod = load_plugin()
    tools = tool_schemas(mod)
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    cases = [c for c in CASES if c["kind"] in IDENTITY_KINDS] if repeats > 1 else CASES
    print(f"provider={API_BASE} model={MODEL}")
    print(f"tools={[t['function']['name'] for t in tools]}")
    rows = []
    for _rep in range(repeats):
        for spec in cases:
            try:
                spec, shown, problems, notes = run_case(mod, spec, tools)
            except Exception as e:  # noqa: BLE001
                rows.append((spec, "调用失败", [str(e)], []))
                continue
            rows.append((spec, shown, problems, notes))

    width = max(len(r[0]["text"]) for r in rows) * 2 + 2
    print(f"\n{'类别':<14}{'用户原话':<{width}}{'模型动作':<40}判定")
    print("-" * (56 + width + 40))
    for spec, shown, problems, notes in rows:
        verdict = "OK" if not problems else "FAIL: " + "；".join(problems)
        if notes:
            verdict += "（" + "；".join(notes) + "）"
        print(f"{spec['kind']:<14}{spec['text']:<{width}}{shown:<40}{verdict}")
    hard = [r for r in rows if not r[0]["soft"]]
    ok = sum(1 for r in hard if not r[2])
    soft_ok = sum(1 for r in rows if r[0]["soft"] and not r[2])
    soft_all = sum(1 for r in rows if r[0]["soft"])
    print(f"\n合计 {len(hard)} 条标注样本，完全符合 {ok} 条")
    if soft_all:
        print(f"另 {soft_all} 条歧义句（不计入）：符合 {soft_ok} 条")
    return 0 if ok == len(hard) else 1


if __name__ == "__main__":
    raise SystemExit(main())

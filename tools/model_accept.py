"""真实模型验收：自然语言 → 工具选择 → 后端执行，全链路。

system prompt 由插件自己的 `silence_llm` 现场生成（含投递事实与焦点），
tools 由插件真实签名 + docstring 生成（与 AstrBot 的 add_func 同构：没有 required）。

每个标注样本跑两轮：
  轮 1：看模型选哪个工具、report_id 是否给了、指向哪一份；
  轮 2：如果它省略了 ID，把**后端真实的拒绝文本**（候选清单）喂回去，
        看它能否在同一轮里带上正确 ID 重试——这是「后端不猜」之后模型必须走的路。
"""

import asyncio
import importlib.util
import inspect
import json
import pathlib
import re
import shutil
import os
import sys
import tempfile

ASTRBOT_HOME = pathlib.Path(os.environ.get("ASTRBOT_HOME", r"C:\Users\Ray\astrbot"))
PLUGIN = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(ASTRBOT_HOME))

import httpx  # noqa: E402

import reports  # noqa: E402

"""真实模型验收脚本（不属于测试套件，手动跑）。

用法：python tools/model_accept.py [重复次数]
  - 不带参数：跑全套 12 条标注样本；
  - 带参数（如 3）：只跑 4 条身份敏感样本，重复 N 次看稳定性。

依赖本机 AstrBot 配置里启用的 chat completion provider（默认 wb2-deepseek-v41-flash），
并用插件自己的 on_llm_request 现场生成 system prompt、用真实签名生成工具定义。
"""

CFG = json.loads(
    (ASTRBOT_HOME / "data" / "cmd_config.json").read_text(encoding="utf-8-sig")
)
SOURCE = {s["id"]: s for s in CFG["provider_sources"]}["workbuddy2api"]
PROV = next(p for p in CFG["provider"] if p["id"] == "wb2-deepseek-v41-flash")
RAW_KEY = SOURCE["key"]
API_KEY = RAW_KEY[0] if isinstance(RAW_KEY, list) else RAW_KEY
API_BASE = SOURCE["api_base"].rstrip("/")
MODEL = PROV["model"]

UMO = "bot_a:FriendMessage:110001"
OWNER = "110001"
GID_A, GID_B = "220001", "220002"
NAME_A, NAME_B = "电脑互助群", "MeowShelter"
A_ID, B_ID = "chatA_yesterday_pc", "chatB_today_meow"

TYPE_MAP = {"string": "string", "str": "string", "int": "integer", "integer": "integer",
            "float": "number", "number": "number", "bool": "boolean", "boolean": "boolean"}


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
        args.append({"type": TYPE_MAP.get(jtype, "string"), "name": name, "description": text.strip()})
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
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props}}})
    out.sort(key=lambda t: t["function"]["name"])
    return out


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
    p._queue = __import__("collections").deque(maxlen=2)
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
    return p


def seed_reports(root):
    root.mkdir(parents=True, exist_ok=True)
    a = {"id": A_ID, "gid": GID_A, "group": NAME_A, "label": "昨天", "generated_at": 1_800_000_000,
         "overview": "刷机翻车成了主线",
         "topics": [{"no": 1, "title": "板子烧了", "summary": "老张的板子烧了", "refs": []},
                    {"no": 2, "title": "换一片", "summary": "老王建议换一片", "refs": []}]}
    b = {"id": B_ID, "gid": GID_B, "group": NAME_B, "label": "今天", "generated_at": 1_800_100_000,
         "overview": "猫舍今天聊了喂食机",
         "topics": [{"no": 1, "title": "喂食机卡住", "summary": "喂食机卡住了", "refs": []},
                    {"no": 2, "title": "改手动", "summary": "改回手动喂", "refs": []}]}
    for rec in (a, b):
        reports.save(root, rec)
        (root / f"{rec['id']}.png").write_bytes(b"png")
    return a, b


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
    payload = {"model": MODEL, "messages": messages, "tools": tools,
               "tool_choice": "auto", "temperature": 0}
    with httpx.Client(timeout=180) as c:
        r = c.post(f"{API_BASE}/chat/completions",
                   headers={"Authorization": f"Bearer {API_KEY}"}, json=payload)
        r.raise_for_status()
        data = r.json()
    msg = data["choices"][0]["message"]
    calls = []
    for call in msg.get("tool_calls") or []:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": call.get("id"), "name": call["function"]["name"],
                      "args": args, "raw": call})
    return calls, (msg.get("content") or "").strip(), msg


def run_tool(p, name, args, text):
    """把模型给的参数真的交给插件执行，返回真实结果文本。"""
    if name == "get_group_report":
        return asyncio.run(p.tool_get_group_report(
            Ev(text), report_id=str(args.get("report_id") or ""),
            topic_no=int(args.get("topic_no") or 0), gid=str(args.get("gid") or "")))
    if name == "resend_report":
        return asyncio.run(p.tool_resend_report(Ev(text), report_id=str(args.get("report_id") or "")))
    if name == "digest_status":
        return asyncio.run(p.tool_digest_status(Ev(text)))
    return "（本验收脚本不执行该工具）"


CASES = [
    # (原话, 期望工具, 期望 report_id, 类别)
    ("生成今天电脑互助群报告", "group_digest", None, "明确请求"),
    ("重新生成今天的日报", "group_digest", None, "明确请求"),
    ("这份群聊情报总结得不错", None, None, "夸奖（不必读）"),
    ("总结得真好，有什么遗漏吗", "get_group_report", A_ID, "夸奖"),
    ("不要总结，我只是夸你", None, None, "否定"),
    ("继续讲刚才第二条", "get_group_report", A_ID, "讨论焦点"),
    ("刚发来的那份第二条", "get_group_report", B_ID, "指最近投递"),
    ("把刚发来的那份再发我一次", "resend_report", B_ID, "重发"),
    ("把正在讨论的那份再发我一次", "resend_report", A_ID, "重发焦点"),
    ("跑到哪了", "digest_status", None, "状态"),
    ("取消这次总结", "cancel_digest", None, "叫停"),
    ("取消吧", None, None, "歧义（应先确认对象）"),
]


def judge(name, arg_id, want_tool, want_id):
    if want_tool is None:
        if name in (None, "cancel_digest") and not arg_id:
            return "OK"
        if name in ("group_digest", "resend_report", "get_group_report"):
            return "误调用"
        return "OK"
    if name != want_tool:
        return "工具错"
    if want_id:
        if not arg_id:
            return "缺 ID"
        if arg_id != want_id:
            return f"ID 错({arg_id})"
    return "OK"


IDENTITY_CASES = [
    ("继续讲刚才第二条", "get_group_report", A_ID, "讨论焦点"),
    ("刚发来的那份第二条", "get_group_report", B_ID, "指最近投递"),
    ("把正在讨论的那份再发我一次", "resend_report", A_ID, "重发焦点"),
    ("把刚发来的那份再发我一次", "resend_report", B_ID, "重发"),
]


def main():
    mod = load_plugin()
    root = pathlib.Path(tempfile.mkdtemp(prefix="model_accept_reports_"))
    if root.exists():
        shutil.rmtree(root)
    a, b = seed_reports(root)
    p = build_plugin(mod, root)
    reports.set_focus(p.state, UMO, A_ID, 1)
    reports.append_delivery(p.state, UMO, report_id=B_ID, group=NAME_B,
                            range_label="今天", stored=True)
    prompt = build_prompt(p)
    tools = tool_schemas(mod)
    print(f"provider={API_BASE} model={MODEL}")
    print(f"tools={[t['function']['name'] for t in tools]}")
    print(f"prompt={len(prompt)} 字，含投递快照={'[本会话最近的报告投递]' in prompt}")
    print()

    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    cases = IDENTITY_CASES if repeats > 1 else CASES
    rows = []
    for _rep in range(repeats):
      for text, want_tool, want_id, kind in cases:
          # 每个样本都从同一个初始状态开始：焦点 A、最近投递 B。
          # 读取成功会改焦点，不重置会让后面的样本继承上一轮的选择。
          if root.exists():
              shutil.rmtree(root)
          seed_reports(root)
          p = build_plugin(mod, root)
          reports.set_focus(p.state, UMO, A_ID, 1)
          reports.append_delivery(
              p.state, UMO, report_id=B_ID, group=NAME_B, range_label="今天", stored=True
          )
          prompt = build_prompt(p)
          base_msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": text}]
          try:
              calls, content, msg = post(base_msgs, tools)
          except Exception as e:  # noqa: BLE001
              rows.append((kind, text, f"调用失败 {e}", "", "", "ERROR"))
              continue
          if not calls:
              rows.append((kind, text, "（无工具调用）", "-", "-", judge(None, "", want_tool, want_id)))
              continue

          first = calls[0]
          name, args = first["name"], first["args"]
          arg_id = str(args.get("report_id") or "")
          tool_out = run_tool(p, name, args, text)
          verdict = judge(name, arg_id, want_tool, want_id)
          if want_id and "已读报告" in tool_out and f"已读报告 {want_id}" not in tool_out:
              verdict = "后端读错"

          retry_name, retry_id, retry_note = "-", "-", ""
          if name in ("get_group_report", "resend_report") and not arg_id:
              # 轮 2：把后端真实的拒绝 + 候选喂回去，看它能不能自己补上 ID
              msgs = base_msgs + [
                  {"role": "assistant", "content": msg.get("content") or "", "tool_calls": [first["raw"]]},
                  {"role": "tool", "tool_call_id": first["id"], "content": tool_out},
              ]
              calls2, _c2, _m2 = post(msgs, tools)
              if calls2:
                  retry_name = calls2[0]["name"]
                  retry_id = str(calls2[0]["args"].get("report_id") or "")
                  if retry_id:
                      out2 = run_tool(p, retry_name, calls2[0]["args"], text)
                      ok = retry_id == want_id and "已读报告" not in out2[:0]
                      retry_note = "轮2 补上 ID" if (want_id is None or retry_id == want_id) else "轮2 ID 仍不对"
                      if verdict == "缺 ID":
                          verdict = retry_note
                  else:
                      retry_note = "轮2 仍然省略 ID"
              else:
                  retry_note = "轮2 反问：" + (_c2 or "")[:26]
              retry_name = f"{retry_name}({retry_id})" if retry_name != "-" and retry_id else (
                  "（无工具调用）" if retry_name == "-" else retry_name
              )

          rows.append((kind, text, f"{name}({arg_id})" if arg_id else name, retry_name, tool_out.splitlines()[0][:34], verdict))

    print(f"{'类别':<12}{'用户原话':<22}{'轮1':<34}{'轮2':<30}{'判定':<14}后端首行")
    print("-" * 140)
    for kind, text, r1, r2, out, verdict in rows:
        print(f"{kind:<12}{text:<22}{r1:<34}{r2:<30}{verdict:<14}{out}")
    ok = sum(1 for r in rows if r[5].startswith("OK") or r[5].startswith("轮2 补上"))
    print(f"\n合计 {len(rows)} 条，符合预期 {ok} 条")


if __name__ == "__main__":
    main()

"""测试脚手架：按路径加载插件主模块，并构造"未启动"的插件实例。

不做 sys.path 魔法以外的任何准备：AstrBot 的依赖都在项目 venv 里，
插件目录本身也要进 sys.path，否则 `from intent import ...` 找不到模块。
"""

import importlib.util
import pathlib
import sys
import time

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = pathlib.Path(r"C:\Users\Ray\astrbot")

for _p in (str(PLUGIN_DIR), str(ASTRBOT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MODULE = None


def today_base(now: float | None = None, count: int = 40, step: int = 5) -> int:
    """造数据用的时间基准，保证落在「今天」窗口里且不跑到未来。

    直接写 time.time() - 3600 的测试，在刚过零点时会掉到昨天，「今天」就查不到了，
    于是每天 0 点到 1 点之间这套测试必然红一次（不是代码坏了，是基准歪了）。
    """
    from datetime import datetime

    if now is None:
        now = time.time()
    start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    base = max(start, now - 3600)
    # 刚过零点的几分钟里，基准贴着 0 点会让后面几行跑到未来（查询用到 until=now 就漏掉了）
    return int(max(start, min(base, now - (count - 1) * step - 1)))


def load_main():
    """加载 main.py（同一进程内只加载一次）。"""
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location("chat_intel_main", PLUGIN_DIR / "main.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["chat_intel_main"] = mod
        spec.loader.exec_module(mod)
        _MODULE = mod
    return _MODULE


def make_plugin(**attrs):
    """用 object.__new__ 造一个不连 AstrBot 的实例，只设测试需要的属性。

    默认值和 __init__ 保持一致，免得测试里出现「对象没有某属性」的假失败。
    """
    mod = load_main()
    p = object.__new__(mod.TechDigestPlugin)
    p.ignore_uids = set(attrs.pop("ignore_uids", []))
    p.use_local_store = attrs.pop("use_local_store", True)
    p.include_images = attrs.pop("include_images", True)
    p.max_images = attrs.pop("max_images", 4)
    p.max_messages = attrs.pop("max_messages", 10000)
    
    p.report_title = attrs.pop("report_title", "群聊情报")
    p.render_ppi = attrs.pop("render_ppi", 72)
    p.font_dir = attrs.pop("font_dir", "")
    p.state = attrs.pop("state", {})
    p.reports_dir = attrs.pop("reports_dir", "")
    p.owner_uids = set(attrs.pop("owner_uids", []))
    p.group_name_map = attrs.pop("group_name_map", {})
    p.silence_platforms = attrs.pop("silence_platforms", [])
    p.push_enabled = attrs.pop("push_enabled", False)
    p.push_umo = attrs.pop("push_umo", "")
    p.legacy_platform_id = attrs.pop("platform_id", "")
    p._push_cfg_error = ""
    p.chats = attrs.pop("chats", [])
    p.group_ids = attrs.pop("group_ids", [])
    p.smart_parse = attrs.pop("smart_parse", False)
    p.keep_days = attrs.pop("keep_days", 7)
    p._running = attrs.pop("_running", False)
    p._tasks = attrs.pop("_tasks", set())
    p._current_req = attrs.pop("_current_req", None)
    p._current_task = attrs.pop("_current_task", None)
    from collections import deque
    p._queue = attrs.pop("_queue", deque(maxlen=2))
    p._llm_blocked = attrs.pop("_llm_blocked", 0)
    p._result_blocked = attrs.pop("_result_blocked", 0)
    p._silenced = attrs.pop("_silenced", 0)
    p._diag_count = attrs.pop("_diag_count", 0)
    for key, value in attrs.items():
        setattr(p, key, value)
    return p, mod


def run_command(p, event, arg=""):
    """跑一次 `/群总结`，把 yield 出来的纯文本收成一个列表。"""
    import asyncio

    async def collect():
        out = []
        async for result in p.cmd_digest(event, arg=arg):
            for comp in getattr(result, "chain", None) or []:
                text = getattr(comp, "text", "")
                if text:
                    out.append(text)
        return out

    return asyncio.run(collect())

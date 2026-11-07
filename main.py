"""技术情报日报 —— 只留技术向内容的群聊日报（可静默投递）。

一件事做到底：
    1. 拉群历史：用指定 AstrBot 平台的 OneBot 适配器翻页取 get_group_msg_history
    2. 清洗：过滤纯表情/图片、机器人自己、超长消息，转成 [HH:MM] 昵称: 内容
    3. 抽取：单次 LLM 调用，只要 技术话题 / 工具与资源 / 就业与工作 / AI 动向 / 今日圣经
    4. 排版：本地 Typst 编译成一张长图，不依赖任何远程渲染服务
    5. 投递：落盘到插件数据目录，永不发回群里；到点私聊推送，或用命令随时生成
    6. 静默通道：可选择让承载它的机器人账号在任何群、任何私聊里都不开口（防暴露）

适合「用一个只读机器人账号挂着群、日报私聊给自己」这类用法。
"""

import asyncio
import json
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_tech_digest"

FONT_FILES = (
    "NotoSansSC.ttf",
    "NotoColorEmoji.ttf",
    "Inter.ttf",
    "ShareTechMono.ttf",
)

SYSTEM_PROMPT = """你是一名给计算机专业读者写情报简报的编辑。
你只关心这些方向：计算机与编程、AI、软件与工具、有意思的网站与资源、工作与就业。
读者的时间很贵，任何与技术无关的内容都不许出现在简报里。
你的文字要克制、具体、可执行：写清楚谁遇到了什么、涉及哪个库/版本/报错/命令，结论是什么。"""

USER_PROMPT_TEMPLATE = """下面是群「{group}」在 {window} 的聊天记录，格式为 `[HH:MM] 昵称: 内容`。

请只抽取与「计算机/编程、AI、软件与工具、有意思的网站与资源、工作与就业」相关的内容，输出严格 JSON：

{{
  "headline": "一句话概括这段时间群里技术侧在聊什么（不超过 40 字；没有就填空字符串）",
  "topics": [
    {{"title": "不超过 12 字", "detail": "2-4 句话：谁遇到了什么、涉及的具体技术细节（库名/版本/报错原文/命令/方案）、最后结论", "people": ["昵称1", "昵称2"]}}
  ],
  "tools": [
    {{"name": "工具/网站/仓库/论文名", "url": "记录里出现过的原始链接，没有就空字符串", "note": "一句话说明它有什么用"}}
  ],
  "jobs": [
    {{"text": "岗位、校招、社招、面试、简历、薪资、行业动向等信息，一句话", "who": "相关人的昵称，可空"}}
  ],
  "ai_news": [
    {{"text": "模型或 AI 工具的发布、评测、公司动态，一句话"}}
  ],
  "quotes": [
    {{"content": "原文，可截取，不超过 80 字", "sender": "昵称", "reason": "一句话冷静点评，说明它为什么值得留"}}
  ]
}}

硬性规则：
1. 与技术无关的一律不要：吃饭、游戏内容、情感恋爱、追星、地域或时政争吵、纯玩梗复读。
2. 数量上限：topics 5 条、tools 8 条、jobs 5 条、ai_news 5 条、quotes 4 条。宁少勿凑，没有就返回空数组。
3. 记录里没有的东西绝对不要编造；url 只能照抄记录中出现过的链接。
4. tools 只在「记录里出现链接、仓库/项目名并说明了用途，或有人真的在用它」时收录；
   只是顺口提一句产品名（例如"我现在都不用 GitHub 了"）不要收录。
5. 纯情绪、纯吐槽、没有信息量的话题（"这个好难啊""怎么办"）不要单独成条，除非里面有具体报错、版本、命令或方案。
6. 只输出 JSON 本体，不要 markdown 代码块，不要任何解释文字。

聊天记录：
{transcript}"""


@register(PLUGIN_NAME, "Ray", "只留技术向内容的群聊日报，本地排版，可静默投递", "1.0.0")
class TechDigestPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.group_id = str(self.config.get("group_id") or "").strip()
        self.platform_id = str(self.config.get("platform_id") or "").strip()
        try:
            self.days = float(self.config.get("days") or 1.0)
        except (TypeError, ValueError):
            self.days = 1.0
        try:
            self.max_messages = int(self.config.get("max_messages") or 1500)
        except (TypeError, ValueError):
            self.max_messages = 1500
        self.daily_time = str(self.config.get("daily_time") or "").strip()
        self.push_enabled = self._as_bool(self.config.get("push_enabled"), False)
        self.push_umo = str(self.config.get("push_umo") or "").strip()
        self.keep_quotes = self._as_bool(self.config.get("keep_quotes"), True)
        self.report_title = str(self.config.get("report_title") or "技术情报日报").strip()
        try:
            self.ppi = max(72, int(self.config.get("render_ppi") or 180))
        except (TypeError, ValueError):
            self.ppi = 180

        ignore = str(self.config.get("ignore_uids") or "")
        self.ignore_uids = {u.strip() for u in ignore.split(",") if u.strip()}

        silence = self.config.get("silence_platforms") or ""
        if isinstance(silence, str):
            silence = [x.strip() for x in silence.split(",") if x.strip()]
        elif isinstance(silence, (list, tuple)):
            silence = [str(x).strip() for x in silence if str(x).strip()]
        self.silence_platforms = silence or ([self.platform_id] if self.platform_id else [])
        self._silenced = 0
        self._llm_blocked = 0
        self._result_blocked = 0

        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.reports_dir = self.data_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.store_dir = self.data_dir / "messages"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.use_local_store = self._as_bool(self.config.get("use_local_store"), True)
        self.font_dir = Path(
            str(self.config.get("font_dir") or "").strip() or (self.data_dir / "fonts")
        )
        self.font_dir.mkdir(parents=True, exist_ok=True)
        self.template = Path(__file__).parent / "templates" / "report.typ"

        self._transcript_cache: tuple[list[dict], str] = ([], "")
        self._task: asyncio.Task | None = None
        self._last_run_date: str | None = None
        self._running = False

        fonts = [f.name for f in self.font_dir.iterdir() if f.suffix.lower() in (".ttf", ".otf", ".ttc")]
        logger.info(
            f"[{PLUGIN_NAME}] 群={self.group_id or '(未配置)'} 平台={self.platform_id or '(未配置)'} "
            f"定时={self.daily_time or '关闭'} 推送={self.push_umo or '关闭'} "
            f"静默平台={self.silence_platforms or '无'} 字体={len(fonts)} 个"
        )
        if not fonts:
            logger.warning(
                f"[{PLUGIN_NAME}] {self.font_dir} 里没有字体文件，中文可能显示为方框。"
                "把 NotoSansSC.ttf 等字体放进去，或在配置里指定 font_dir。"
            )
        if not self.group_id:
            logger.warning(f"[{PLUGIN_NAME}] 还没配置 group_id，生成前请在插件配置里填好群号")

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def _as_bool(value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(value)

    def _get_bot(self):
        if not self.platform_id:
            return None
        try:
            inst = self.context.get_platform_inst(self.platform_id)
        except Exception:
            inst = None
        if inst is None:
            return None
        return getattr(inst, "bot", None)

    def _get_provider(self):
        provider_id = str(self.config.get("provider_id") or "").strip()
        if provider_id:
            try:
                p = self.context.provider_manager.get_provider_by_id(provider_id)
                if p:
                    return p
            except Exception:
                pass
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    # ------------------------------------------------------------------ 静默通道
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def silence_events(self, event: AstrMessageEvent):
        """第一道闸：静默平台的事件直接终止，不进后续任何流程。"""
        platform_id = self._platform_of(event)
        if platform_id not in self.silence_platforms:
            return
        self._record(event)
        self._silenced += 1
        if self._silenced <= 3 or self._silenced % 200 == 0:
            try:
                sender = event.get_sender_name()
            except Exception:
                sender = "?"
            logger.info(
                f"[{PLUGIN_NAME}] 静默平台 {platform_id} 的事件（累计 {self._silenced} 条，"
                f"最近来自 {sender}）"
            )
        event.stop_event()

    @filter.on_llm_request()
    async def silence_llm(self, event: AstrMessageEvent, req: ProviderRequest) -> bool:
        """第二道闸：即使事件绕过 stop_event（例如 @ 唤醒），也不让模型出手。"""
        platform_id = self._platform_of(event)
        if platform_id not in self.silence_platforms:
            return False
        self._llm_blocked += 1
        if self._llm_blocked <= 5 or self._llm_blocked % 100 == 0:
            logger.warning(
                f"[{PLUGIN_NAME}] 拦下一次 LLM 请求（平台 {platform_id}，累计 {self._llm_blocked} 次）"
            )
        return True

    @filter.on_decorating_result()
    async def silence_result(self, event: AstrMessageEvent):
        """第三道闸：回复已经生成也不发出去。"""
        platform_id = self._platform_of(event)
        if platform_id not in self.silence_platforms:
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        self._result_blocked += 1
        if self._result_blocked <= 5 or self._result_blocked % 100 == 0:
            logger.warning(
                f"[{PLUGIN_NAME}] 清空了一条本要发到 {platform_id} 的回复（累计 {self._result_blocked} 次）"
            )
        result.chain = []
        event.stop_event()

    @staticmethod
    def _platform_of(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_id() or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------ 本地消息库
    # SnowLuma / NapCat 这类实现的历史接口一次只给最近 ~200 条，
    # 想覆盖一整天只能自己攒：静默守卫能看到每一条群消息，顺手落盘。
    def _record(self, event: AstrMessageEvent) -> None:
        if not self.use_local_store or not self.group_id:
            return
        try:
            gid = str(event.get_group_id() or "")
            if gid != self.group_id:
                return
            uid = str(event.get_sender_id() or "")
            if not uid or uid in self.ignore_uids:
                return
            text = re.sub(r"\s+", " ", (event.message_str or "")).strip()
            if not text or text in ("[表情]", "[图片]"):
                return
            record = {
                "t": int(time.time()),
                "uid": uid,
                "name": str(event.get_sender_name() or "")[:16],
                "text": text[:280],
            }
            path = self.store_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            self._diag("异常", repr(e), exc=True)

    def _diag(self, tag: str, detail: str, exc: bool = False) -> None:
        """只在头几次把落盘问题打出来，避免刷屏。"""
        self._diag_count = getattr(self, "_diag_count", 0) + 1
        if self._diag_count <= 5:
            logger.warning(f"[{PLUGIN_NAME}] 记录消息失败（{tag}）：{detail}", exc_info=exc)

    def _store_stats(self, since: float) -> tuple[int, str]:
        records = self._load_store(since)
        if not records:
            return 0, "无"
        last = max(records, key=lambda r: int(r.get("t") or 0))
        return len(records), datetime.fromtimestamp(int(last.get("t") or 0)).strftime("%m-%d %H:%M")

    def _load_store(self, since: float) -> list[dict]:
        out: list[dict] = []
        if not self.store_dir.exists():
            return out
        for path in sorted(self.store_dir.glob("*.jsonl")):
            try:
                with path.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if int(record.get("t") or 0) >= since:
                            out.append(record)
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 读取 {path.name} 失败: {e}")
        return out

    # ------------------------------------------------------------------ 拉消息
    async def fetch_history(self) -> tuple[list[dict], str]:
        if not self.group_id:
            return [], "还没配置 group_id"
        bot = self._get_bot()
        if bot is None or not hasattr(bot, "call_action"):
            return [], f"平台 {self.platform_id or '(未配置)'} 未连接，拿不到消息"

        since = time.time() - self.days * 86400
        collected: list[dict] = []
        seen: set[str] = set()
        anchor = None

        while len(collected) < self.max_messages:
            params = {"group_id": int(self.group_id), "count": 200}
            if anchor is not None:
                params["message_seq"] = anchor
            try:
                raw = await bot.call_action("get_group_msg_history", **params)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 拉取历史失败: {e}")
                break
            batch = self._unwrap_messages(raw)
            if not batch:
                break

            added = 0
            for m in batch:
                mid = str(m.get("message_id") or m.get("real_id") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                if int(m.get("time") or 0) >= since:
                    collected.append(m)
                    added += 1

            # 锚点必须取这一页里最老的一条，否则会一直拿同一页
            oldest_msg = min(batch, key=lambda m: int(m.get("time") or 0))
            oldest = int(oldest_msg.get("time") or 0)
            next_anchor = oldest_msg.get("message_seq") or oldest_msg.get("message_id")
            logger.debug(
                f"[{PLUGIN_NAME}] 拉取一页 {len(batch)} 条（新增 {added}，累计 {len(collected)}），"
                f"最老 {datetime.fromtimestamp(oldest):%m-%d %H:%M}，下一页锚点 {next_anchor}"
            )
            if oldest < since or next_anchor is None or added == 0:
                break
            if anchor is not None and str(next_anchor) == str(anchor):
                break
            anchor = next_anchor
            await asyncio.sleep(0.2)

        collected.sort(key=lambda m: int(m.get("time") or 0))
        records = [self._norm_onebot(m) for m in collected]
        return records, ""

    def _norm_onebot(self, msg: dict) -> dict:
        sender = msg.get("sender") or {}
        return {
            "t": int(msg.get("time") or 0),
            "uid": str(msg.get("user_id") or ""),
            "name": str(sender.get("card") or sender.get("nickname") or "")[:16],
            "text": self._msg_text(msg)[:280],
        }

    @staticmethod
    def _unwrap_messages(raw) -> list[dict]:
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, dict):
            return []
        for key in ("messages", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                inner = value.get("messages")
                if isinstance(inner, list):
                    return inner
        return []

    async def _group_name(self) -> str:
        configured = str(self.config.get("group_name") or "").strip()
        if configured:
            return configured
        bot = self._get_bot()
        if bot is not None and hasattr(bot, "call_action"):
            try:
                info = await bot.call_action("get_group_info", group_id=int(self.group_id))
                data = info.get("data") if isinstance(info, dict) else None
                if isinstance(data, dict) and data.get("group_name"):
                    return str(data["group_name"])
            except Exception:
                pass
        return f"群 {self.group_id}"

    @staticmethod
    def _msg_text(msg: dict) -> str:
        segments = msg.get("message")
        if isinstance(segments, str):
            return re.sub(r"\s+", " ", segments).strip()
        parts: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                parts.append(str(data.get("text") or ""))
            elif stype == "image":
                parts.append("[图片]")
            elif stype == "face":
                parts.append("[表情]")
            elif stype == "at":
                parts.append("@" + str(data.get("qq") or ""))
            elif stype == "reply":
                continue
            elif stype in ("record", "video"):
                parts.append("[语音/视频]")
            elif stype == "file":
                parts.append("[文件]")
            elif stype == "forward":
                parts.append("[聊天记录]")
            else:
                parts.append(f"[{stype}]")
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def _build_transcript(self) -> tuple[str, int, int]:
        records, _ = self._transcript_cache
        lines: list[str] = []
        senders: set[str] = set()
        for record in records:
            uid = str(record.get("uid") or "")
            if uid in self.ignore_uids:
                continue
            text = str(record.get("text") or "").strip()
            if not text or text in ("[表情]", "[图片]"):
                continue
            name = str(record.get("name") or uid)[:16]
            ts = int(record.get("t") or 0)
            stamp = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"
            senders.add(uid)
            lines.append(f"[{stamp}] {name}: {text}")
        transcript = "\n".join(lines)
        if len(transcript) > 70000:
            transcript = transcript[-70000:]
        return transcript, len(lines), len(senders)

    # ------------------------------------------------------------------ LLM
    async def _extract(self, group_name: str, window: str) -> dict | None:
        provider = self._get_provider()
        if provider is None:
            logger.error(f"[{PLUGIN_NAME}] 没有可用的模型供应商")
            return None
        transcript, _, _ = self._build_transcript()
        if not transcript.strip():
            return {"headline": "", "topics": [], "tools": [], "jobs": [], "ai_news": [], "quotes": []}
        prompt = USER_PROMPT_TEMPLATE.format(group=group_name, window=window, transcript=transcript)
        try:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt=SYSTEM_PROMPT, contexts=[]
            )
            text = getattr(resp, "completion_text", "") or ""
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 调用模型失败: {e}", exc_info=True)
            return None
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            logger.warning(f"[{PLUGIN_NAME}] 模型输出里找不到 JSON：{cleaned[:200]}")
            return None
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as e:
            logger.warning(f"[{PLUGIN_NAME}] JSON 解析失败: {e}")
            return None

    # ------------------------------------------------------------------ 渲染
    def _render(self, payload: dict) -> Path | None:
        try:
            import typst
        except ImportError:
            logger.error(f"[{PLUGIN_NAME}] 未安装 typst，请 pip install typst")
            return None
        out = self.reports_dir / (
            f"tech_digest_{self.group_id or 'group'}_{datetime.now():%Y%m%d_%H%M%S}.png"
        )
        # 模板把数据读成同目录的 data.json，所以复制到临时任务目录里编译
        job = Path(tempfile.mkdtemp(prefix="tech_digest_"))
        try:
            shutil.copyfile(self.template, job / "main.typ")
            (job / "data.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            typst.compile(
                str(job / "main.typ"),
                output=str(out),
                root=str(job),
                font_paths=[str(self.font_dir)],
                format="png",
                ppi=self.ppi,
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Typst 排版失败: {e}", exc_info=True)
            return None
        finally:
            shutil.rmtree(job, ignore_errors=True)
        return out

    # ------------------------------------------------------------------ 主流程
    async def generate(self) -> tuple[Path | None, str]:
        if self._running:
            return None, "已经有一份日报在生成了，稍等"
        self._running = True
        started = time.time()
        try:
            since = time.time() - self.days * 86400
            records = self._load_store(since) if self.use_local_store else []
            hist, err = await self.fetch_history()
            if hist:
                records.extend(hist)

            seen: set[tuple] = set()
            uniq: list[dict] = []
            for record in sorted(records, key=lambda r: int(r.get("t") or 0)):
                key = (
                    str(record.get("uid") or ""),
                    int(record.get("t") or 0) // 60,
                    str(record.get("text") or "")[:40],
                )
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(record)
            if not uniq:
                return None, err or "这段时间没拿到消息"
            self._transcript_cache = (uniq, "")

            group_name = await self._group_name()
            window = f"最近 {self.days:g} 天"
            transcript, msg_count, people = self._build_transcript()

            data = await self._extract(group_name, window)
            if data is None:
                return None, "模型没有给出可用的结构化结果"

            quotes = data.get("quotes") or []
            if not self.keep_quotes:
                quotes = []

            now = datetime.now()
            payload = {
                "title": self.report_title,
                "date": now.strftime("%Y-%m-%d"),
                "group": (
                    group_name
                    if self.group_id in group_name
                    else f"{group_name} · {self.group_id}"
                ),
                "stats": {"line": f"{msg_count} 条消息 · {people} 人参与 · 采样{window}"},
                "headline": str(data.get("headline") or "").strip(),
                "topics": self._norm_list(data.get("topics"), ("title", "detail", "people")),
                "tools": self._norm_list(data.get("tools"), ("name", "url", "note")),
                "jobs": self._norm_list(data.get("jobs"), ("text", "who")),
                "ai_news": self._norm_list(data.get("ai_news"), ("text",)),
                "quotes": self._norm_list(quotes, ("content", "sender", "reason")),
                "footer": f"生成于 {now:%Y-%m-%d %H:%M} · {PLUGIN_NAME} · 本报告不会发到群里",
            }

            path = self._render(payload)
            if path is None:
                return None, "排版成图失败，看日志"
            cost = time.time() - started
            info = (
                f"{payload['date']} 日报：{msg_count} 条消息 / {people} 人 · "
                f"话题 {len(payload['topics'])} · 资源 {len(payload['tools'])} · "
                f"就业 {len(payload['jobs'])} · AI {len(payload['ai_news'])} · "
                f"金句 {len(payload['quotes'])} · {cost:.0f}s"
            )
            logger.info(f"[{PLUGIN_NAME}] {info} -> {path.name}")
            return path, info
        finally:
            self._running = False

    @staticmethod
    def _norm_list(items, keys: tuple[str, ...]) -> list[dict]:
        out: list[dict] = []
        if not isinstance(items, list):
            return out
        for item in items:
            if not isinstance(item, dict):
                continue
            row = {}
            for key in keys:
                value = item.get(key)
                if isinstance(value, list):
                    value = "、".join(str(v) for v in value if str(v).strip())
                row[key] = str(value).strip() if value is not None else ""
            if any(row[k] for k in keys[:2]):
                out.append(row)
        return out

    async def _push(self, path: Path, caption: str) -> bool:
        if not self.push_enabled or not self.push_umo:
            return False
        try:
            ok = await self.context.send_message(
                self.push_umo, MessageChain([Image.fromFileSystem(str(path))])
            )
            if ok:
                await asyncio.sleep(1)
                await self.context.send_message(self.push_umo, MessageChain().message(caption))
                logger.info(f"[{PLUGIN_NAME}] 已推送 {path.name} -> {self.push_umo}")
            return bool(ok)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 推送失败: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------ 定时
    async def _scheduler(self) -> None:
        while True:
            try:
                if self.daily_time:
                    now = datetime.now()
                    today = now.strftime("%Y-%m-%d")
                    if now.strftime("%H:%M") == self.daily_time and self._last_run_date != today:
                        self._last_run_date = today
                        logger.info(f"[{PLUGIN_NAME}] 定时任务触发 {self.daily_time}")
                        path, info = await self.generate()
                        if path is not None:
                            await self._push(path, info)
                        else:
                            logger.warning(f"[{PLUGIN_NAME}] 定时生成失败: {info}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 定时任务异常: {e}", exc_info=True)
            await asyncio.sleep(30)

    async def initialize(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._scheduler())

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    # ------------------------------------------------------------------ 命令
    @filter.command("techdigest", alias={"技术日报"})
    async def cmd_digest(self, event: AstrMessageEvent):
        """生成本群技术情报日报；加参数 last 重发最近一份。"""
        # 唤醒前缀可能已被去掉，命令名与别名都可能是任意大小写，这里统一用正则剥掉
        arg = re.sub(
            r"^\s*/?\s*(?:techdigest|技术日报)\s*",
            "",
            event.message_str or "",
            flags=re.IGNORECASE,
        ).strip()
        if arg in ("status", "状态"):
            since = time.time() - self.days * 86400
            count, last = self._store_stats(since)
            latest = self._latest_report()
            yield event.plain_result(
                "\n".join(
                    [
                        f"目标群：{self.group_id}（{await self._group_name()}）",
                        f"平台：{self.platform_id or '未配置'} · 静默："
                        f"{'、'.join(self.silence_platforms) or '无'}",
                        f"本地库：{count} 条（最近 {last}）· 窗口 {self.days:g} 天",
                        f"静默事件 {self._silenced} · 拦下 LLM {self._llm_blocked} 次 · "
                        f"清空回复 {self._result_blocked} 次",
                        f"最近报告：{latest.name if latest else '无'}",
                        f"定时：{self.daily_time or '关闭'} · 推送：{self.push_umo or '关闭'}",
                    ]
                )
            )
            return
        if arg in ("last", "最新", "上一份"):
            latest = self._latest_report()
            if latest is None:
                yield event.plain_result("还没有生成过日报。")
                return
            stamp = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m-%d %H:%M")
            yield event.image_result(str(latest))
            yield event.plain_result(f"最近一份：{stamp} 生成（{latest.name}）")
            return
        if self._running:
            yield event.plain_result("正在生成中，好了会直接发给你。")
            return
        yield event.plain_result("开工了：拉消息 → 抽技术向内容 → 本地排版，大概 1~2 分钟。")
        asyncio.create_task(self._generate_and_send(event.unified_msg_origin))

    async def _generate_and_send(self, umo: str) -> None:
        path, info = await self.generate()
        if path is None:
            await self.context.send_message(umo, MessageChain().message(f"生成失败：{info}"))
            return
        await self.context.send_message(umo, MessageChain([Image.fromFileSystem(str(path))]))
        await asyncio.sleep(1)
        await self.context.send_message(umo, MessageChain().message(info))

    def _latest_report(self) -> Path | None:
        files = sorted(
            (p for p in self.reports_dir.glob("*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        return files[-1] if files else None

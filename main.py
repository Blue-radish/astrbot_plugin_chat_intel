"""群聊情报 —— 每个群一份兴趣，按兴趣总结群聊，图片留存，本地排版，静默投递。

一件事做到底：
    1. 多群累积：静默通道里把目标群的消息（含图片）实时落盘
    2. 时间语义：看最新=只总结没看过的；也可指定今天/昨天/具体日期
    3. 抽取：LLM 结构化输出，只要 技术话题 / 工具与资源 / 就业与工作 / AI 动向 / 今日圣经
    4. 排版：本地 Typst 编译成一张长图（含现场图片），不依赖远程渲染
    5. 投递：落盘到插件数据目录，永不发回群里；到点私聊推送，或命令随时生成
    6. 静默通道：承载它的机器人账号在任何群、任何私聊里都不开口
"""

import asyncio
import hashlib
import io
import json
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_chat_intel"


try:  # 插件以包形式被 AstrBot 加载时走相对导入
    from .interest import ChatInterest, parse_chats
except ImportError:  # pragma: no cover - 单测按文件路径加载模块
    from interest import ChatInterest, parse_chats

try:  # 简繁归一化：群名可能是繁体，用户打的是简体
    from zhconv import convert as _zh_convert
except Exception:  # pragma: no cover - 没装就退化成原文匹配
    _zh_convert = None

# 报告里各板块的条数上限（合并多个分片后仍要克制）
CAP_TOPICS = 8
CAP_TOOLS = 10
CAP_JOBS = 8
CAP_AI_NEWS = 6
CAP_QUOTES = 4

IMG_CACHE_VERSION = 3  # 判据说辞变化时 +1，旧缓存作废

CHUNK_CHARS = 55000  # 单次喂给模型的转录上限，超了切片后合并

ARG_PROMPT = """把用户这句话里的「时间范围」和「群」抽出来，只输出 JSON：
{"date": "", "group": ""}

date 写成这些形式之一：今天 / 昨天 / 前天 / 大前天 / 本周 / 上周 / 最近3天 / 3天前 / 2026-11-05。
没提到时间就留空字符串（表示"只看没看过的"）。

group 写群名里最有辨识度的一小段（例如"考古"），没提到群就留空字符串。
不要解释、不要代码块。

用户的话：{text}"""

VISION_PROMPT = """这是群聊里出现的一张图片。判断它作为「技术情报」的配图有没有保留价值。

值得留（keep=true）：
- 报错窗口、异常堆栈、编译或运行失败提示
- 代码、终端、日志、命令行输出
- 架构图、流程图、电路或硬件图纸、协议时序图
- 技术类数据图表、性能曲线、监控面板（接口耗时、压测、资源占用这类）
- 论文、文档、规范、接口说明的截图
- 招聘或职位信息、JD、薪资与面试相关截图
- 群聊或论坛里关于技术、行业、就业话题的讨论截图（例如硬件行情、模型发布、公司动向、裁员或招聘）

不该留（keep=false）：
- 表情包、梗图、二次元或动漫图、摄影照片、自拍、游戏画面、纯风景
- 没有可读信息的截图
- 生活类 App 界面：睡眠、健康、运动、天气、购物、外卖、记账
- 个人社交动态：朋友圈、微博、小红书里的晒图或情绪帖

遇到模棱两可的，按「读者是学计算机、写代码、找工作的人，这图对他有没有信息价值」来判断，没有就 false。

只输出 JSON：{"keep": true 或 false, "kind": "报错截图/代码/终端/图表/文档/招聘/讨论截图/电路图纸/表情包/照片/动漫/游戏/生活界面/社交动态/其他", "text": "图里能看清的文字，摘录不超过 24 字，看不清就给空字符串"}"""

IMAGE_PICK_PROMPT = """下面是群聊里出现过的候选图片，每行给出：编号、发送者、时间、这条消息的文字、上下文。

读者只关心技术内容。**只有这些图值得留下**：报错截图、代码或终端截图、架构图/流程图、数据图表、
论文或文档截图、招聘或职位信息截图、与具体技术方案相关的配置界面。

**坚决不要**：表情包、梗图、二次元/动漫图、自拍、纯风景、单纯搞笑图、群头像或昵称截图。
判断提示：表情包往往尺寸很小、接近正方形、画面是卡通脸或带大字；技术截图通常尺寸大、横宽或竖长、里面有文字和界面。
候选里给出了图片尺寸，任一边小于 300 像素或长宽比接近 1:1 的，优先当成表情包排除。

输出严格 JSON：{{"picks": [编号, ...]}}
要求：最多 {limit} 个，按价值从高到低排序；宁少勿滥，全都不相关就返回空数组；只输出 JSON 本体。

候选：
{items}"""

# 模型不可用时的兜底：用上下文文字里的技术关键词判断
# 出现这些词，图片才可能是技术截图（本条消息自己带字时才算数）
TECH_HINTS_STRONG = (
    "报错", "错误", "异常", "日志", "代码", "终端", "配置", "部署", "接口", "架构", "论文",
    "数据集", "编译", "测试结果", "性能", "监控", "崩溃", "堆栈", "版本",
    "error", "exception", "traceback", "stack", "panic", "warning", "bug", "api", "sql",
    "docker", "k8s", "timeout", "screenshot", "stdout",
)

TECH_HINTS = (
    "报错", "错误", "异常", "日志", "代码", "终端", "命令", "配置", "部署", "接口", "数据库",
    "架构", "流程图", "图表", "论文", "截图", "编译", "测试", "性能", "监控", "超时", "崩溃",
    "error", "exception", "traceback", "stack", "panic", "warning", "bug", "api", "sql",
    "docker", "k8s", "git", "npm", "pip", "http", "404", "500", "timeout", "screenshot",
    "offer", "jd", "recruit",
)

SYSTEM_PROMPT = """你是一名给计算机专业读者写情报简报的编辑。
你只关心这些方向：计算机与编程、AI、软件与工具、有意思的网站与资源、工作与就业。
读者的时间很贵，任何与技术无关的内容都不许出现在简报里。
你的文字要克制、具体、可执行：写清楚谁遇到了什么、涉及哪个库/版本/报错/命令，结论是什么。"""

USER_PROMPT_TEMPLATE = """下面是群「{group}」在 {window} 的聊天记录，格式为 `[HH:MM] 昵称: 内容`（`[图]` 表示这条带了图片）。

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
6. 如果记录里出现 `[图]` 且上下文在讲技术（报错截图、代码截图、架构图、数据图表），
   在对应 topic 的 detail 里点一句「（有配图）」，方便读者找到原图。
7. 只输出 JSON 本体，不要 markdown 代码块，不要任何解释文字。

聊天记录：
{transcript}"""


@register(PLUGIN_NAME, "Ray", "群聊情报：每个群一份兴趣，按兴趣总结群聊，视觉模型筛图，本地排版成图，静默私聊投递", "1.2.2")
class TechDigestPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.chats: list[ChatInterest] = []
        self.group_ids: list[str] = []
        self.group_name_map = self._parse_group_names()
        try:
            self.days = float(self.config.get("days") or 1.0)
        except (TypeError, ValueError):
            self.days = 1.0
        try:
            self.max_messages = max(50, int(self.config.get("max_messages") or 10000))
        except (TypeError, ValueError):
            self.max_messages = 10000
        self.daily_time = str(self.config.get("daily_time") or "").strip()
        self.push_enabled = self._as_bool(self.config.get("push_enabled"), False)
        self.push_umo = str(self.config.get("push_umo") or "").strip()
        self.keep_quotes = self._as_bool(self.config.get("keep_quotes"), True)
        self.report_title = str(self.config.get("report_title") or "群聊情报").strip()
        try:
            self.ppi = max(72, int(self.config.get("render_ppi") or 180))
        except (TypeError, ValueError):
            self.ppi = 180

        self.include_images = self._as_bool(self.config.get("include_images"), True)
        try:
            self.max_images = max(0, int(self.config.get("max_images") or 6))
        except (TypeError, ValueError):
            self.max_images = 6
        try:
            self.max_image_mb = max(1, int(self.config.get("max_image_mb") or 8))
        except (TypeError, ValueError):
            self.max_image_mb = 8
        try:
            self.keep_days = max(1, int(self.config.get("keep_days") or 7))
        except (TypeError, ValueError):
            self.keep_days = 7
        self.smart_parse = self._as_bool(self.config.get("smart_parse"), True)

        owners = str(self.config.get("owner_uids") or "")
        self.owner_uids = {u.strip() for u in re.split(r"[,，\s]+", owners) if u.strip()}

        ignore = str(self.config.get("ignore_uids") or "")
        self.ignore_uids = {u.strip() for u in ignore.split(",") if u.strip()}

        silence = self.config.get("silence_platforms") or ""
        if isinstance(silence, str):
            silence = [x.strip() for x in silence.split(",") if x.strip()]
        elif isinstance(silence, (list, tuple)):
            silence = [str(x).strip() for x in silence if str(x).strip()]
        self.platform_id = str(self.config.get("platform_id") or "").strip()
        self.silence_platforms = silence or ([self.platform_id] if self.platform_id else [])
        # 必须在 platform_id 之后解析 chats：每群的 platform 缺省时要回退到它
        self._apply_chats(self.config.get("chats"))
        self._silenced = 0
        self._llm_blocked = 0
        self._result_blocked = 0
        self._diag_count = 0

        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.reports_dir = self.data_dir / "reports"
        self.store_dir = self.data_dir / "messages"
        self.images_dir = self.data_dir / "images"
        for d in (self.reports_dir, self.store_dir, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.use_local_store = self._as_bool(self.config.get("use_local_store"), True)
        self.font_dir = Path(
            str(self.config.get("font_dir") or "").strip() or (self.data_dir / "fonts")
        )
        self.font_dir.mkdir(parents=True, exist_ok=True)
        self.template = Path(__file__).parent / "templates" / "report.typ"
        self.state_path = self.data_dir / "state.json"
        self.state = self._load_state()

        self._transcript_cache: tuple[list[dict], str] = ([], "")
        self._task: asyncio.Task | None = None
        self._last_run_date: str | None = None
        self._running = False

        fonts = [
            f.name
            for f in self.font_dir.iterdir()
            if f.suffix.lower() in (".ttf", ".otf", ".ttc")
        ]
        logger.info(
            f"[{PLUGIN_NAME}] 群={self.group_ids or '(未配置)'} 平台={self.platform_id or '(未配置)'} "
            f"定时={self.daily_time or '关闭'} 推送={self.push_umo or '关闭'} "
            f"图片={'开' if self.include_images else '关'} 上限={self.max_messages} 条 "
            f"静默平台={self.silence_platforms or '无'} 字体={len(fonts)} 个"
        )
        if not fonts:
            logger.warning(
                f"[{PLUGIN_NAME}] {self.font_dir} 里没有字体文件，中文可能显示为方框。"
            )
        if not self.group_ids:
            logger.warning(f"[{PLUGIN_NAME}] 还没配置 chats（群），生成前请先在设置里加群")
        else:
            no_want = [c.gid for c in self.chats if not c.want]
            if no_want:
                logger.warning(
                    f"[{PLUGIN_NAME}] 这些群还没配置「想要的主题」，生成时会跳过：{no_want}"
                )

    # ------------------------------------------------------------------ 配置解析
    @staticmethod
    def _as_bool(value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(value)

    def _parse_group_ids(self) -> list[str]:
        raw = self.config.get("group_ids")
        if not raw:
            raw = self.config.get("group_id") or ""
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw]
        else:
            items = [x.strip() for x in re.split(r"[,，\s]+", str(raw))]
        return [x for x in items if x.isdigit()]

    def _parse_group_names(self) -> dict[str, str]:
        raw = str(self.config.get("group_names") or "").strip()
        mapping: dict[str, str] = {}
        for pair in re.split(r"[,，;；\n]+", raw):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            gid, name = pair.split("=", 1)
            gid, name = gid.strip(), name.strip()
            if gid and name:
                mapping[gid] = name
        return mapping

    def _apply_chats(self, raw) -> None:
        """把配置里的 chats 解析成 ChatInterest 列表，并派生 group_ids。"""
        fallback = getattr(self, "platform_id", "") or ""
        chats, skipped = parse_chats(raw, fallback)
        if not chats:
            legacy = self._parse_group_ids()
            if legacy:
                chats = [ChatInterest(gid=gid, platform=fallback) for gid in legacy]
                logger.warning(
                    f"[{PLUGIN_NAME}] 检测到旧配置 group_ids={legacy}，已临时当作 chats 使用；"
                    f"请在设置界面为每个群补上「想要的主题」"
                )
        self.chats = chats
        self.group_ids = [c.gid for c in chats]
        if skipped:
            logger.warning(f"[{PLUGIN_NAME}] 这些 chats 行没有群号，已跳过：{skipped}")

    def _chat(self, gid: str | None) -> ChatInterest | None:
        if not gid:
            return None
        for chat in self.chats:
            if chat.gid == str(gid):
                return chat
        return None

    @staticmethod
    def _chat_status_line(
        chat: ChatInterest, name: str, count: int, imgs: int, last: str
    ) -> str:
        """状态里每个群一行：谁在采集、今天多少条、上次覆盖到哪。"""
        parts = [f"{name}（{chat.gid}）"]
        parts.append(f"采集 {chat.platform or '未配置'}")
        parts.append(f"今日 {count} 条 / 带图 {imgs} 条")
        parts.append(f"上次报告覆盖到 {last or '还没生成过'}")
        if chat.want:
            parts.append(f"兴趣 {len(chat.want)} 条")
        else:
            parts.append("⚠ 未配置兴趣")
        return " · ".join(parts)

    # ------------------------------------------------------------------ 状态
    def _load_state(self) -> dict:
        if self.state_path.is_file():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] state.json 读取失败，按空状态处理: {e}")
        return {"groups": {}}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] state.json 写入失败: {e}")

    def _group_state(self, gid: str) -> dict:
        return self.state.setdefault("groups", {}).setdefault(gid, {})

    # ------------------------------------------------------------------ 基础
    def _get_bot(self, gid: str | None = None):
        platform_id = self.platform_id
        chat = self._chat(gid)
        if chat is not None and chat.platform:
            platform_id = chat.platform
        if not platform_id:
            return None
        try:
            inst = self.context.get_platform_inst(platform_id)
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
        """第一道闸：目标群的消息一律落盘（与是否禁言无关）；静默平台再终止事件。

        采集与禁言是两件事：禁言只表示"这个账号不说话"，不该影响收数据。
        所以先把消息记下来，再决定要不要让这个账号闭嘴。
        """
        platform_id = self._platform_of(event)
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if gid and gid in self.group_ids:
            await self._record(event)
        if platform_id not in self.silence_platforms:
            return
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

    # ------------------------------------------------------------------ 大白话直通
    _STRONG_INTENT = re.compile(
        r"(群聊总结|群聊日报|技术日报|技术总结|日报|聊了啥|聊了什么|聊了些啥|群里聊|群聊内容|群聊记录)"
    )
    _SUBJECT = re.compile(r"(群|群聊|聊天记录)")
    _ASK = re.compile(r"(总结|统计|整理|汇总|有哪些|有什么|说了什么|讨论了什么|聊)")
    _TIME = re.compile(
        r"(今天|今日|昨天|昨日|前天|大前天|最近|本周|这周|上周|\d+\s*天前|\d{1,2}\s*月|\d{4}-\d{1,2})"
    )

    def _digest_intent(self, text: str) -> bool:
        """判断这句话是不是在要群聊总结。宁可漏，不要误触发。"""
        text = text or ""
        if self._STRONG_INTENT.search(text):
            return True
        if not self._SUBJECT.search(text):
            return False
        return bool(self._ASK.search(text) or self._TIME.search(text))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=900)
    async def intercept_digest_intent(self, event: AstrMessageEvent):
        """私聊里用大白话要总结时直接出报告，不赌模型会不会调工具。"""
        if not self._tool_allowed(event):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith(("/", "／", "#")):
            return
        if not self._digest_intent(text):
            return
        if self._running or not self.group_ids:
            return
        if any(k in text for k in ("重发", "再发", "状态", "配置")):
            return
        for gid in self.group_ids:
            await self._group_name(gid)
        rng, gid, _rest = self._resolve_request(text)
        event.stop_event()  # 模型这段就别开口了
        logger.info(
            f"[{PLUGIN_NAME}] 大白话直通：范围={rng[2]} 群={gid or '全部'} 原文「{text}」"
        )
        asyncio.create_task(
            self._generate_and_send(event.unified_msg_origin, *rng, gid)
        )

    @filter.on_llm_request()
    async def silence_llm(self, event: AstrMessageEvent, req: ProviderRequest) -> bool:
        """第二道闸：即使事件绕过 stop_event（例如 @ 唤醒），也不让模型出手。"""
        platform_id = self._platform_of(event)
        if platform_id not in self.silence_platforms:
            # 这里放行，但要明确告诉模型：涉及群聊内容就调工具，别自己编
            if self._tool_allowed(event) and self.group_ids:
                hint = (
                    "\n\n[工具使用规则] 当用户想了解被监控群聊里聊了什么、想看技术话题、"
                    "要日报或群聊总结时，必须调用 group_digest 工具（when=时间，group=群名片段，"
                    "都留空表示只看没看过的全部群）。你本身拿不到群聊原文，不要凭记忆或想象回答，"
                    "也不要说自己看不到群消息。调用工具后不要再输出文字。"
                )
                req.system_prompt = (req.system_prompt or "") + hint
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
        # 工具已经自己把报告图发出去了，模型这句「好的我帮你生成」就不要再发了
        if event.get_extra("tech_digest_tool_sent"):
            result = event.get_result()
            if result is not None and getattr(result, "chain", None):
                result.chain = []
            return
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
    # OneBot 的历史接口一次只给最近约 200 条，想覆盖一整天只能自己攒：
    # 静默守卫能看到每一条群消息，顺手落盘；图片链接有时效，也当场下载转存。
    async def _record(self, event: AstrMessageEvent) -> None:
        if not self.use_local_store:
            return
        try:
            gid = str(event.get_group_id() or "")
            if gid not in self.group_ids:
                return
            uid = str(event.get_sender_id() or "")
            if not uid or uid in self.ignore_uids:
                return
            text = re.sub(r"\s+", " ", (event.message_str or "")).strip()

            images: list[str] = []
            if self.include_images and self.max_images > 0:
                for url in self._event_image_urls(event)[:3]:
                    saved = await self._download_image(url)
                    if saved:
                        images.append(saved)

            if not text and not images:
                return
            record = {
                "t": int(time.time()),
                "gid": gid,
                "uid": uid,
                "name": str(event.get_sender_name() or "")[:16],
                "text": text[:280],
                "platform": self._platform_of(event),
            }
            if images:
                record["imgs"] = images
            path = self.store_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            self._diag("异常", repr(e), exc=True)

    @staticmethod
    def _event_image_urls(event) -> list[str]:
        """从原始 OneBot 事件里挖图片链接（比组件转换更可靠）。"""
        urls: list[str] = []
        obj = getattr(event, "message_obj", None)
        raw = getattr(obj, "raw_message", None)
        segments = raw.get("message") if isinstance(raw, dict) else None
        if isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data") or {}
                url = data.get("url") or data.get("file")
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)
        if not urls:
            for comp in getattr(obj, "message", None) or []:
                if not isinstance(comp, AstrImage):
                    continue
                url = getattr(comp, "url", None) or getattr(comp, "file", None)
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)
        return urls

    async def _download_image(self, url: str) -> str | None:
        if not url or not str(url).startswith("http"):
            return None
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.content
        except Exception as e:
            self._diag("图片下载失败", f"{type(e).__name__}: {e}")
            return None
        if len(data) > self.max_image_mb * 1024 * 1024:
            return None
        return self._save_jpeg(data)

    def _save_jpeg(self, data: bytes) -> str | None:
        """统一转成限宽 JPEG，返回相对 data_dir 的路径。"""
        digest = hashlib.sha1(data).hexdigest()[:16]
        day_dir = self.images_dir / f"{datetime.now():%Y-%m-%d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        out = day_dir / f"{digest}.jpg"
        if out.is_file():
            return str(out.relative_to(self.data_dir))
        try:
            from PIL import Image as PILImage

            img = PILImage.open(io.BytesIO(data))
            img = img.convert("RGB")
            if img.width > 1200:
                img = img.resize((1200, max(1, round(img.height * 1200 / img.width))))
            img.save(out, "JPEG", quality=85)
            return str(out.relative_to(self.data_dir))
        except Exception as e:
            self._diag("图片转换失败", f"{type(e).__name__}: {e}")
            return None

    def _diag(self, tag: str, detail: str, exc: bool = False) -> None:
        self._diag_count += 1
        if self._diag_count <= 5:
            logger.warning(f"[{PLUGIN_NAME}] 落盘失败（{tag}）：{detail}", exc_info=exc)

    def _load_records(self, gid: str, since: float, until: float) -> list[dict]:
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
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = int(rec.get("t") or 0)
                        if ts < since or ts >= until:
                            continue
                        rec_gid = str(rec.get("gid") or (self.group_ids[0] if self.group_ids else ""))
                        if rec_gid != gid:
                            continue
                        out.append(rec)
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 读取 {path.name} 失败: {e}")
        out.sort(key=lambda r: int(r.get("t") or 0))
        out = self._dedupe_records(out)
        if len(out) > self.max_messages:
            out = out[-self.max_messages :]
        return out

    @staticmethod
    def _dedupe_records(records: list[dict]) -> list[dict]:
        """同一条消息被两个 bot 各收一次时只留一条（与平台无关）。"""
        seen: set[tuple] = set()
        out: list[dict] = []
        for rec in records:
            key = (
                str(rec.get("uid") or ""),
                int(rec.get("t") or 0),
                str(rec.get("text") or "")[:40],
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)
        return out

    # ------------------------------------------------------------------ 拉历史
    async def _fetch_history(self, gid: str, since: float) -> list[dict]:
        bot = self._get_bot()
        if bot is None or not hasattr(bot, "call_action"):
            return []
        collected: list[dict] = []
        seen: set[str] = set()
        anchor = None
        while len(collected) < self.max_messages:
            params = {"group_id": int(gid), "count": 200}
            if anchor is not None:
                params["message_seq"] = anchor
            try:
                raw = await bot.call_action("get_group_msg_history", **params)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 拉取 {gid} 历史失败: {e}")
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
            oldest_msg = min(batch, key=lambda m: int(m.get("time") or 0))
            oldest = int(oldest_msg.get("time") or 0)
            next_anchor = oldest_msg.get("message_seq") or oldest_msg.get("message_id")
            if oldest < since or next_anchor is None or added == 0:
                break
            if anchor is not None and str(next_anchor) == str(anchor):
                break
            anchor = next_anchor
            await asyncio.sleep(0.2)
        collected.sort(key=lambda m: int(m.get("time") or 0))
        records = [self._norm_onebot(gid, m) for m in collected]
        if self.include_images and self.max_images > 0:
            await self._attach_history_images(collected, records)
        return records

    async def _attach_history_images(self, messages: list[dict], records: list[dict]) -> None:
        """历史消息里的图片也试着补下来（链接有时效，能下就下）。"""
        budget = max(0, self.max_images * 3)
        for msg, rec in zip(messages, records):
            if budget <= 0:
                break
            urls: list[str] = []
            for seg in msg.get("message") or []:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data") or {}
                url = data.get("url") or data.get("file")
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)
            saved: list[str] = []
            for url in urls[:2]:
                path = await self._download_image(url)
                if path:
                    saved.append(path)
                    budget -= 1
                    if budget <= 0:
                        break
            if saved:
                rec["imgs"] = saved

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

    def _norm_onebot(self, gid: str, msg: dict) -> dict:
        sender = msg.get("sender") or {}
        record = {
            "t": int(msg.get("time") or 0),
            "gid": gid,
            "uid": str(msg.get("user_id") or ""),
            "name": str(sender.get("card") or sender.get("nickname") or "")[:16],
            "text": self._msg_text(msg)[:280],
        }
        return record

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
                parts.append("[图]")
            elif stype == "face":
                parts.append("[表情]")
            elif stype == "at":
                parts.append("@" + str(data.get("qq") or ""))
            elif stype in ("record", "video"):
                parts.append("[语音/视频]")
            elif stype == "file":
                parts.append("[文件]")
            elif stype == "forward":
                parts.append("[聊天记录]")
            else:
                parts.append(f"[{stype}]")
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    async def _group_name(self, gid: str) -> str:
        """群名自动检测。报告图里只用群名，文字回复里会带群号。"""
        if gid in self.group_name_map:
            return self.group_name_map[gid]
        bot = self._get_bot()
        if bot is None or not hasattr(bot, "call_action"):
            self._diag("取群名失败", "拿不到 bot 实例")
            return "群聊"
        # aiocqhttp 的 call_action 直接返回 data 本身，也可能被包成 {"data": ...}，两种都试
        for action, params in (
            ("get_group_info", {"group_id": int(gid)}),
            ("get_group_list", {}),
        ):
            try:
                result = await bot.call_action(action, **params)
            except Exception as e:
                self._diag(f"{action} 失败", repr(e))
                continue
            payload = result.get("data") if isinstance(result, dict) else result
            if isinstance(payload, dict) and payload.get("group_name"):
                name = str(payload["group_name"]).strip()
                if name:
                    self.group_name_map[gid] = name
                    return name
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and str(item.get("group_id")) == gid:
                        name = str(item.get("group_name") or "").strip()
                        if name:
                            self.group_name_map[gid] = name
                            return name
        self._diag("取群名失败", f"{gid} 的群信息里没有 group_name")
        return "群聊"

    # ------------------------------------------------------------------ 时间范围
    # ------------------------------------------------------------------ 宽松解析
    # 命令后面的参数不要求固定格式：从一整句里抠出「时间范围」和「群」，
    # 抠不干净的碎片再交给模型兜底（smart_parse），所以这些说法都成立：
    #   /群聊总结 今天 考古      /群聊总结 今天的考古群      /群聊总结 2026年11月5日
    #   /群聊总结 帮我看看昨天考古群聊了啥                /群聊总结 最近3天
    _STOPWORDS = (
        "帮我", "帮忙", "给我", "麻烦", "顺便", "辛苦", "请", "看一下", "看看", "看下", "瞧",
        "总结", "统计", "整理", "生成", "输出", "来一份", "一份", "一个", "报告", "日报",
        "群聊", "群里", "聊天记录", "聊天", "消息", "记录", "内容", "讨论", "啥", "什么",
        "怎么样", "如何", "关于", "里面", "里的", "那波", "这波",
        "的", "群", "里", "在", "和", "跟", "再", "来", "份", "那", "这", "把", "给",
        "了", "吧", "啊", "呢", "呀", "吗", "一下",
        "。", "，", "、", "？", "！", "；", "：", " ", "\t",
    )

    def _clean_text(self, text: str) -> str:
        out = text or ""
        for word in sorted(self._STOPWORDS, key=len, reverse=True):
            out = out.replace(word, " ")
        return re.sub(r"\s+", " ", out).strip(" .,:;!?")

    def _match_range(self, text: str):
        """在整句里找时间表达，返回 ((since, until, label, incremental), 命中的原文)。"""
        now = time.time()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        text = text or ""

        def from_day(day: datetime, matched: str):
            start = day.timestamp()
            stop = (day + timedelta(days=1)).timestamp()
            return (start, min(stop, now), f"{day:%Y-%m-%d}", False), matched

        # 具体日期：2026年11月5日 / 2026-11-05 / 11月5日 / 11-05
        m = re.search(
            r"(\d{4})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*[日号]?",
            text,
        )
        if m:
            try:
                return from_day(
                    datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))),
                    m.group(0),
                )
            except ValueError:
                pass
        for pattern in (r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?",
                        r"(?<![\d:])(\d{1,2})\s*[./\-]\s*(\d{1,2})(?![\d:])"):
            m = re.search(pattern, text)
            if not m:
                continue
            try:
                day = datetime(today.year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                continue
            if day > today:
                day = day.replace(year=today.year - 1)
            return from_day(day, m.group(0))

        # 相对日
        for words, back, label in (
            (("大前天",), 3, "大前天"),
            (("前天",), 2, "前天"),
            (("昨天", "昨日", "昨儿"), 1, "昨天"),
            (("今天", "今日", "本日", "今儿"), 0, "今天"),
        ):
            for word in words:
                if word in text:
                    rng, _matched = from_day(today - timedelta(days=back), word)
                    start, stop, _lab, inc = rng
                    return (start, stop, label, inc), word

        m = re.search(r"(\d+)\s*天前", text)
        if m:
            return from_day(today - timedelta(days=int(m.group(1))), m.group(0))

        # 最近 N 天
        m = re.search(r"(?:最近|近|过去|这几天|最近\s*的?)\s*(\d+)\s*天", text)
        if m:
            days = max(1, int(m.group(1)))
            return (now - days * 86400, now, f"最近 {days} 天", False), m.group(0)

        # 本周 / 上周
        if "上周" in text or "上星期" in text:
            monday = today - timedelta(days=today.weekday() + 7)
            return (
                monday.timestamp(),
                min((monday + timedelta(days=7)).timestamp(), now),
                "上周",
                False,
            ), ("上周" if "上周" in text else "上星期")
        for word in ("本周", "这周", "这个星期"):
            if word in text:
                monday = today - timedelta(days=today.weekday())
                return (
                    monday.timestamp(),
                    min((monday + timedelta(days=7)).timestamp(), now),
                    "本周",
                    False,
                ), word

        return None, ""

    @staticmethod
    def _fold(text: str) -> str:
        """繁转简 + 小写，让「极东」能匹配「極東」。"""
        text = text or ""
        if _zh_convert is None:
            return text.lower()
        try:
            return _zh_convert(text, "zh-hans").lower()
        except Exception:
            return text.lower()

    def _match_group(self, text: str) -> tuple[str | None, str]:
        """按群号或群名片段匹配。返回 (群号, 命中的原文)。"""
        text = text or ""
        for gid in self.group_ids:
            if gid in text:
                return gid, gid
        lowered = self._fold(text)
        best_size, best_gid, best_hit = 0, None, ""
        for gid, name in self.group_name_map.items():
            if gid not in self.group_ids:
                continue
            bare = self._fold(name)
            if bare and bare in lowered:
                return gid, name
            # 群名里任意 2~6 字的片段出现就算命中："考古" ⊂ "極東電子考古學結社"
            for size in range(min(6, len(bare)), 1, -1):
                hit = ""
                for i in range(len(bare) - size + 1):
                    piece = bare[i : i + size]
                    if piece in lowered:
                        hit = piece
                        break
                if hit:
                    if size > best_size:
                        best_size, best_gid, best_hit = size, gid, hit
                    break
        return (best_gid, best_hit) if best_gid else (None, "")

    def _parse_range(self, arg: str) -> tuple[float, float, str, bool]:
        """从一段文本里解析时间范围；解析不出就是「只看没看过的」。"""
        rng, _matched = self._match_range(arg or "")
        return rng if rng else (0.0, time.time(), "最新消息", True)

    def _resolve_request(self, text: str):
        """把一整句话解析成 (时间范围, 群号, 剩下的没认出来的碎片)。"""
        rng, range_txt = self._match_range(text)
        gid, group_txt = self._match_group(text)
        rest = text or ""
        for piece in (range_txt, group_txt):
            if piece:
                rest = rest.replace(piece, " ")
        rest = self._clean_text(rest)
        if gid and rest:
            folded_name = self._fold(self.group_name_map.get(gid, ""))
            rest = " ".join(
                w
                for w in rest.split()
                if not (self._fold(w) and self._fold(w) in folded_name)
            ).strip()
        if rng is None:
            rng = (0.0, time.time(), "最新消息", True)
        return rng, gid, rest

    async def _llm_parse_args(self, text: str) -> tuple[str, str] | None:
        """规则没认出来的碎片，交给模型翻译成 {date, group}。"""
        provider = self._get_provider()
        if provider is None or not text.strip():
            return None
        try:
            resp = await provider.text_chat(
                prompt=ARG_PROMPT.replace("{text}", text[:160]),
                system_prompt="你只做参数抽取，输出 JSON，不解释。",
                contexts=[],
            )
            out = getattr(resp, "completion_text", "") or ""
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 命令解析兜底调用模型失败: {e}")
            return None
        data = self._parse_json(out)
        if not isinstance(data, dict):
            return None
        logger.info(f"[{PLUGIN_NAME}] 命令兜底解析「{text}」→ {data}")
        return str(data.get("date") or "").strip(), str(data.get("group") or "").strip()

    def _resolve_since(self, gid: str, since: float, incremental: bool, until: float) -> float:
        if not incremental:
            return since
        last = float(self._group_state(gid).get("last_ts") or 0)
        if last > 0:
            return min(last, until)
        return until - self.days * 86400

    # ------------------------------------------------------------------ 抽取
    def _build_transcript(self, records: list[dict]) -> tuple[str, int, int]:
        lines: list[str] = []
        senders: set[str] = set()
        for rec in records:
            uid = str(rec.get("uid") or "")
            if uid in self.ignore_uids:
                continue
            text = str(rec.get("text") or "").strip()
            has_img = bool(rec.get("imgs"))
            if not text and not has_img:
                continue
            if text in ("[表情]", "[图片]"):
                continue
            name = str(rec.get("name") or uid)[:16]
            ts = int(rec.get("t") or 0)
            stamp = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"
            body = text if text else "[图]"
            if has_img and "[图]" not in body:
                body += " [图]"
            senders.add(uid)
            lines.append(f"[{stamp}] {name}: {body}")
        return "\n".join(lines), len(lines), len(senders)

    @staticmethod
    def _split_chunks(transcript: str, size: int = CHUNK_CHARS) -> list[str]:
        if len(transcript) <= size:
            return [transcript] if transcript.strip() else []
        chunks: list[str] = []
        current: list[str] = []
        length = 0
        for line in transcript.splitlines():
            if length + len(line) + 1 > size and current:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(line)
            length += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _extract_chunk(self, group_name: str, window: str, transcript: str) -> dict | None:
        provider = self._get_provider()
        if provider is None:
            logger.error(f"[{PLUGIN_NAME}] 没有可用的模型供应商")
            return None
        prompt = USER_PROMPT_TEMPLATE.format(
            group=group_name, window=window, transcript=transcript
        )
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
        cleaned = re.sub(
            r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE
        ).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            logger.warning(f"[{PLUGIN_NAME}] 模型输出里找不到 JSON：{cleaned[:200]}")
            return None
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as e:
            logger.warning(f"[{PLUGIN_NAME}] JSON 解析失败: {e}")
            return None

    @staticmethod
    def _dedupe(items: list, key_fn, cap: int) -> list:
        seen: set[str] = set()
        out: list = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = key_fn(item)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= cap:
                break
        return out

    def _merge_parts(self, parts: list[dict]) -> dict:
        merged = {
            "headline": "",
            "topics": [],
            "tools": [],
            "jobs": [],
            "ai_news": [],
            "quotes": [],
        }
        for part in parts:
            if not merged["headline"]:
                merged["headline"] = str(part.get("headline") or "").strip()
            for key in ("topics", "tools", "jobs", "ai_news", "quotes"):
                value = part.get(key)
                if isinstance(value, list):
                    merged[key].extend(value)
        merged["topics"] = self._dedupe(
            merged["topics"], lambda t: str(t.get("title") or "").strip()[:20], CAP_TOPICS
        )
        merged["tools"] = self._dedupe(
            merged["tools"], lambda t: str(t.get("name") or "").strip().lower(), CAP_TOOLS
        )
        merged["jobs"] = self._dedupe(
            merged["jobs"], lambda t: str(t.get("text") or "").strip()[:24], CAP_JOBS
        )
        merged["ai_news"] = self._dedupe(
            merged["ai_news"], lambda t: str(t.get("text") or "").strip()[:24], CAP_AI_NEWS
        )
        merged["quotes"] = self._dedupe(
            merged["quotes"], lambda t: str(t.get("content") or "").strip()[:24], CAP_QUOTES
        )
        return merged

    # ------------------------------------------------------------------ 图片挑选
    @staticmethod
    def _image_dims(path: Path) -> tuple[int, int]:
        """返回 (宽, 高)；读不出来给 (0, 0)。"""
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as img:
                return img.width, img.height
        except Exception:
            return 0, 0

    def _image_worth_keeping(
        self, rec: dict, prev: str, nxt: str, width: int, height: int
    ) -> bool:
        """尺寸/形状硬过滤 + 文字线索。宁可漏图，也不放梗图。"""
        if width * height < 250000:  # 500x500 以下基本是表情包、贴纸
            return False
        if width and height and 0.85 <= width / height <= 1.18:
            return False  # 近方形，多数是表情包
        own = str(rec.get("text") or "").lower()
        ctx = f"{prev} {nxt}".lower()
        if any(hint in own for hint in TECH_HINTS_STRONG) or "http" in own:
            return True
        # 本条没字，但前后在聊技术：只留很大的图（截图、图表通常很大）
        return any(hint in ctx for hint in TECH_HINTS) and max(width, height) >= 800

    async def _select_images(
        self, records: list[dict], topic_people: set[str]
    ) -> list[dict]:
        """有视觉模型就让它看图判类型；没有才退回尺寸/文字规则。"""
        if not self.include_images or self.max_images <= 0:
            return []
        candidates = self._image_candidates(records)
        if not candidates:
            return []
        provider = self._get_vision_provider()
        if provider is not None:
            kept = await self._vision_picks(provider, candidates)
        else:
            kept = await self._rule_picks(candidates, topic_people)
        if not kept:
            return []
        kept.sort(key=lambda c: int(c["rec"].get("t") or 0))
        return [self._image_entry(c) for c in kept[: self.max_images]]

    def _image_candidates(self, records: list[dict]) -> list[dict]:
        candidates: list[dict] = []
        for idx, rec in enumerate(records):
            files = [
                self.data_dir / rel
                for rel in (rec.get("imgs") or [])
                if (self.data_dir / rel).is_file()
            ]
            if not files:
                continue
            candidates.append(
                {
                    "rec": rec,
                    "files": files,
                    "prev": str(records[idx - 1].get("text") or "") if idx > 0 else "",
                    "next": str(records[idx + 1].get("text") or "")
                    if idx + 1 < len(records)
                    else "",
                }
            )
        return candidates[-20:]  # 太多图就别全问了，取最近的 20 条

    async def _vision_picks(
        self, provider, candidates: list[dict]
    ) -> list[dict]:
        """并发送图给视觉模型判断，只留技术相关的截图。结果会缓存到 state.json。"""
        if self.state.get("images_version") != IMG_CACHE_VERSION:
            self.state["images"] = {}
            self.state["images_version"] = IMG_CACHE_VERSION
        cache = self.state.setdefault("images", {})
        semaphore = asyncio.Semaphore(3)
        results: dict[str, dict] = {}

        async def judge(cand: dict) -> None:
            path = cand["files"][0]
            key = path.name
            cached = cache.get(key)
            if isinstance(cached, dict) and "keep" in cached:
                results[key] = cached
                return
            async with semaphore:
                verdict = await self._classify_image(provider, path)
            if verdict is None:
                return
            results[key] = verdict
            cache[key] = verdict

        await asyncio.gather(*(judge(c) for c in candidates))

        kept: list[dict] = []
        dropped: list[str] = []
        for cand in candidates:
            verdict = results.get(cand["files"][0].name)
            if verdict and verdict.get("keep"):
                cand["kind"] = verdict.get("kind") or ""
                cand["ocr"] = verdict.get("text") or ""
                kept.append(cand)
            else:
                dropped.append(f"{cand['files'][0].name}:{(verdict or {}).get('kind', '?')}")
        logger.info(
            f"[{PLUGIN_NAME}] 视觉判定 {len(candidates)} 张候选：留 {len(kept)} 张"
            f"，丢弃 {len(candidates) - len(kept)} 张（其中 {len(results)} 张有判定）"
        )
        if dropped and len(dropped) <= 8:
            logger.info(f"[{PLUGIN_NAME}] 丢弃明细: {'; '.join(dropped)}")
        return kept[-self.max_images :]

    async def _rule_picks(self, candidates: list[dict], topic_people: set[str]) -> list[dict]:
        """没有视觉模型时的退路：尺寸/形状 + 文字线索，再让模型看上下文做减法。"""
        filtered = []
        for cand in candidates:
            width, height = self._image_dims(cand["files"][0])
            if self._image_worth_keeping(
                cand["rec"], cand["prev"], cand["next"], width, height
            ):
                filtered.append(cand)
        if not filtered:
            return []
        filtered = filtered[-12:]
        picks = await self._ask_which_images(filtered)
        if picks is None:
            picks = self._heuristic_image_picks(filtered, topic_people)
        out = []
        for i in picks:
            if 0 <= i < len(filtered):
                cand = filtered[i]
                cand["kind"] = "规则筛选"
                out.append(cand)
        return out

    def _image_entry(self, cand: dict) -> dict:
        rec = cand["rec"]
        ts = int(rec.get("t") or 0)
        name = str(rec.get("name") or "?")
        stamp = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else ""
        caption = f"{name} · {stamp}"
        kind = str(cand.get("kind") or "").strip()
        if kind:
            caption += f" · {kind}"
        return {"path": str(cand["files"][0]), "caption": caption}

    async def _classify_image(self, provider, path: Path) -> dict | None:
        try:
            resp = await provider.text_chat(
                prompt=VISION_PROMPT,
                image_urls=[str(path)],
                contexts=[],
                system_prompt="你只判断图片类型，判断克制，宁严勿松。",
            )
            text = getattr(resp, "completion_text", "") or ""
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 识图失败 {path.name}: {e}")
            return None
        data = self._parse_json(text)
        if not isinstance(data, dict):
            return None
        return {
            "keep": bool(data.get("keep")),
            "kind": str(data.get("kind") or "").strip()[:12],
            "text": str(data.get("text") or "").strip()[:24],
        }

    def _get_vision_provider(self):
        pid = str(self.config.get("vision_provider_id") or "").strip()
        provider = None
        if pid:
            try:
                provider = self.context.provider_manager.get_provider_by_id(pid)
            except Exception:
                provider = None
        if provider is None:
            provider = self._get_provider()
        if provider is None:
            return None
        cfg = getattr(provider, "provider_config", None)
        modalities = cfg.get("modalities") if isinstance(cfg, dict) else None
        if isinstance(modalities, list) and "image" not in modalities:
            logger.info(
                f"[{PLUGIN_NAME}] 当前模型不支持图片输入，图片退回规则筛选"
            )
            return None
        return provider

    async def _ask_which_images(self, candidates: list[dict]) -> list[int] | None:
        provider = self._get_provider()
        if provider is None:
            return None
        lines: list[str] = []
        for i, cand in enumerate(candidates):
            rec = cand["rec"]
            ts = int(rec.get("t") or 0)
            stamp = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"
            lines.append(
                f"#{i} 发送者={rec.get('name') or '?'} 时间={stamp} 尺寸={cand.get('meta') or '?'} "
                f"本条={str(rec.get('text') or '(无文字)')[:80]} "
                f"上文={cand['prev'][-60:] or '(无)'} 下文={cand['next'][:60] or '(无)'}"
            )
        prompt = IMAGE_PICK_PROMPT.format(
            limit=self.max_images, items="\n".join(lines)
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你只关心技术内容，判断克制，不确定就不留。",
                contexts=[],
            )
            text = getattr(resp, "completion_text", "") or ""
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 图片筛选调用模型失败: {e}")
            return None
        data = self._parse_json(text)
        picks = data.get("picks") if isinstance(data, dict) else None
        if not isinstance(picks, list):
            return None
        out: list[int] = []
        for item in picks:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        logger.info(f"[{PLUGIN_NAME}] 模型从 {len(candidates)} 张候选里挑了 {len(out)} 张")
        return out

    @staticmethod
    def _heuristic_image_picks(
        candidates: list[dict], topic_people: set[str]
    ) -> list[int]:
        scored: list[tuple[int, int]] = []
        for i, cand in enumerate(candidates):
            rec = cand["rec"]
            text = (
                str(rec.get("text") or "")
                + " "
                + str(cand["prev"])
                + " "
                + str(cand["next"])
            ).lower()
            score = sum(1 for hint in TECH_HINTS if hint in text)
            if score == 0:
                continue
            if str(rec.get("name") or "") in topic_people:
                score += 2
            scored.append((score, i))
        scored.sort(key=lambda x: -x[0])
        return [i for _score, i in scored]

    # ------------------------------------------------------------------ 渲染
    def _render(self, payload: dict, images: list[dict], gid: str, label: str) -> Path | None:
        try:
            import typst
        except ImportError:
            logger.error(f"[{PLUGIN_NAME}] 未安装 typst，请 pip install typst")
            return None
        slug = re.sub(r"[^\w\-]+", "", label) or "report"
        out = self.reports_dir / (
            f"tech_digest_{gid}_{datetime.now():%Y%m%d_%H%M%S}_{slug}.png"
        )
        job = Path(tempfile.mkdtemp(prefix="tech_digest_"))
        try:
            shutil.copyfile(self.template, job / "main.typ")
            img_dir = job / "img"
            img_dir.mkdir(exist_ok=True)
            payload["images"] = []
            for i, img in enumerate(images):
                src = Path(img["path"])
                ext = src.suffix.lower() or ".jpg"
                dst = img_dir / f"{i}{ext}"
                try:
                    shutil.copyfile(src, dst)
                except Exception:
                    continue
                payload["images"].append(
                    {"path": f"img/{dst.name}", "caption": img["caption"]}
                )
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
    async def generate_group(
        self, gid: str, since: float, until: float, label: str, incremental: bool
    ) -> tuple[Path | None, str]:
        if gid not in self.group_ids:
            return None, f"群 {gid} 不在监控列表里", True
        real_since = self._resolve_since(gid, since, incremental, until)
        group_name = await self._group_name(gid)

        records = self._load_records(gid, real_since, until) if self.use_local_store else []
        if len(records) < 20:
            history = await self._fetch_history(gid, real_since)
            if history:
                seen: set[tuple] = set()
                merged: list[dict] = []
                for rec in sorted(records + history, key=lambda r: int(r.get("t") or 0)):
                    key = (
                        str(rec.get("uid") or ""),
                        int(rec.get("t") or 0) // 60,
                        str(rec.get("text") or "")[:40],
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(rec)
                records = merged[-self.max_messages :]

        if not records:
            return None, f"{group_name}：{label}没有消息", False

        transcript, msg_count, people = self._build_transcript(records)
        if not transcript.strip():
            return None, f"{group_name}：{label}只有表情和图片，没有可总结的文本", False

        chunks = self._split_chunks(transcript)
        parts: list[dict] = []
        for i, chunk in enumerate(chunks, 1):
            window = label if len(chunks) == 1 else f"{label}（第 {i}/{len(chunks)} 段）"
            part = await self._extract_chunk(group_name, window, chunk)
            if part:
                parts.append(part)
            if len(chunks) > 1:
                await asyncio.sleep(0.5)
        if not parts:
            return None, f"{group_name}：模型没有给出可用的结构化结果", True

        data = self._merge_parts(parts)
        quotes = data.get("quotes") or []
        if not self.keep_quotes:
            quotes = []

        topic_people: set[str] = set()
        for topic in data.get("topics") or []:
            value = topic.get("people")
            if isinstance(value, list):
                topic_people.update(str(v) for v in value)
            elif value:
                topic_people.add(str(value))
        images = await self._select_images(records, topic_people)

        now = datetime.now()
        payload = {
            "title": self.report_title,
            "date": now.strftime("%Y-%m-%d"),
            "group": group_name,
            "stats": {
                "line": f"{msg_count} 条消息 · {people} 人参与 · {label}",
            },
            "headline": str(data.get("headline") or "").strip(),
            "topics": self._norm_list(data.get("topics"), ("title", "detail", "people")),
            "tools": self._norm_list(data.get("tools"), ("name", "url", "note")),
            "jobs": self._norm_list(data.get("jobs"), ("text", "who")),
            "ai_news": self._norm_list(data.get("ai_news"), ("text",)),
            "quotes": self._norm_list(quotes, ("content", "sender", "reason")),
            "footer": f"生成于 {now:%Y-%m-%d %H:%M} · {PLUGIN_NAME} · 本报告不会发到群里",
        }

        path = self._render(payload, images, gid, label)
        if path is None:
            return None, f"{group_name}：排版成图失败，看日志", True

        if incremental:
            self._group_state(gid)["last_ts"] = int(until)
        self._group_state(gid)["last_report"] = path.name
        self._save_state()

        info = (
            f"[{group_name}] {label}：{msg_count} 条 / {people} 人 · "
            f"话题 {len(payload['topics'])} · 资源 {len(payload['tools'])} · "
            f"就业 {len(payload['jobs'])} · AI {len(payload['ai_news'])} · "
            f"金句 {len(payload['quotes'])} · 图 {len(payload['images'])}"
            + (f" · {len(chunks)} 段" if len(chunks) > 1 else "")
        )
        logger.info(f"[{PLUGIN_NAME}] {info} -> {path.name}")
        self._prune_old_files()
        return path, info, False

    async def generate_all(
        self, since: float, until: float, label: str, incremental: bool, only_gid: str | None = None
    ) -> list[tuple[str, Path | None, str, bool]]:
        """返回 (群号, 图路径或 None, 说明, 是否报错)。"""
        results: list[tuple[str, Path | None, str, bool]] = []
        targets = [only_gid] if only_gid else list(self.group_ids)
        for gid in targets:
            try:
                path, info, is_error = await self.generate_group(
                    gid, since, until, label, incremental
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 生成 {gid} 失败: {e}", exc_info=True)
                path, info, is_error = None, f"群 {gid} 生成异常：{e}", True
            results.append((gid, path, info, is_error))
        return results

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

    async def _push(self, path: Path) -> bool:
        """只发报告图，不带任何说明文字。"""
        if not self.push_enabled or not self.push_umo:
            return False
        try:
            ok = await self.context.send_message(
                self.push_umo, MessageChain([AstrImage.fromFileSystem(str(path))])
            )
            if ok:
                logger.info(f"[{PLUGIN_NAME}] 已推送 {path.name} -> {self.push_umo}")
            return bool(ok)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 推送失败: {e}", exc_info=True)
            return False

    async def _push_text(self, text: str) -> bool:
        """只有出错时才发文字。"""
        if not self.push_enabled or not self.push_umo:
            return False
        try:
            return bool(
                await self.context.send_message(
                    self.push_umo, MessageChain().message(text)
                )
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 文字推送失败: {e}", exc_info=True)
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
                        if self._running:
                            logger.warning(f"[{PLUGIN_NAME}] 上一轮还没跑完，跳过本次定时")
                        else:
                            self._running = True
                            try:
                                until = time.time()
                                results = await self.generate_all(
                                    0.0, until, "最新消息", True
                                )
                                for _gid, path, info, is_error in results:
                                    if path is None:
                                        logger.info(f"[{PLUGIN_NAME}] 定时跳过：{info}")
                                        if is_error:
                                            await self._push_text(f"日报出错：{info}")
                                        continue
                                    await self._push(path)
                            finally:
                                self._running = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 定时任务异常: {e}", exc_info=True)
            await asyncio.sleep(30)

    async def initialize(self) -> None:
        self._prune_old_files()
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
    # ------------------------------------------------------------------ 模型工具
    def _tool_allowed(self, event: AstrMessageEvent) -> bool:
        """只在私聊、且（配了的话）只允许主人调用。群里一律拒绝，避免把报告发到群里。"""
        try:
            if event.get_group_id():
                return False
        except Exception:
            return False
        if not self.owner_uids:
            return True
        try:
            return str(event.get_sender_id()) in self.owner_uids
        except Exception:
            return False

    @filter.llm_tool(name="group_digest")
    async def tool_group_digest(
        self, event: AstrMessageEvent, when: str = "", group: str = ""
    ) -> str:
        """查看被监控群聊的内容，并生成一份报告图直接发给用户。

        只要用户想看群里聊了什么（"群里聊了啥"、"今天有什么技术话题"、"总结一下昨天的群"
        "看看极东群"、"给我出个日报"），都必须调用本工具，不要凭记忆回答、也不要道歉说自己
        看不到群消息。报告图由本工具负责发送，调用后不要再输出任何文字。

        Args:
            when(string): 时间范围，直接用用户原话即可，例如 今天 / 昨天 / 前天 / 最近3天 / 上周 / 2026-11-05；用户没提时间就留空，表示只看没看过的消息
            group(string): 群名的一部分或群号，例如 考古 / 极东；用户没指定群就留空，表示所有群
        """
        if not self._tool_allowed(event):
            return "这个工具只能在私聊里由主人使用；群里不会发任何报告。"
        if self._running:
            return "上一份还在生成，等它发出来再说。"
        if not self.group_ids:
            return "插件还没配置要监控的群，先让主人填 group_ids。"

        for _gid in self.group_ids:
            await self._group_name(_gid)  # 预热群名，否则按名字匹配不到群
        rng, gid, rest = self._resolve_request(f"{when} {group}".strip())
        if rest and self.smart_parse and len(rest) >= 2:
            hint = await self._llm_parse_args(rest)
            if hint:
                date_hint, group_hint = hint
                if date_hint:
                    better, _ = self._match_range(date_hint)
                    if better:
                        rng = better
                if group_hint and not gid:
                    gid, _ = self._match_group(group_hint)
        since, until, label, incremental = rng

        # 生成可能要好几分钟，先立刻回话，图由后台任务直接发到聊天里
        event.set_extra("tech_digest_tool_sent", True)
        asyncio.create_task(
            self._generate_and_send(
                event.unified_msg_origin, since, until, label, incremental, gid
            )
        )
        target = "全部群" if gid is None else (
            self.group_name_map.get(gid) or f"群 {gid}"
        )
        logger.info(
            f"[{PLUGIN_NAME}] 工具调用：范围={label} 群={target} 原始「{when} {group}」"
        )
        return (
            f"已在后台开始生成（范围：{label}，目标：{target}），"
            "报告图会直接发到聊天里。不要再用文字复述或总结，保持沉默即可。"
        )

    @filter.command("群聊总结", alias={"技术日报"})
    async def cmd_digest(self, event: AstrMessageEvent):
        """生成技术情报日报（成功时不发任何提示，只发那张图）。

        /群聊总结            只看没看过的（自上次报告以来的新消息）
        /群聊总结 今天        今天 00:00 到现在
        /群聊总结 昨天        昨天整天
        /群聊总结 2026-11-05  指定日期
        /群聊总结 3天前       指定日期（相对）
        /群聊总结 今天 群名    只跑名字匹配的那个群
        /群聊总结 重发        重发最近一份
        /群聊总结 状态        配置与本地库情况
        """
        arg = re.sub(
            r"^\s*/?\s*(?:群聊总结|技术日报)\s*",
            "",
            event.message_str or "",
            flags=re.IGNORECASE,
        ).strip()

        for gid in self.group_ids:
            await self._group_name(gid)  # 先把群名填进缓存，下面才能按群名匹配

        norm = self._clean_text(arg)

        if norm in ("状态", "配置", "情况", "设置", "详情"):
            yield event.plain_result("\n".join(await self._status_lines()))
            return
        if norm in ("重发", "再发", "上一份", "最新一份", "最后一份", "刚才那份"):
            latest = self._latest_report()
            if latest is None:
                yield event.plain_result("还没有生成过日报。")
                return
            yield event.image_result(str(latest))
            return

        rng, only_gid, rest = self._resolve_request(arg)
        # 规则没吃掉的碎片，问一次模型（可在配置里关掉）
        if rest and self.smart_parse and len(rest) >= 2:
            hint = await self._llm_parse_args(rest)
            if hint:
                date_hint, group_hint = hint
                if date_hint:
                    better, _ = self._match_range(date_hint)
                    if better:
                        rng = better
                if group_hint and not only_gid:
                    better_gid, _ = self._match_group(group_hint)
                    if better_gid:
                        only_gid = better_gid
        since, until, label, incremental = rng

        if self._running:
            yield event.plain_result("上一份还在生成，跑完会直接把图发给你。")
            return
        if not self.group_ids:
            yield event.plain_result("还没配置要监控的群（group_ids）。")
            return
        logger.info(
            f"[{PLUGIN_NAME}] 命令解析：范围={label} 增量={incremental} "
            f"群={only_gid or '全部'} 原始「{arg}」"
        )
        # 成功路径全程不吭声，只发图；失败才回文字
        asyncio.create_task(
            self._generate_and_send(
                event.unified_msg_origin, since, until, label, incremental, only_gid
            )
        )

    async def _status_lines(self) -> list[str]:
        lines: list[str] = []
        for gid in self.group_ids:
            name = await self._group_name(gid)
            now = time.time()
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            today_records = self._load_records(gid, today_start, now)
            last_ts = int(float(self._group_state(gid).get("last_ts") or 0))
            last = (
                datetime.fromtimestamp(last_ts).strftime("%m-%d %H:%M")
                if last_ts
                else "还没生成过"
            )
            imgs = sum(1 for r in today_records if r.get("imgs"))
            chat = self._chat(gid)
            if chat is not None:
                lines.append(
                    self._chat_status_line(chat, name, len(today_records), imgs, last)
                )
            else:
                lines.append(
                    f"{name}（{gid}）：今日 {len(today_records)} 条 / 带图 {imgs} 条 · "
                    f"上次报告覆盖到 {last}"
                )
        lines.append(
            f"平台 {self.platform_id or '未配置'} · 静默 {'、'.join(self.silence_platforms) or '无'}"
            f"（拦下事件 {self._silenced} / LLM {self._llm_blocked} / 回复 {self._result_blocked}）"
        )
        latest = self._latest_report()
        lines.append(f"最近报告：{latest.name if latest else '无'}")
        lines.append(
            f"定时 {self.daily_time or '关闭'} · 推送 {self.push_umo or '关闭'} · "
            f"上限 {self.max_messages} 条 · 图片上限 {self.max_images} 张 · "
            f"数据保留 {self.keep_days} 天"
        )
        return lines

    async def _generate_and_send(
        self,
        umo: str,
        since: float,
        until: float,
        label: str,
        incremental: bool,
        only_gid: str | None,
    ) -> None:
        self._running = True
        try:
            results = await self.generate_all(since, until, label, incremental, only_gid)
            for _gid, path, info, is_error in results:
                if path is None:
                    logger.info(f"[{PLUGIN_NAME}] 跳过：{info}")
                    if is_error:
                        await self.context.send_message(
                            umo, MessageChain().message(f"出错了：{info}")
                        )
                    continue
                await self.context.send_message(
                    umo, MessageChain([AstrImage.fromFileSystem(str(path))])
                )
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 生成流程异常: {e}", exc_info=True)
            try:
                await self.context.send_message(umo, MessageChain().message(f"生成失败：{e}"))
            except Exception:
                pass
        finally:
            self._running = False

    # ------------------------------------------------------------------ 过期清理
    def _prune_old_files(self) -> None:
        """只保留最近 keep_days 天的消息与图片，防止磁盘无上限增长。"""
        cutoff_day = (datetime.now() - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        removed_msgs = removed_imgs = 0
        for path in self.store_dir.glob("*.jsonl"):
            if path.stem < cutoff_day:
                try:
                    path.unlink()
                    removed_msgs += 1
                except Exception:
                    pass
        for day_dir in self.images_dir.iterdir():
            if not day_dir.is_dir() or day_dir.name >= cutoff_day:
                continue
            try:
                removed_imgs += sum(1 for _ in day_dir.iterdir())
                shutil.rmtree(day_dir, ignore_errors=True)
            except Exception:
                pass
        if removed_msgs or removed_imgs:
            logger.info(
                f"[{PLUGIN_NAME}] 清理过期数据（保留 {self.keep_days} 天）："
                f"消息文件 {removed_msgs} 个、图片 {removed_imgs} 张"
            )
        # 顺手把报告也按同样天数清理，避免 reports/ 无限膨胀
        removed_reports = 0
        cutoff_ts = (datetime.now() - timedelta(days=self.keep_days)).timestamp()
        for path in self.reports_dir.glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink()
                    removed_reports += 1
            except Exception:
                pass
        if removed_reports:
            logger.info(f"[{PLUGIN_NAME}] 清理旧报告 {removed_reports} 份")
        # 图片没了，判定缓存也别留着
        alive = {p.name for p in self.images_dir.rglob("*.jpg")}
        cache = self.state.get("images")
        if isinstance(cache, dict) and cache:
            stale = [k for k in cache if k not in alive]
            for key in stale:
                cache.pop(key, None)
            if stale:
                self._save_state()
                logger.info(f"[{PLUGIN_NAME}] 清理失效的判图缓存 {len(stale)} 条")

    def _latest_report(self) -> Path | None:
        files = sorted(
            (p for p in self.reports_dir.glob("*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        return files[-1] if files else None

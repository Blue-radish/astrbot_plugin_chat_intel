"""技术情报日报 —— 多群技术向日报，图片留存，本地排版，静默投递。

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

PLUGIN_NAME = "astrbot_plugin_tech_digest"

# 报告里各板块的条数上限（合并多个分片后仍要克制）
CAP_TOPICS = 8
CAP_TOOLS = 10
CAP_JOBS = 8
CAP_AI_NEWS = 6
CAP_QUOTES = 4

CHUNK_CHARS = 55000  # 单次喂给模型的转录上限，超了切片后合并

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


@register(PLUGIN_NAME, "Ray", "多群技术情报日报：只留技术向内容，本地排版成图，静默私聊投递", "1.1.0")
class TechDigestPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.group_ids = self._parse_group_ids()
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
        self.report_title = str(self.config.get("report_title") or "技术情报日报").strip()
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

        ignore = str(self.config.get("ignore_uids") or "")
        self.ignore_uids = {u.strip() for u in ignore.split(",") if u.strip()}

        silence = self.config.get("silence_platforms") or ""
        if isinstance(silence, str):
            silence = [x.strip() for x in silence.split(",") if x.strip()]
        elif isinstance(silence, (list, tuple)):
            silence = [str(x).strip() for x in silence if str(x).strip()]
        self.platform_id = str(self.config.get("platform_id") or "").strip()
        self.silence_platforms = silence or ([self.platform_id] if self.platform_id else [])
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
            logger.warning(f"[{PLUGIN_NAME}] 还没配置 group_ids，生成前请先填群号")

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
        """第一道闸：静默平台的事件直接终止，同时把目标群的消息落盘。"""
        platform_id = self._platform_of(event)
        if platform_id not in self.silence_platforms:
            return
        await self._record(event)
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
        if len(out) > self.max_messages:
            out = out[-self.max_messages :]
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
    def _parse_range(self, arg: str) -> tuple[float, float, str, bool]:
        """返回 (since, until, 界面显示的范围名, 是否增量模式)。"""
        now = time.time()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        arg = (arg or "").strip()

        if not arg:
            return 0.0, now, "最新消息", True

        def day_bounds(day: datetime, label: str):
            start = day.timestamp()
            end = (day + timedelta(days=1)).timestamp()
            return start, min(end, now), label, False

        if arg in ("今天", "今日", "today"):
            return day_bounds(today, "今天")
        if arg in ("昨天", "昨日", "yesterday"):
            return day_bounds(today - timedelta(days=1), "昨天")
        if arg in ("前天",):
            return day_bounds(today - timedelta(days=2), "前天")

        m = re.fullmatch(r"(\d+)\s*天前", arg)
        if m:
            n = int(m.group(1))
            day = today - timedelta(days=n)
            return day_bounds(day, f"{day:%Y-%m-%d}")

        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", arg)
        if m:
            try:
                day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return 0.0, now, "最新消息", True
            return day_bounds(day, f"{day:%Y-%m-%d}")

        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", arg)
        if m:
            try:
                day = datetime(today.year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                return 0.0, now, "最新消息", True
            if day > today:
                day = day.replace(year=today.year - 1)
            return day_bounds(day, f"{day:%Y-%m-%d}")

        return 0.0, now, "最新消息", True

    @staticmethod
    def _looks_like_range(token: str) -> bool:
        t = (token or "").strip()
        if not t:
            return False
        if t in ("今天", "今日", "today", "昨天", "昨日", "yesterday", "前天"):
            return True
        return bool(
            re.fullmatch(r"\d+\s*天前", t)
            or re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", t)
            or re.fullmatch(r"\d{1,2}[-/.]\d{1,2}", t)
        )

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
        """先按尺寸与文字线索硬过滤，再让模型看上下文做最后一道减法。"""
        if not self.include_images or self.max_images <= 0:
            return []
        candidates: list[dict] = []
        for idx, rec in enumerate(records):
            files = [
                self.data_dir / rel
                for rel in (rec.get("imgs") or [])
                if (self.data_dir / rel).is_file()
            ]
            if not files:
                continue
            width, height = self._image_dims(files[0])
            prev_text = str(records[idx - 1].get("text") or "") if idx > 0 else ""
            next_text = (
                str(records[idx + 1].get("text") or "")
                if idx + 1 < len(records)
                else ""
            )
            if not self._image_worth_keeping(rec, prev_text, next_text, width, height):
                continue
            candidates.append(
                {
                    "rec": rec,
                    "files": files,
                    "meta": f"{width}x{height}",
                    "prev": prev_text,
                    "next": next_text,
                }
            )
        if not candidates:
            return []
        candidates = candidates[-12:]

        picks = await self._ask_which_images(candidates)
        if picks is None:
            picks = self._heuristic_image_picks(candidates, topic_people)

        selected: list[dict] = []
        for i in picks:
            if not 0 <= i < len(candidates):
                continue
            cand = candidates[i]
            rec = cand["rec"]
            ts = int(rec.get("t") or 0)
            name = str(rec.get("name") or "?")
            stamp = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else ""
            selected.append({"path": str(cand["files"][0]), "caption": f"{name} · {stamp}"})
            if len(selected) >= self.max_images:
                break
        selected.sort(key=lambda i: i["caption"])
        return selected

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
            return None, f"群 {gid} 不在监控列表里"
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
            return None, f"{group_name}：{label}没有消息"

        transcript, msg_count, people = self._build_transcript(records)
        if not transcript.strip():
            return None, f"{group_name}：{label}只有表情和图片，没有可总结的文本"

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
            return None, f"{group_name}：模型没有给出可用的结构化结果"

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
            return None, f"{group_name}：排版成图失败，看日志"

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
        return path, info

    async def generate_all(
        self, since: float, until: float, label: str, incremental: bool, only_gid: str | None = None
    ) -> list[tuple[str, Path | None, str]]:
        results: list[tuple[str, Path | None, str]] = []
        targets = [only_gid] if only_gid else list(self.group_ids)
        for gid in targets:
            try:
                path, info = await self.generate_group(gid, since, until, label, incremental)
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 生成 {gid} 失败: {e}", exc_info=True)
                path, info = None, f"群 {gid}：生成异常 {e}"
            results.append((gid, path, info))
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

    async def _push(self, path: Path, caption: str) -> bool:
        if not self.push_enabled or not self.push_umo:
            return False
        try:
            ok = await self.context.send_message(
                self.push_umo, MessageChain([AstrImage.fromFileSystem(str(path))])
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
                        if self._running:
                            logger.warning(f"[{PLUGIN_NAME}] 上一轮还没跑完，跳过本次定时")
                        else:
                            self._running = True
                            try:
                                until = time.time()
                                results = await self.generate_all(
                                    0.0, until, "最新消息", True
                                )
                                for _gid, path, info in results:
                                    if path is None:
                                        logger.warning(f"[{PLUGIN_NAME}] 定时生成失败: {info}")
                                        continue
                                    await self._push(path, info)
                            finally:
                                self._running = False
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
        """生成技术情报日报。

        /techdigest            只看没看过的（自上次报告以来的新消息）
        /techdigest 今天        今天 00:00 到现在
        /techdigest 昨天        昨天整天
        /techdigest 2026-11-05  指定日期
        /techdigest 3天前       指定日期（相对）
        /techdigest 昨天 群名    只跑名字匹配的那个群
        /techdigest last       重发最近一份
        /techdigest status     配置与本地库状态
        """
        arg = re.sub(
            r"^\s*/?\s*(?:techdigest|技术日报)\s*",
            "",
            event.message_str or "",
            flags=re.IGNORECASE,
        ).strip()

        tokens = [t for t in re.split(r"\s+", arg) if t]

        if tokens and tokens[0] in ("status", "状态"):
            yield event.plain_result("\n".join(await self._status_lines()))
            return
        if tokens and tokens[0] in ("last", "最新一份", "上一份"):
            latest = self._latest_report()
            if latest is None:
                yield event.plain_result("还没有生成过日报。")
                return
            stamp = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m-%d %H:%M")
            yield event.image_result(str(latest))
            yield event.plain_result(f"最近一份：{stamp} 生成（{latest.name}）")
            return

        for gid in self.group_ids:
            await self._group_name(gid)  # 先把群名填进缓存，下面才能按群名过滤
        only_gid, range_tokens = self._split_group_filter(tokens)
        range_arg = next(
            (t for t in range_tokens if self._looks_like_range(t)),
            "",
        )
        since, until, label, incremental = self._parse_range(range_arg)

        if self._running:
            yield event.plain_result("正在生成中，跑完会直接发给你。")
            return
        targets = [only_gid] if only_gid else list(self.group_ids)
        names = [f"{await self._group_name(g)}（{g}）" for g in targets]
        if not names:
            yield event.plain_result("还没配置要监控的群（group_ids）。")
            return
        yield event.plain_result(
            f"开工：{'、'.join(names)} · 范围 {label}。"
            "消息多的时候要过几遍模型，可能要几分钟。"
        )
        asyncio.create_task(
            self._generate_and_send(
                event.unified_msg_origin, since, until, label, incremental, only_gid
            )
        )

    def _split_group_filter(self, tokens: list[str]) -> tuple[str | None, list[str]]:
        if not tokens:
            return None, tokens
        for i, token in enumerate(tokens):
            lowered = token.lower()
            for gid, name in self.group_name_map.items():
                if gid not in self.group_ids:
                    continue
                bare = name.lower()
                if gid == token or (bare and (bare in lowered or lowered in bare)):
                    rest = tokens[:i] + tokens[i + 1 :]
                    return gid, rest
            for gid in self.group_ids:
                if gid == token:
                    rest = tokens[:i] + tokens[i + 1 :]
                    return gid, rest
        return None, tokens

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
            f"上限 {self.max_messages} 条 · 图片上限 {self.max_images} 张"
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
            for _gid, path, info in results:
                if path is None:
                    await self.context.send_message(umo, MessageChain().message(f"跳过：{info}"))
                    continue
                await self.context.send_message(
                    umo, MessageChain([AstrImage.fromFileSystem(str(path))])
                )
                await asyncio.sleep(1)
                await self.context.send_message(umo, MessageChain().message(info))
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 生成流程异常: {e}", exc_info=True)
            try:
                await self.context.send_message(umo, MessageChain().message(f"生成失败：{e}"))
            except Exception:
                pass
        finally:
            self._running = False

    def _latest_report(self) -> Path | None:
        files = sorted(
            (p for p in self.reports_dir.glob("*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        return files[-1] if files else None

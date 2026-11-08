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
import os
import re
import shutil
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_chat_intel"

# 追问/查证类说法：出现这些就不许被「生成」拦截器劫走，交给模型挑读取工具。
# 「日报第二条有什么依据」以前会命中 digest 动作词（有什么），于是又生成了一份新报告。
RE_FOLLOWUP = re.compile(
    r"第[一二三四五六七八九十百\d]+\s*[条个]|这条|那条|哪条|这个说法|那个说法|话题|依据|原话|引用|"
    r"出处|来源|靠谱|可信|准确|解释|展开|具体点|后来|后续|验证|有人试过|实测|猜测|瞎编|"
    r"什么意思|为什么这么说|结论"
)
QUERY_PAGE = 20            # 一次最多显示多少条命中
QUERY_CTX_BEFORE = 1       # 命中消息往前带几条上下文
QUERY_CTX_AFTER = 2        # 往后带几条（「后来呢」靠这个）
QUERY_SCAN_LIMIT = 50000   # 查询扫描上限（和「总结最多吃多少条」是两件事）


try:  # 插件以包形式被 AstrBot 加载时走相对导入
    from . import imaging  # 长图切段横排（出图脚本共用同一实现）
    from . import reports  # 报告的结构化存档（讨论报告、回查原话要用）
    from .intent import detect_intent
    from .interest import (
        ChatInterest,
        build_digest_prompt,
        build_name_map,
        sanitize_name,
        build_vision_prompt,
        interest_fingerprint,
        merge_topics,
        normalize_topics,
        parse_chats,
        split_names,
        topic_members,
        topic_time_range,
    )
except ImportError:  # pragma: no cover - 单测按文件路径加载模块
    import imaging  # noqa: F401
    import reports  # noqa: F401
    from intent import detect_intent
    from interest import (
        ChatInterest,
        build_digest_prompt,
        build_name_map,
        sanitize_name,
        build_vision_prompt,
        interest_fingerprint,
        merge_topics,
        normalize_topics,
        parse_chats,
        split_names,
        topic_members,
        topic_time_range,
    )

try:  # 简繁归一化：群名可能是繁体，用户打的是简体
    from zhconv import convert as _zh_convert
except Exception:  # pragma: no cover - 没装就退化成原文匹配
    _zh_convert = None

# 报告里各板块的条数上限（合并多个分片后仍要克制）
# 历史分页最多翻多少页（防止极端情况下一直翻）
MAX_HISTORY_PAGES = 300
# 排队上限：正在生成时最多再排两份（交互 + 定时），再多就明说跑了
QUEUE_MAX = 2
# 单段总结失败重试次数（含首次）
CHUNK_RETRY = 2

# 每个话题最多问几张图（问得越多越贵，缓存会挡住重复判定）
PER_TOPIC_ASK = 6
# 一次生成最多问几张图（跨话题总量，第一次跑没有缓存时的花费上限）
MAX_JUDGE_PER_RUN = 30

IMG_CACHE_VERSION = 5  # 判据说辞/判据维度变化时 +1，旧缓存作废
# 4：判图改用每群兴趣；5：判图提示词改成「与话题和兴趣相关」，缓存键带上话题

CHUNK_CHARS = 55000  # 单次喂给模型的转录上限，超了切片后合并
MSG_TEXT_CAP = 800  # 单条消息保留多少字（含换行）；超了截断并留标记

ARG_PROMPT = """把用户这句话里的「时间范围」和「群」抽出来，只输出 JSON：
{"date": "", "group": ""}

date 写成这些形式之一：今天 / 昨天 / 前天 / 大前天 / 本周 / 上周 / 最近3天 / 3天前 / 2026-11-05。
没提到时间就留空字符串（表示"只看没看过的"）。

group 写群名里最有辨识度的一小段（例如"考古"），没提到群就留空字符串。
不要解释、不要代码块。

用户的话：{text}"""



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

SYSTEM_PROMPT = """你在给群友写一份私人情报简报。文字克制、具体、可执行。"""

@register(PLUGIN_NAME, "Ray", "群聊情报：每个群一份标签，把群友聊出来的话题整理成一张长图，视觉模型筛图，本地排版，静默私聊投递；报告可回头讨论、按证据回应追问", "2.2.9")
class TechDigestPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.chats: list[ChatInterest] = []
        self._queue: deque = deque(maxlen=QUEUE_MAX)
        self.group_ids: list[str] = []
        self.group_name_map: dict[str, str] = {}
        self.days = self._num_cfg("days", 1.0, lo=0.0, as_float=True)
        self.max_messages = self._num_cfg("max_messages", 10000, lo=50)
        self.daily_time, self._daily_cfg_error = self.normalize_daily_time(
            self.config.get("daily_time")
        )
        self.push_enabled = self._as_bool(self.config.get("push_enabled"), False)
        # 推送目标用「发信账号 + 收件人 QQ」两个框拼，旧配置里整串的 push_umo 继续认
        self.push_umo, self._push_cfg_error = self.build_push_target(self.config)
        self.report_title = str(self.config.get("report_title") or "群聊情报").strip()
        self.ppi = self._num_cfg("render_ppi", 180, lo=72)

        self.include_images = self._as_bool(self.config.get("include_images"), True)
        # 0 是合法值（= 不配图），不能当成缺省
        self.max_images = self._num_cfg("max_images", 4, lo=0)
        self.max_image_mb = self._num_cfg("max_image_mb", 8, lo=1)
        self.keep_days = self._num_cfg("keep_days", 7, lo=1)
        self.smart_parse = self._as_bool(self.config.get("smart_parse"), True)

        owners = str(self.config.get("owner_uids") or "")
        self.owner_uids = {u.strip() for u in re.split(r"[,，\s]+", owners) if u.strip()}

        ignore = str(self.config.get("ignore_uids") or "")
        self.ignore_uids = {u.strip() for u in ignore.split(",") if u.strip()}

        # 静默必须显式填；读取账号现在是每群必填，不再有全局默认账号
        self.silence_platforms = self._parse_silence(self.config)
        self.legacy_platform_id = str(self.config.get("platform_id") or "").strip()
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
            (self.data_dir / "fonts")
        )
        self.font_dir.mkdir(parents=True, exist_ok=True)
        self.template = Path(__file__).parent / "templates" / "report.typ"
        self.state_path = self.data_dir / "state.json"
        self.state = self._load_state()
        # 老配置的一次性迁移（必须在读状态之后：标记存在状态文件里）
        self._migrate_legacy_config()

        self._transcript_cache: tuple[list[dict], str] = ([], "")
        self._task: asyncio.Task | None = None
        self._last_run_date: str | None = None
        self._running = False
        self._running_since = 0.0

        fonts = [
            f.name
            for f in self.font_dir.iterdir()
            if f.suffix.lower() in (".ttf", ".otf", ".ttc")
        ]
        logger.info(
            f"[{PLUGIN_NAME}] 群={self.group_ids or '(未配置)'} "
            f"定时={self.daily_time or '关闭'} 推送={self.push_umo or '关闭'} "
            f"图片={'开' if self.include_images else '关'} 上限={self.max_messages} 条 "
            f"静默平台={self.silence_platforms or '无'} 字体={len(fonts)} 个"
        )
        if not fonts:
            logger.warning(
                f"[{PLUGIN_NAME}] {self.font_dir} 里没有字体文件，中文可能显示为方框。"
            )
        if self.legacy_platform_id:
            logger.warning(
                f"[{PLUGIN_NAME}] 配置里的 platform_id（{self.legacy_platform_id}）已经不用了："
                "读取账号改成每个群各自填「读取账号」，不再有全局默认账号"
            )
        missing_platform = [c.gid for c in self.chats if not c.platform]
        if missing_platform:
            logger.warning(
                f"[{PLUGIN_NAME}] 这些群没填「读取账号」（必填），生成时会跳过：{missing_platform}"
            )
        if self._push_cfg_error:
            logger.error(f"[{PLUGIN_NAME}] 推送设置有问题：{self._push_cfg_error}")
        if self._daily_cfg_error:
            logger.error(f"[{PLUGIN_NAME}] {self._daily_cfg_error}")
        if not self.group_ids:
            logger.warning(
                f"[{PLUGIN_NAME}] 还没配置要总结的群（chats），现在是待配置状态；"
                "到设置页「要总结的群」里加一行（群号 + 关心的标签）就能用"
            )
        else:
            no_want = [c.gid for c in self.chats if not c.tags_pos]
            if no_want:
                logger.warning(
                    f"[{PLUGIN_NAME}] 这些群还没配置「关心的标签」，生成时会跳过：{no_want}"
                )

    # ------------------------------------------------------------------ 配置解析
    def _num_cfg(self, key: str, default, lo=None, hi=None, as_float: bool = False):
        """读数字配置：只有缺失/空串/类型不对才用默认值。

        注意 0 是合法值（例如 max_images=0 表示不配图），不能用 `or default` 吞掉。
        """
        raw = self.config.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default
        try:
            value = float(raw) if as_float else int(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"[{PLUGIN_NAME}] 配置 {key}={raw!r} 不是数字，按默认值 {default} 处理"
            )
            return default
        if lo is not None and value < lo:
            logger.warning(f"[{PLUGIN_NAME}] 配置 {key}={value} 超出范围，按 {lo} 处理")
            value = lo
        if hi is not None and value > hi:
            logger.warning(f"[{PLUGIN_NAME}] 配置 {key}={value} 超出范围，按 {hi} 处理")
            value = hi
        return float(value) if as_float else int(value)

    @staticmethod
    def normalize_daily_time(raw) -> tuple[str, str]:
        """把「9:00」补成「09:00」；看不懂就返回 ("", 原因)，别让它静默永不触发。"""
        text = str(raw or "").strip()
        if not text:
            return "", ""
        matched = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", text)
        if not matched:
            return "", f"定时时间「{text}」看不懂，请写成 HH:MM（比如 09:00），现在不定时生成"
        hour, minute = int(matched.group(1)), int(matched.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return "", f"定时时间「{text}」不是有效时刻，现在不定时生成"
        return f"{hour:02d}:{minute:02d}", ""

    @staticmethod
    def _parse_silence(config: dict) -> list[str]:
        """解析静默名单。静默与「读历史账号」互不联动，必须显式配置。"""
        config = config or {}
        raw = config.get("silence_platforms") or ""
        if isinstance(raw, str):
            return [x.strip() for x in raw.split(",") if x.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]
        return []

    @staticmethod
    def build_push_target(config: dict) -> tuple[str, str]:
        """把「发信账号 + 收件人 QQ 号」拼成会话 id，返回 (会话 id, 错误说明)。

        两个输入框是给人和给校验看的；拼出来的会话 id 才交给推送层。
        旧的整串 push_umo 不在这里兜底：宿主加载配置时会给缺失字段补空串，
        靠「字段存不存在」区分不了「老配置」和「用户主动清空」，
        所以老配置走 migrate_push_target() 一次性搬进这两个字段。
        """
        config = config or {}
        platform = str(config.get("push_platform") or "").strip()
        uid = str(config.get("push_uid") or "").strip()
        if not platform and not uid:
            return "", ""
        if not platform:
            return "", "填了收件人 QQ 号，但没填「用哪个机器人发」"
        if not uid:
            return "", "填了发信账号，但没填收件人 QQ 号"
        if not uid.isdigit():
            return "", f"收件人 QQ 号「{uid}」不是纯数字"
        return f"{platform}:FriendMessage:{uid}", ""

    @staticmethod
    def migrate_push_target(config: dict, state: dict) -> str:
        """老配置的一次性迁移：把整串 push_umo 拆进两个可见字段。

        宿主（AstrBotConfig）加载时会按 schema 补齐默认值，所以「新字段存在」不代表用户
        填过；只能用状态文件里的一次性标记判断是否已经迁过。
        返回迁移后的推送目标（没有就是空串）。
        """
        config = config or {}
        if state is None:
            state = {}
        if state.get("config_migrated_v224"):
            return TechDigestPlugin.build_push_target(config)[0]
        legacy = str(config.get("push_umo") or "").strip()
        platform = str(config.get("push_platform") or "").strip()
        uid = str(config.get("push_uid") or "").strip()
        if legacy and not platform and not uid:
            parsed = TechDigestPlugin._parse_umo(legacy)
            if parsed:
                plat, _mtype, target = parsed
                if target.isdigit():
                    try:
                        config["push_platform"] = plat
                        config["push_uid"] = target
                    except Exception as e:
                        logger.warning(f"[{PLUGIN_NAME}] 迁移推送目标失败: {e}")
                    else:
                        logger.warning(
                            f"[{PLUGIN_NAME}] 旧推送目标「{legacy}」已搬进"
                            f"「发信账号 {plat} / 收件人 QQ {target}」两个字段；"
                            "以后在设置里清空就真的不推送了"
                        )
        state["config_migrated_v224"] = True
        return TechDigestPlugin.build_push_target(config)[0]

    @staticmethod
    def migrate_chat_platforms(rows, legacy_id: str) -> list[str]:
        """老配置的全局 platform_id 补进没填「读取账号」的群行，返回补过的群号。

        已有单群设置保持优先；没有群号的行不动。
        """
        legacy = str(legacy_id or "").strip()
        if not legacy:
            return []
        filled: list[str] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            gid = str(row.get("gid") or "").strip()
            if not gid or str(row.get("platform") or "").strip():
                continue
            row["platform"] = legacy
            filled.append(gid)
        return filled

    def _parse_group_ids(self) -> list[str]:
        """兼容老配置里的 group_ids（字符串或列表），新配置走 chats。"""
        raw = self.config.get("group_ids")
        if raw is None:
            return []
        if isinstance(raw, str):
            items = [x.strip() for x in re.split(r"[,，、\s]+", raw) if x.strip()]
        elif isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            return []
        return [gid for gid in items if gid.isdigit()]

    @staticmethod
    def _as_bool(value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(value)

    def _apply_chats(self, raw) -> None:
        """把配置里的 chats 解析成 ChatInterest 列表，并派生 group_ids。

        读取账号是每群必填（不再有全局默认账号）：没填的群照样采集，生成时跳过并提醒。
        """
        chats, skipped = parse_chats(raw, "")
        if not chats:
            legacy = self._parse_group_ids()
            if legacy:
                chats = [ChatInterest(gid=gid, platform="") for gid in legacy]
                logger.warning(
                    f"[{PLUGIN_NAME}] 检测到旧配置 group_ids={legacy}，已临时当作 chats 使用；"
                    f"请在设置界面为每个群补上「关心的标签」"
                )
        self.chats = chats
        self.group_ids = [c.gid for c in chats]
        if skipped:
            logger.warning(f"[{PLUGIN_NAME}] 这些 chats 行没有群号，已跳过：{skipped}")
        if not chats:
            logger.info(
                f"[{PLUGIN_NAME}] 还没有可用的群配置（chats 为空），处于待配置状态："
                "添加群号与关心的标签后才会生成报告"
            )

    def _chat(self, gid: str | None) -> ChatInterest | None:
        if not gid:
            return None
        for chat in self.chats:
            if chat.gid == str(gid):
                return chat
        return None

    def _chat_status_line(
        self, chat: ChatInterest, name: str, count: int, imgs: int, last: str = ""
    ) -> str:
        """状态里每个群一行：谁在采集、今天多少条、上次覆盖到哪。"""
        parts = [f"{name}（{chat.gid}）"]
        parts.append(f"读历史用 {chat.platform}" if chat.platform else "⚠ 没填读取账号")
        parts.append(f"今日 {count} 条 / 带图 {imgs} 条")
        parts.append(self._last_report_desc(chat.gid))
        if chat.tags_pos:
            parts.append(f"标签 {len(chat.tags_pos)} 个")
        else:
            parts.append("⚠ 未配置标签")
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
        """先写临时文件再原子替换：中途崩溃也不会把 state.json 写坏。"""
        tmp = None
        try:
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] state.json 写入失败: {e}")
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    def _group_state(self, gid: str) -> dict:
        return self.state.setdefault("groups", {}).setdefault(gid, {})

    # ------------------------------------------------------------------ 基础
    def _get_bot(self, gid: str | None = None):
        """按这个群自己的「读取账号」拿 bot 实例；没填就是没有（没有全局默认账号）。"""
        chat = self._chat(gid)
        platform_id = chat.platform if chat is not None else ""
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
        """用 AstrBot 当前的默认对话模型，不用用户填 id。"""
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    @staticmethod
    def _vision_capable(provider) -> bool:
        """模型配置里声明了能读图就算能读图（没声明就当作能，交给模型自己报错）。"""
        cfg = getattr(provider, "provider_config", None)
        modalities = cfg.get("modalities") if isinstance(cfg, dict) else None
        if isinstance(modalities, list) and modalities:
            return "image" in [str(m).lower() for m in modalities]
        return True

    # ------------------------------------------------------------------ 静默通道
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def silence_events(self, event: AstrMessageEvent):
        """第一道闸：目标群的消息一律落盘（与是否禁言无关）；静默平台再终止事件。

        采集与禁言是两件事：禁言只表示"这个账号不说话"，不该影响收数据。
        所以先把消息记下来，再决定要不要让这个账号闭嘴。
        """
        platform_id = self._platform_of(event)
        silenced = platform_id in self.silence_platforms
        if silenced:
            # 先闭嘴再去采集：图片下载可能要几秒，别让"不说话"这件事被拖住
            event.stop_event()
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if gid and gid in self.group_ids:
            await self._record(event)
        if not silenced:
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

    # ------------------------------------------------------------------ 大白话直通
    def _should_act(self, text: str, intent) -> bool:
        """意图够不够格接手这件事。弱指向（只说"群里"）还得带时间词或群名。"""
        if intent is None:
            return False
        if intent.strong:
            return True
        rng, _range_txt = self._match_range(text)
        gid, _group_txt = self._match_group(text)
        return bool(rng or gid)

    async def _send_text(self, umo: str, text: str) -> None:
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 文本发送失败: {e}")

    async def _send_image(self, umo: str, path: Path) -> None:
        try:
            await self.context.send_message(
                umo, MessageChain([AstrImage.fromFileSystem(str(path))])
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 图片发送失败: {e}")

    async def _handle_intent(self, event: AstrMessageEvent, intent) -> None:
        """按意图分流：状态 / 重发 / 重跑 / 总结。"""
        text = (event.message_str or "").strip()
        umo = event.unified_msg_origin

        if intent.kind == "status":
            await self._send_text(umo, "\n".join(await self._status_lines()))
            return
        if intent.kind == "resend":
            latest = self._latest_report()
            if latest is None:
                await self._send_text(umo, "还没有生成过报告。")
            else:
                await self._send_image(umo, latest)
            return

        for gid in self.group_ids:
            await self._group_name(gid)  # 预热群名，否则按名字匹配不到群
        rng, gid, rest = self._resolve_request(text)
        unknown_group = ""
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
                    if not gid:
                        unknown_group = group_hint  # 模型听出了群名，但配置里没有这个群
        since, until, label, incremental = rng
        if intent.kind == "rerun":
            incremental = False  # 重跑：忽略增量位，按范围重来
        if not gid and not unknown_group and rest:
            core = self._clean_text(rest)
            if self._looks_like_group(core):
                unknown_group = core
        if unknown_group:
            # 用户点名了某个群却对不上：不要退回「全部群」，那是白烧 token
            await self._send_text(
                umo,
                f"没找到「{unknown_group}」这个群，这次就不生成了。\n"
                f"现在配好的群：{await self._known_groups_text()}",
            )
            return
        logger.info(
            f"[{PLUGIN_NAME}] 直通 {intent.kind}：范围={label} 增量={incremental} "
            f"群={gid or '全部'} 命中「{intent.anchor}+{intent.action}」原文「{text}」"
        )
        if not self._claim():
            if self._enqueue(("digest", umo, since, until, label, incremental, gid, True)):
                await self._send_text(umo, "上一份还在生成，这份先排队，跑完接着来。")
            else:
                await self._send_text(umo, f"已经在跑一份、后面还排着 {QUEUE_MAX} 份了，等它们跑完再叫我。")
            return
        await self._execute(
            ("digest", umo, since, until, label, incremental, gid, True)
        )

    def _enqueue(self, request: tuple) -> bool:
        """入队一份待跑的请求；队列满返回 False，调用方据此明确告知用户。"""
        if len(self._queue) >= (self._queue.maxlen or 1):
            return False
        self._queue.append(request)
        return True

    def _claim(self) -> bool:
        """同步抢占执行位：返回 True 表示这份由我来跑。

        「看忙不忙」和「置忙」之间不能有 await，否则两次几乎同时的调用会都看到空闲、
        各起一个后台任务（模型工具就是这样撞的）。所以提交时就地占位，再起任务。
        """
        if self._running:
            return False
        self._running = True
        self._running_since = time.time()
        return True

    def _release_stale_claim(self, limit: float = 3600.0) -> bool:
        """兜底：任务被取消导致占位没释放时，超过 limit 秒强制放掉。"""
        if not self._running:
            return False
        if time.time() - float(self._running_since or 0) < limit:
            return False
        logger.error(
            f"[{PLUGIN_NAME}] 执行位被占了 {int(limit)} 秒以上（任务可能被中断），强制释放"
        )
        self._running = False
        return True

    async def _execute(self, request: tuple) -> None:
        """唯一的生成执行入口：跑 → 清忙 → 接着吃队列里的下一份。

        进入这里时执行位已经由 `_claim()` 同步占上；之后每吃完一份都要重新抢一次。
        """
        while True:
            try:
                if request and request[0] == "daily":
                    await self._run_daily_once()
                else:
                    await self._generate_and_send(*request[1:])
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 生成任务失败: {e}", exc_info=True)
            finally:
                self._running = False
            if not self._queue:
                return
            request = self._queue.popleft()
            self._running = True
            self._running_since = time.time()
            logger.info(
                f"[{PLUGIN_NAME}] 接着跑排队的任务（队列还剩 {len(self._queue)} 份）"
            )

    async def _drain_pending(self) -> None:
        """把排队的第一份接着跑掉（跑完会继续吃后面的）。"""
        if not self._queue or not self._claim():
            return
        await self._execute(self._queue.popleft())

    async def _known_groups_text(self) -> str:
        """现在配了哪些群，用来在「找不到这个群」时回答用户。"""
        if not self.group_ids:
            return "（还没有配置任何群）"
        parts = []
        for gid in self.group_ids:
            name = await self._group_name(gid)
            parts.append(f"{name}（{gid}）" if name and name != gid else gid)
        return "、".join(parts)

    @staticmethod
    def _looks_like_group(text: str) -> bool:
        """剩下的碎片像不像在点名一个群。

        只认高置信度信号：英文/数字 ≥3 位（群号、MeowShelter 这类），或 ≥4 个连续汉字。
        门槛刻意抬高一档——把「情报跑」这种残留误判成群名，会让人正常问一句都生成不了。
        中文群名写错字的情况交给下面的模型解析把关。
        """
        if not text:
            return False
        if re.search(r"[A-Za-z0-9_\-]{3,}", text):
            return True
        return bool(re.search(r"[\u4e00-\u9fff]{4,}", text))

    async def _hint_no_owner(self, event: AstrMessageEvent, umo: str) -> None:
        """没配 owner 时提示一次怎么配（一小时最多一次，避免刷屏）。"""
        now = int(time.time())
        if now - int(self.state.get("owner_hint_ts") or 0) < 3600:
            return
        self.state["owner_hint_ts"] = now
        self._save_state()
        await self._send_text(
            umo,
            "这个插件还没配置「谁可以拉报告」（owner_uids），现在谁私聊都不会生成。\n"
            "去 WebUI → 插件管理 → 群聊情报 → 设置 → 「谁可以要报告（owner_uids）」里填上你自己的 QQ 号，"
            "保存后再叫我。",
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=900)
    async def intercept_digest_intent(self, event: AstrMessageEvent):
        """私聊里用大白话要报告时直接出图，不赌模型会不会调工具。"""
        if not self._tool_allowed(event):
            # 群里一个字都不说（这是一条硬规矩：群内零动静）
            try:
                if event.get_group_id():
                    return
            except Exception:
                return
            # 私聊里只对「在要报告」的人说清楚原因，别把闲聊也打断
            text = (event.message_str or "").strip()
            if text and not text.startswith(("/", "／", "#")):
                intent = detect_intent(text)
                if self._should_act(text, intent) and not self.owner_uids:
                    event.stop_event()
                    try:
                        await self._hint_no_owner(event, event.unified_msg_origin)
                    except Exception as e:
                        logger.warning(f"[{PLUGIN_NAME}] 权限提示发送失败: {e}")
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith(("/", "／", "#")):
            return
        intent = detect_intent(text)
        if not self._should_act(text, intent):
            return
        if RE_FOLLOWUP.search(text):
            # 「日报第二条有什么依据」「他原话怎么说的」是在追问已经发过的报告，
            # 不是要新报告：放给模型去选 get_group_report / query_group_records。
            logger.info(f"[{PLUGIN_NAME}] 追问类请求，交给模型处理：「{text[:40]}」")
            return
        event.stop_event()  # 模型这段就别开口了
        asyncio.create_task(self._handle_intent(event, intent))

    @filter.on_llm_request()
    async def silence_llm(self, event: AstrMessageEvent, req: ProviderRequest) -> bool:
        """第二道闸：即使事件绕过 stop_event（例如 @ 唤醒），也不让模型出手。"""
        platform_id = self._platform_of(event)
        if platform_id not in self.silence_platforms:
            # 这里放行，但要明确告诉模型：涉及群聊内容就调工具，别自己编
            if self._tool_allowed(event) and self.group_ids:
                hint = (
                    "\n\n[群聊情报工具] 你手上有三个工具，按用户想要的东西选：\n"
                    "1) 用户要**新的一份**总结/日报/情报 → 调用 group_digest"
                    "（when=时间，group=群名片段，都留空表示只看没看过的全部群）。"
                    "报告图由这个工具自己发出去，调用后不要再输出文字。\n"
                    "2) 用户在**讨论已经发过的报告**（“刚才那份”“第二个话题”“这一条靠谱吗”）→ "
                    "调用 get_group_report 读正文，不要重新生成报告。需要原话时再调 "
                    "query_group_records。\n"
                    "3) 用户要**查证原始消息**（“他原话怎么说的”“后来有人验证了吗”“是不是实测”）→ "
                    "调用 query_group_records，按时间、关键词或消息引用查，并带上前后文；从报告里拿到的引用，把 report_id 一起传，群和时间范围会自动取。\n"
                    "回答时分清三件事：报告里的概括、群友的原始说法（带时间和人）、"
                    "你自己的分析。查不到就直说是记录过期、采集有缺口或那一时段没有相关内容，"
                    "不要用推测填补。除 group_digest 外，其他工具执行完都要正常回答。"
                )
                blurb = reports.blurb(self.state, event.unified_msg_origin)
                if blurb:
                    hint += "\n" + blurb
                req.system_prompt = (req.system_prompt or "") + hint
            return False
        self._llm_blocked += 1
        if self._llm_blocked <= 5 or self._llm_blocked % 100 == 0:
            logger.warning(
                f"[{PLUGIN_NAME}] 拦下一次 LLM 请求（平台 {platform_id}，累计 {self._llm_blocked} 次）"
            )
        # 光返回 True 没用：AstrBot 的 call_event_hook 只看 event.is_stopped()，
        # 必须真的把事件停掉，否则模型照样会跑一遍（只是回复被第三道闸清掉）
        event.stop_event()
        return True

    @filter.on_decorating_result()
    async def silence_result(self, event: AstrMessageEvent):
        """第三道闸：回复已经生成也不发出去。"""
        # 工具已经自己把报告图发出去了，模型这句「好的我帮你生成」就不要再发了
        if event.get_extra("chat_intel_tool_sent"):
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
            text = self._cap_text(event.message_str or "")

            images: list[str] = []
            if self.include_images and self.max_images > 0:
                for url in self._event_image_urls(event)[:3]:
                    saved = await self._download_image(url)
                    if saved:
                        images.append(saved)

            if not text and not images:
                return
            record = {
                "t": self._event_time(event),
                "mid": str(getattr(getattr(event, "message_obj", None), "message_id", "") or ""),
                "gid": gid,
                "uid": uid,
                "name": str(event.get_sender_name() or "")[:16],
                "text": text,
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
    def _event_time(event) -> int:
        """优先用 OneBot 事件自带的时间：两个 bot 跨秒收到同一条消息时才去得掉重。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            try:
                stamp = int(raw.get("time") or 0)
                if stamp > 0:
                    return stamp
            except (TypeError, ValueError):
                pass
        return int(time.time())

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
            # 只报前几条，避免刷屏；tag 自带定位信息（图片下载失败 / 取群名失败 / 异常 …）
            logger.warning(f"[{PLUGIN_NAME}] {tag}：{detail}", exc_info=exc)

    def _load_records(
        self, gid: str, since: float, until: float, limit: bool = True
    ) -> list[dict]:
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
                        # 没有群号的旧行直接忽略：老版本会把它算到 group_ids[0] 头上，
                        # 那会把 A 群的聊天串进 B 群的报告里。
                        if str(rec.get("gid") or "") != gid:
                            continue
                        out.append(rec)
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 读取 {path.name} 失败: {e}")
        out.sort(key=lambda r: int(r.get("t") or 0))
        out = self._dedupe_records(out)
        if limit and len(out) > self.max_messages:
            out = out[-self.max_messages :]
        return out

    def _merge_records(
        self, local: list[dict], history: list[dict], limit: bool = True
    ) -> list[dict]:
        """本地记录 + 历史记录：按消息 id 去重（撞键合并图片）、按时间排序、留最新 N 条。

        顺序很重要：本地记录是最新的，历史记录是补回来的旧的；不排序直接截尾会把最新的丢掉。
        """
        merged = self._dedupe_records(list(local) + list(history))
        merged.sort(key=lambda r: int(r.get("t") or 0))
        return merged[-self.max_messages :] if limit else merged

    @staticmethod
    def _dedupe_records(records: list[dict]) -> list[dict]:
        """同一条消息被两个 bot / 本地与历史各收一次时只留一条（与平台无关）。

        有消息 id 就用 id（最准）；老记录没有 id 才回落到「发送者 + 时间 + 文本」指纹。
        撞键时要把图片并起来，不能整条丢掉——两个 bot 收到的图片链接常常不一样。
        """
        index: dict[tuple, dict] = {}
        out: list[dict] = []
        for rec in records:
            mid = str(rec.get("mid") or "")
            key = (
                ("mid", mid)
                if mid
                else (
                    "fp",
                    str(rec.get("uid") or ""),
                    int(rec.get("t") or 0),
                    str(rec.get("text") or "")[:40],
                )
            )
            kept = index.get(key)
            if kept is None:
                index[key] = rec
                out.append(rec)
                continue
            imgs = kept.setdefault("imgs", [])
            for rel in rec.get("imgs") or []:
                if rel not in imgs:
                    imgs.append(rel)
        return out

    # ------------------------------------------------------------------ 拉历史
    async def _fetch_history(
        self, gid: str, since: float, until: float | None = None
    ) -> list[dict]:
        bot = self._get_bot(gid)
        if bot is None or not hasattr(bot, "call_action"):
            return []
        collected: list[dict] = []
        seen: set[str] = set()
        anchor = None
        pages = 0
        while len(collected) < self.max_messages and pages < MAX_HISTORY_PAGES:
            pages += 1
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
                t = int(m.get("time") or 0)
                if t >= since and (until is None or t < until):
                    collected.append(m)
                    added += 1
            oldest_msg = min(batch, key=lambda m: int(m.get("time") or 0))
            oldest = int(oldest_msg.get("time") or 0)
            next_anchor = oldest_msg.get("message_seq") or oldest_msg.get("message_id")
            # 只根据「翻到比起始时间更早」和「翻不动了」结束；
            # 这一页没有落在目标范围内的消息并不代表再往前也没有（要的时间段可能更早）
            if oldest < since or next_anchor is None:
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
            "text": self._cap_text(self._msg_text(msg)),
            "mid": str(msg.get("message_id") or msg.get("real_id") or ""),
        }
        return record

    @classmethod
    def _cap_text(cls, text: str) -> str:
        """保留原文的换行与缩进（命令、代码、报错靠它才看得懂），只在真超长时截断并标记。"""
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in raw.split("\n")]
        # 连续空行压成一个，避免整屏空白占满预算
        cleaned: list[str] = []
        blank = False
        for line in lines:
            if not line.strip():
                if blank:
                    continue
                blank = True
                cleaned.append("")
            else:
                blank = False
                cleaned.append(line)
        text_out = "\n".join(cleaned).strip()
        if len(text_out) > MSG_TEXT_CAP:
            return text_out[:MSG_TEXT_CAP] + " …（过长已截断）"
        return text_out

    @staticmethod
    def _msg_text(msg: dict) -> str:
        segments = msg.get("message")
        if isinstance(segments, str):
            return segments
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
        return "".join(parts).strip()

    async def _group_name(self, gid: str) -> str:
        """群名自动检测。报告图里只用群名，文字回复里会带群号。"""
        if gid in self.group_name_map:
            return self.group_name_map[gid]
        bot = self._get_bot(gid)
        if bot is None or not hasattr(bot, "call_action"):
            # 启动瞬间 bot 还没连上，属正常情况：不缓存、不刷警告，下次再试
            logger.debug(f"[{PLUGIN_NAME}] 取群名失败 {gid}：暂无 bot 实例")
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
    # 用户说的话不要求固定格式：从一整句里抠出「时间范围」和「群」，
    # 抠不干净的碎片再交给模型兜底（smart_parse），所以这些说法都成立：
    #   群聊情报今天考古聊了啥      群聊情报看看今天的考古群      群聊情报2026年11月5日
    #   帮我看看昨天群聊情报里考古群聊了啥            群聊情报最近3天
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
    @staticmethod
    def _stamp(ts: int, multi_day: bool) -> str:
        """跨天的记录要带日期，否则「第二天验证」这种后续完全对不上号。"""
        moment = datetime.fromtimestamp(ts)
        return moment.strftime("%m-%d %H:%M") if multi_day else moment.strftime("%H:%M")

    def _build_transcript(
        self, records: list[dict], name_map: dict[str, str] | None = None
    ) -> tuple[str, int, int]:
        if name_map is None:
            name_map = build_name_map([rec.get("name") for rec in records])
        days = {
            datetime.fromtimestamp(int(rec.get("t") or 0)).date()
            for rec in records
            if rec.get("t")
        }
        multi_day = len(days) > 1
        lines: list[str] = []
        senders: set[str] = set()
        for idx, rec in enumerate(records):
            uid = str(rec.get("uid") or "")
            if uid in self.ignore_uids:
                continue
            text = str(rec.get("text") or "").strip()
            has_img = bool(rec.get("imgs"))
            if not text and not has_img:
                continue
            if text in ("[表情]", "[图片]"):
                continue
            name = name_map.get(sanitize_name(rec.get("name"))) or (
                sanitize_name(rec.get("name")) or str(uid)
            )[:16]
            ts = int(rec.get("t") or 0)
            stamp = self._stamp(ts, multi_day) if ts else "--:--"
            body = text if text else "[图]"
            if has_img and "[图]" not in body:
                body += " [图]"
            senders.add(uid)
            lines.append(f"[{stamp}] #{idx} {name}: {body}")
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

    async def _extract_chunk(
        self, chat: ChatInterest, group_name: str, window: str, transcript: str
    ) -> dict | None:
        provider = self._get_provider()
        if provider is None:
            logger.error(f"[{PLUGIN_NAME}] 没有可用的模型供应商")
            return None
        # 指令必须写在 user 消息里，且放在转录之后（这个 provider 只认最后一条 user）
        prompt = build_digest_prompt(chat, group_name, window, transcript)
        for attempt in range(1, CHUNK_RETRY + 1):
            try:
                resp = await provider.text_chat(
                    prompt=prompt, system_prompt=SYSTEM_PROMPT, contexts=[]
                )
                text = getattr(resp, "completion_text", "") or ""
            except Exception as e:
                logger.error(
                    f"[{PLUGIN_NAME}] 调用模型失败（第 {attempt}/{CHUNK_RETRY} 次）: {e}",
                    exc_info=(attempt == CHUNK_RETRY),
                )
                if attempt < CHUNK_RETRY:
                    await asyncio.sleep(2)
                continue
            data = self._parse_json(text)
            ok, why = self._chunk_ok(data)
            if ok:
                return data
            logger.warning(
                f"[{PLUGIN_NAME}] 这一段结果不可用（第 {attempt}/{CHUNK_RETRY} 次）：{why}；"
                f"原文前 120 字：{text[:120]}"
            )
            if attempt < CHUNK_RETRY:
                await asyncio.sleep(2)
        return None

    @staticmethod
    def _chunk_ok(data) -> tuple[bool, str]:
        """这一段的结果能不能用：顶层是对象、topics 是列表（空列表表示没有命中话题，算成功）。

        不能用「非空字典」当成功——模型偶尔会回 {"error": ...} 之类的对象，
        那样这一段的内容会静默消失，游标却照常前进。
        """
        if not isinstance(data, dict):
            return False, f"顶层不是 JSON 对象（{type(data).__name__}）"
        if "topics" not in data:
            return False, "缺少 topics 字段"
        if not isinstance(data.get("topics"), list):
            return False, f"topics 不是列表（{type(data.get('topics')).__name__}）"
        return True, ""

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

    def _merge_parts(self, parts: list[dict], chat: ChatInterest) -> dict:
        """合并各分片的话题：跨块重复的并成一条，总览按最终留下的话题生成。"""
        headline = ""
        topic_groups: list[list[dict]] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if not headline:
                headline = str(part.get("headline") or "").strip()
            topic_groups.append(part.get("topics") or [])
        topics = merge_topics(chat, topic_groups)
        return {"headline": self._make_headline(headline, topics), "topics": topics}

    @staticmethod
    def _make_headline(model_headline: str, topics: list[dict]) -> str:
        """总览要覆盖整份报告：单个话题直接用模型那句，多个话题按标题列出来。

        之前是「取第一个非空分片的 headline」，多分片时它只描述第一块内容。
        """
        titles = [str(t.get("title") or "").strip() for t in topics if t.get("title")]
        if not titles:
            return model_headline
        if len(titles) == 1:
            return model_headline or titles[0]
        shown = "、".join(titles[:3])
        more = f" 等 {len(titles)} 个话题" if len(titles) > 3 else ""
        return f"这段时间主要在聊 {shown}{more}"

    @staticmethod
    def _payload_topics(topics: list[dict], images: dict[int, list[dict]] | None = None) -> list[dict]:
        """交给模板的话题结构。

        - tags 必须保持成数组：模板按数组逐个渲染标签，一旦在 Python 侧拼成「甲、乙」字符串，
          模板就会把它当字符串逐字拆开，渲染成一字一个标签块。
        - parts 是把正文按「昵称 / 非昵称」切好的小段，模板靠它把群友 ID 排成浅一点的色。
        """
        out: list[dict] = []
        for topic in topics:
            title = str(topic.get("title") or "").strip()
            if not title:
                continue
            raw_tags = topic.get("tags") or []
            summary = str(topic.get("summary") or "").strip()
            out.append(
                {
                    "title": title,
                    "tags": [str(tag) for tag in raw_tags]
                    if isinstance(raw_tags, (list, tuple))
                    else [],
                    "time": str(topic.get("time") or ""),
                    "members": str(topic.get("members") or ""),
                    "summary": summary,
                    "parts": split_names(summary, topic.get("names") or []),
                    "images": list((images or {}).get(len(out), [])),
                    "count": int(topic.get("count") or 0),
                }
            )
        return out

    @staticmethod
    def _describe_topics(
        topics: list[dict],
        records: list[dict],
        name_map: dict[str, str] | None = None,
    ) -> list[dict]:
        """给每个话题算出时间范围和参与人：用模型给的消息编号回查原始记录，不靠模型自己写。"""
        if name_map is None:
            name_map = build_name_map([rec.get("name") for rec in records])
        out: list[dict] = []
        for topic in topics:
            nums = [n for n in topic.get("msg_nos") or [] if 0 <= n < len(records)]
            picked = [records[n] for n in nums]
            names: list[str] = []
            for rec in picked:
                raw = sanitize_name(rec.get("name"))
                name = name_map.get(raw, raw)
                if name and name not in names:
                    names.append(name)
            out.append(
                {
                    "title": topic.get("title") or "",
                    "tags": topic.get("tags") or [],
                    "summary": topic.get("summary") or "",
                    "msg_nos": nums,
                    "time": topic_time_range([int(r.get("t") or 0) for r in picked]),
                    "members": topic_members(names),
                    "names": names,  # 完整名单，正文高亮用（members 会给「等 N 人」截断）
                    "count": len(picked),
                }
            )
        return out

    # ------------------------------------------------------------------ 图片挑选
    async def _alert_missing_platform(self, group_name: str, gid: str) -> None:
        """某个群没填读取账号时私聊提醒一次（同一个群不反复刷）。"""
        key = f"warned_platform_{gid}"
        if self.state.get(key):
            return
        text = (
            f"⚠ {group_name} 没填「读取账号」，这个群不会生成报告。\n"
            f"在插件设置 → 要总结的群 → 这个群的「读取账号」里填上读它的那个 bot（平台实例 id，比如 bot_b）。"
        )
        try:
            sent = await self._push_text(text)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 提醒发送失败: {e}")
            return
        if sent is False:
            return
        self.state[key] = int(time.time())
        self._save_state()

    async def _alert_missing_tags(self, group_name: str, gid: str) -> None:
        """某个群没配关心的标签时私聊提醒一次，不反复骚扰。"""
        key = f"warned_tags_{gid}"
        if self.state.get(key):
            return
        text = (
            f"⚠ {group_name} 还没配置「关心的标签」，这次跳过了。\n"
            f"在插件设置 → 要总结的群 → 这个群的「关心的标签」里填几个词（比如 电脑、动漫、科研）就行。"
        )
        try:
            sent = await self._push_text(text)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 告警发送失败: {e}")
            return
        if sent is False:  # 明确没发出去就别记账，下次还提醒
            return
        self.state[key] = int(time.time())
        self._save_state()

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

    async def _images_for_topics(
        self, records: list[dict], topics: list[dict], chat: ChatInterest
    ) -> dict[int, list[dict]]:
        """把图分给话题：只挑话题自己引用的那些消息里的图，每个话题最多 max_images 张。

        顺序：先按话题收集候选（最近 PER_TOPIC_ASK 张），跨话题总量封顶 MAX_JUDGE_PER_RUN 张，
        再交给视觉模型判一次（结论带缓存），最后按话题挑回前 max_images 张。
        """
        if not self.include_images or self.max_images <= 0 or not topics:
            return {}
        by_idx = {cand["idx"]: cand for cand in self._image_candidates(records)}
        if not by_idx:
            return {}

        # 分配顺序：先把每个话题「自己引用了的」图给它，再按编号跨度补空位。
        # 之前只按跨度先来先得，前面的话题会把跨度里别的话题明确引用的图抢走，
        # 而且同一张图（同一文件）可能重复出现。
        per_topic: list[list[dict]] = [[] for _ in topics]
        claimed: set[int] = set()
        used_hash: set[str] = set()

        def take(bucket: list[dict], idx: int, title: str) -> bool:
            if idx in claimed:
                return False
            cand = by_idx[idx]
            digest = cand.get("hash") or ""
            if digest and digest in used_hash:
                claimed.add(idx)  # 同一张图已经挂到别处了，别重复出现
                return False
            claimed.add(idx)
            if digest:
                used_hash.add(digest)
            bucket.append(dict(cand, topic=title))
            return True

        # 第一遍：话题明确引用的图消息
        for i, topic in enumerate(topics):
            title = topic.get("title") or ""
            for idx in sorted({n for n in topic.get("msg_nos") or [] if n in by_idx}):
                if len(per_topic[i]) >= PER_TOPIC_ASK:
                    break
                take(per_topic[i], idx, title)
        # 第二遍：跨度里剩下的（模型经常不引用纯「[图]」那一行，按跨度兜一下）
        for i, topic in enumerate(topics):
            if len(per_topic[i]) >= PER_TOPIC_ASK:
                continue
            nums = sorted({n for n in topic.get("msg_nos") or [] if 0 <= n < len(records)})
            if not nums:
                continue
            lo, hi = nums[0], nums[-1]
            for idx in sorted(by_idx):
                if len(per_topic[i]) >= PER_TOPIC_ASK:
                    break
                if lo <= idx <= hi:
                    take(per_topic[i], idx, topic.get("title") or "")
        logger.info(
            f"[{PLUGIN_NAME}] 图分配：{len(by_idx)} 张带图消息，各话题命中 "
            f"{[len(x) for x in per_topic]}"
        )

        # 去重后按「每个话题轮流取一张」拼成待判列表，避免某个大话题把所有额度吃光
        picked: list[dict] = []
        seen: set[int] = set()
        depth = max((len(x) for x in per_topic), default=0)
        for level in range(depth):
            for bucket in per_topic:
                if level >= len(bucket):
                    continue
                cand = bucket[level]
                if cand["idx"] in seen:
                    continue
                seen.add(cand["idx"])
                picked.append(cand)
                if len(picked) >= MAX_JUDGE_PER_RUN:
                    break
            if len(picked) >= MAX_JUDGE_PER_RUN:
                break
        if not picked:
            return {}

        provider = self._get_vision_provider()
        if provider is not None:
            kept = await self._vision_picks(provider, picked, chat)
        else:
            kept = await self._rule_picks(picked, chat)
        keep_idx = {cand["idx"] for cand in kept}
        if not keep_idx:
            return {}

        out: dict[int, list[dict]] = {}
        for i, bucket in enumerate(per_topic):
            chosen = [cand for cand in bucket if cand["idx"] in keep_idx][: self.max_images]
            if chosen:
                out[i] = [self._image_entry(cand) for cand in chosen]
        return out

    def _file_hash(self, path: Path) -> str:
        """文件内容哈希：同一张图被转存/重发多次时用来去重（按文件名去不掉）。"""
        cache = getattr(self, "_hash_cache", None)
        if cache is None:
            cache = self._hash_cache = {}
        key = str(path)
        if key in cache:
            return cache[key]
        try:
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
        except Exception:
            digest = ""
        cache[key] = digest
        return digest

    def _image_candidates(self, records: list[dict]) -> list[dict]:
        """挑出带图的消息，连同它前后两条的文字，供判图用。idx 消息在 records 里的下标。"""
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
                    "idx": idx,
                    "rec": rec,
                    "files": files,
                    "hash": self._file_hash(files[0]),
                    "prev": str(records[idx - 1].get("text") or "") if idx > 0 else "",
                    "next": str(records[idx + 1].get("text") or "")
                    if idx + 1 < len(records)
                    else "",
                }
            )
        return candidates

    def _image_cache_key(self, chat: ChatInterest, path: Path, topic: str = "") -> str:
        """判图缓存键：标签指纹 + 话题 + 文件名。

        判图时会带着话题标题和前后文看，同一张图在不同话题里的意义可能完全不同，
        所以话题也要进键；只按标签+文件名缓存，会把 A 话题的结论直接套到 B 话题上。
        """
        tag = hashlib.sha1(str(topic or "").strip().encode("utf-8")).hexdigest()[:8]
        return f"{interest_fingerprint(chat)}:{tag}:{path.name}"

    def _reset_image_cache_if_stale(self) -> None:
        if self.state.get("images_version") != IMG_CACHE_VERSION:
            self.state["images"] = {}
            self.state["images_version"] = IMG_CACHE_VERSION

    async def _vision_picks(
        self, provider, candidates: list[dict], chat: ChatInterest
    ) -> list[dict]:
        """并发送图给视觉模型，按这个群的兴趣留图；结论按兴趣指纹缓存。"""
        self._reset_image_cache_if_stale()
        cache = self.state.setdefault("images", {})
        semaphore = asyncio.Semaphore(3)
        results: dict[str, dict] = {}

        async def judge(cand: dict) -> None:
            path = cand["files"][0]
            key = self._image_cache_key(chat, path, cand.get("topic") or "")
            cached = cache.get(key)
            if isinstance(cached, dict) and "keep" in cached:
                results[key] = cached
                return
            async with semaphore:
                verdict = await self._classify_image(provider, path, cand, chat)
            if verdict is None:
                return
            results[key] = verdict
            cache[key] = verdict

        await asyncio.gather(*(judge(c) for c in candidates))

        kept: list[dict] = []
        dropped: list[str] = []
        for cand in candidates:
            verdict = results.get(
                self._image_cache_key(chat, cand["files"][0], cand.get("topic") or "")
            )
            if verdict and verdict.get("keep"):
                cand["kind"] = verdict.get("kind") or ""
                cand["ocr"] = verdict.get("text") or ""
                kept.append(cand)
            else:
                dropped.append(f"{cand['files'][0].name}:{(verdict or {}).get('kind', '?')}")
        logger.info(
            f"[{PLUGIN_NAME}] 视觉判定（兴趣 {interest_fingerprint(chat)}）{len(candidates)} 张候选："
            f"留 {len(kept)} 张，丢弃 {len(candidates) - len(kept)} 张（其中 {len(results)} 张有判定）"
        )
        if dropped and len(dropped) <= 8:
            logger.info(f"[{PLUGIN_NAME}] 丢弃明细: {'; '.join(dropped)}")
        return kept

    async def _rule_picks(self, candidates: list[dict], chat: ChatInterest) -> list[dict]:
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
        filtered = filtered[: len(filtered)]
        picks = await self._ask_which_images(filtered, chat)
        if picks is None:
            picks = self._heuristic_image_picks(filtered, chat)
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

    async def _classify_image(
        self, provider, path: Path, cand: dict | None = None, chat: ChatInterest | None = None
    ) -> dict | None:
        cand = cand or {"prev": "", "rec": {}, "next": ""}
        chat = chat or ChatInterest("", "", ())
        try:
            resp = await provider.text_chat(
                prompt=build_vision_prompt(
                    chat,
                    self.group_name_map.get(chat.gid) or chat.name or "这个群",
                    str(cand.get("prev") or ""),
                    str((cand.get("rec") or {}).get("text") or ""),
                    str(cand.get("next") or ""),
                    str(cand.get("topic") or ""),
                ),
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
        """先看默认模型能不能读图；不能就翻一遍所有供应商，找一个声明支持的；都没有就退回规则筛图。"""
        provider = self._get_provider()
        if provider is not None and self._vision_capable(provider):
            return provider
        candidates = []
        try:
            insts = self.context.provider_manager.get_insts() or []
            candidates = list(insts.values()) if isinstance(insts, dict) else list(insts)
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 取供应商列表失败: {e}")
        for cand in candidates:
            if cand is provider:
                continue
            if self._vision_capable(cand):
                cfg = getattr(cand, "provider_config", None)
                name = cfg.get("id") if isinstance(cfg, dict) else None
                logger.info(f"[{PLUGIN_NAME}] 判图改用能读图的模型 {name or '（未命名）'}")
                return cand
        logger.info(f"[{PLUGIN_NAME}] 没有能读图的模型，图片退回规则筛选")
        return None

    async def _ask_which_images(
        self, candidates: list[dict], chat: ChatInterest | None = None
    ) -> list[int] | None:
        provider = self._get_provider()
        if provider is None:
            return None
        lines: list[str] = []
        for i, cand in enumerate(candidates):
            rec = cand["rec"]
            ts = int(rec.get("t") or 0)
            stamp = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"
            lines.append(
                f"#{i} 发送者={rec.get('name') or '?'} 时间={stamp} "
                f"尺寸={self._image_dims(cand['files'][0])[0]}x{self._image_dims(cand['files'][0])[1]} "
                f"本条={str(rec.get('text') or '(无文字)')[:80]} "
                f"上文={cand['prev'][-60:] or '(无)'} 下文={cand['next'][:60] or '(无)'}"
            )
        want_txt = "、".join(chat.tags_pos) if chat and chat.tags_pos else "技术相关内容"
        prompt = IMAGE_PICK_PROMPT.format(
            limit=self.max_images, items="\n".join(lines)
        ) + f"\n\n这个群关心的标签：{want_txt}。只挑与这些标签有关的图。"
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你只关心上面列出的主题，判断克制，不确定就不留。",
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
        candidates: list[dict], chat: ChatInterest | None = None
    ) -> list[int]:
        scored: list[tuple[int, int]] = []
        keywords = [w.lower() for w in (chat.tags_pos if chat else ()) if w]
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
            score += sum(1 for word in keywords if word in text)
            if score == 0:
                continue
            scored.append((score, i))
        scored.sort(key=lambda x: -x[0])
        return [i for _score, i in scored]

    # ------------------------------------------------------------------ 渲染
    # 长图（超长竖图）处理：实现在 imaging.py，出图脚本也复用同一套
    TALL_RATIO = imaging.TALL_RATIO
    STACK_RATIO = imaging.STACK_RATIO
    SLICE_MAX = imaging.SLICE_MAX

    @staticmethod
    def plan_tall_image(src: Path) -> dict | None:
        """这张图要不要特殊排版：None 普通图；cols=1 铺满整行；cols≥2 分段横排。"""
        return imaging.plan_tall_file(src)

    @staticmethod
    def compose_tall_image(src: Path, dst: Path) -> Path | None:
        """超高的图分段横排到 dst；不需要分段或失败返回 None。"""
        out = imaging.compose_tall_image(src, dst)
        if out == dst:
            logger.info(f"[{PLUGIN_NAME}] 长图分段横排：{src.name} → {dst.name}")
        return out

    def _render(self, payload: dict, gid: str, label: str) -> Path | None:
        try:
            import typst
        except ImportError:
            logger.error(f"[{PLUGIN_NAME}] 未安装 typst，请 pip install typst")
            return None
        slug = re.sub(r"[^\w\-]+", "", label) or "report"
        out = self.reports_dir / (
            f"chat_intel_{gid}_{datetime.now():%Y%m%d_%H%M%S}_{slug}.png"
        )
        job = Path(tempfile.mkdtemp(prefix="chat_intel_"))
        try:
            shutil.copyfile(self.template, job / "main.typ")
            img_dir = job / "img"
            img_dir.mkdir(exist_ok=True)
            # 图片是挂在各个话题下面的，排版时按话题把文件复制进临时目录并改写路径
            seq = 0
            for topic in payload.get("topics") or []:
                copies: list[dict] = []
                for img in topic.get("images") or []:
                    src = Path(img.get("path") or "")
                    if not src.is_file():
                        continue
                    # 长图分两种：普通高的整张铺满一行（字最大），离谱高的才分段横排
                    seq += 1
                    plan = self.plan_tall_image(src)
                    composed = img_dir / f"{seq}.png"
                    sliced = bool(plan and plan.get("cols", 0) >= 2) and (
                        self.compose_tall_image(src, composed) == composed
                    )
                    if sliced:
                        dst = composed
                    else:
                        ext = src.suffix.lower() or ".jpg"
                        dst = img_dir / f"{seq}{ext}"
                        try:
                            shutil.copyfile(src, dst)
                        except Exception:
                            continue
                    entry = {"path": f"img/{dst.name}", "caption": img.get("caption") or ""}
                    if sliced or plan:
                        # 长图铺满版心：挤在两列里字只有一半大小，手机上读不了
                        entry["wide"] = True
                    copies.append(entry)
                topic["images"] = copies
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
        self,
        gid: str,
        since: float,
        until: float,
        label: str,
        incremental: bool,
        umo: str = "",
    ) -> tuple[Path | None, str]:
        chat = self._chat(gid)
        if chat is None:
            return None, f"群 {gid} 不在配置里", True
        if not chat.platform:
            group_name = await self._group_name(gid)
            await self._alert_missing_platform(group_name, gid)
            return (
                None,
                f"{group_name}：没填「读取账号」，这个群先跳过（每群的读取账号是必填）",
                False,
            )
        if not chat.tags_pos:
            group_name = await self._group_name(gid)
            await self._alert_missing_tags(group_name, gid)
            # 已经在上面私聊提醒过了，这里不再当成"出错"再喊一遍
            return None, f"{group_name}：这个群还没配置「关心的标签」，跳过", False
        real_since = self._resolve_since(gid, since, incremental, until)
        group_name = await self._group_name(gid)

        records = self._load_records(gid, real_since, until) if self.use_local_store else []
        if len(records) < 20:
            history = await self._fetch_history(gid, real_since, until)
            if history:
                records = self._merge_records(records, history)
        records.sort(key=lambda r: int(r.get("t") or 0))
        records = records[-self.max_messages :]

        if not records:
            return None, f"{group_name}：{label}没有消息", False

        name_map = build_name_map([rec.get("name") for rec in records])
        transcript, msg_count, people = self._build_transcript(records, name_map)
        if not transcript.strip():
            return None, f"{group_name}：{label}只有表情和图片，没有可总结的文本", False

        chunks = self._split_chunks(transcript)
        parts: list[dict] = []
        failed: list[int] = []
        for i, chunk in enumerate(chunks, 1):
            window = label if len(chunks) == 1 else f"{label}（第 {i}/{len(chunks)} 段）"
            part = await self._extract_chunk(chat, group_name, window, chunk)
            ok, why = self._chunk_ok(part)
            if ok:
                parts.append(part)
            else:
                failed.append(i)
                if part is not None:
                    logger.warning(
                        f"[{PLUGIN_NAME}] {group_name} 第 {i} 段结果结构不对：{why}"
                    )
            if len(chunks) > 1:
                await asyncio.sleep(0.5)
        if not parts:
            return None, f"{group_name}：模型没有给出可用的结构化结果", True
        incomplete = bool(failed)
        if incomplete:
            logger.warning(
                f"[{PLUGIN_NAME}] {group_name}：第 {'、'.join(map(str, failed))}/{len(chunks)} 段生成失败，"
                "本轮按不完整处理（不推进增量游标）"
            )

        data = self._merge_parts(parts, chat)

        topics = self._describe_topics(data.get("topics") or [], records, name_map)
        if not topics:
            # 完整跑完但一条都没命中：也要把游标推上去，否则「看最新」每次都会重跑同一批消息
            if incremental and not incomplete:
                self._group_state(gid)["last_ts"] = int(until)
                self._save_state()
            return None, f"{group_name}：{label}没有命中标签的话题", False

        topic_images = await self._images_for_topics(records, topics, chat)
        image_count = sum(len(v) for v in topic_images.values())

        now = datetime.now()
        payload = {
            "title": chat.title or self.report_title,
            "date": now.strftime("%Y-%m-%d"),
            "group": group_name,
            "stats": {
                "line": f"{len(records)} 条消息 · {len(topics)} 个话题 · {label}",
            },
            "warn": (
                f"⚠ 第 {'、'.join(map(str, failed))}/{len(chunks)} 段生成失败，本报告不完整"
                if incomplete
                else ""
            ),
            "headline": str(data.get("headline") or "").strip(),
            "topics": self._payload_topics(topics, topic_images),
            "footer": f"生成于 {now:%Y-%m-%d %H:%M} · {PLUGIN_NAME} · 本报告不会发到群里",
        }

        path = self._render(payload, gid, label)
        if path is None:
            return None, f"{group_name}：排版成图失败，看日志", True

        # 报告正文另存一份 JSON：以前它只活在渲染临时目录里，出图就被删，
        # 用户问「刚才那份第二条」时没有任何依据。
        record = reports.build_record(
            report_id=reports.report_id_from_image(path.name),
            gid=gid,
            group=group_name,
            platform=str(chat.platform or ""),
            label=label,
            since=real_since,
            until=until,
            requested_since=since,
            incremental=incremental,
            topics=topics,
            records=records,
            image_name=path.name,
            images=[
                {"topic": index + 1, "file": str(img.get("path") or ""), "caption": str(img.get("caption") or "")}
                for index, items in sorted(topic_images.items())
                for img in items
            ],
            headline=str(data.get("headline") or ""),
            incomplete=incomplete,
            failed_chunks=failed,
            chunk_total=len(chunks),
            truncated=len(records) >= self.max_messages,
        )
        stored = reports.save(self.reports_dir, record) is not None
        if not stored:
            logger.warning(
                f"[{PLUGIN_NAME}] {path.name} 的正文存档写失败：图能发，但之后读不回来"
            )
        if umo:
            reports.resolve(self.state, umo, record, stored=stored)

        # 只有完整跑完才推进增量游标，否则漏掉的内容下一轮还能补回来
        if incremental and not incomplete:
            self._group_state(gid)["last_ts"] = int(until)
        self._group_state(gid)["last_report"] = path.name
        self._group_state(gid)["last_report_ts"] = int(now.timestamp())
        self._group_state(gid)["last_range"] = label
        if incomplete:
            self._group_state(gid)["last_incomplete"] = int(now.timestamp())
        self._save_state()

        tag_desc = "、".join(
            sorted({tag for topic in topics for tag in topic["tags"]})
        ) or "无"
        info = (
            f"[{group_name}] {label}：{len(records)} 条 / {len(topics)} 个话题"
            f"（标签 {tag_desc}）· 图 {image_count}"
            + (f" · {len(chunks)} 段" if len(chunks) > 1 else "")
        )
        if incomplete:
            info += f" · ⚠ 缺 {len(failed)} 段（不完整）"
        logger.info(f"[{PLUGIN_NAME}] {info} -> {path.name}")
        self._prune_old_files()
        return path, info, False

    async def generate_all(
        self,
        since: float,
        until: float,
        label: str,
        incremental: bool,
        only_gid: str | None = None,
        umo: str = "",
    ) -> list[tuple[str, Path | None, str, bool]]:
        """返回 (群号, 图路径或 None, 说明, 是否报错)。"""
        results: list[tuple[str, Path | None, str, bool]] = []
        targets = [only_gid] if only_gid else list(self.group_ids)
        for gid in targets:
            try:
                path, info, is_error = await self.generate_group(
                    gid, since, until, label, incremental, umo
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

    # 私聊类消息类型白名单（各平台命名不同，统一按小写比对）
    _PRIVATE_TYPES = ("friendmessage", "privatemessage", "friend", "private", "temp")

    @staticmethod
    def _parse_umo(umo: str) -> tuple[str, str, str] | None:
        """把 unified_msg_origin 拆成 (平台实例, 消息类型, 目标)。格式不对返回 None。"""
        parts = str(umo or "").split(":")
        if len(parts) != 3:
            return None
        platform, mtype, target = (part.strip() for part in parts)
        if not platform or not mtype or not target:
            return None
        return platform, mtype, target

    def _push_target_ok(self) -> bool:
        """投递口必须是**格式正确的私聊会话**——「报告永不发回群里」不能只靠配置自觉。"""
        if not getattr(self, "push_enabled", False) or not getattr(self, "push_umo", ""):
            return False
        parsed = self._parse_umo(self.push_umo)
        if parsed is None:
            logger.error(
                f"[{PLUGIN_NAME}] push_umo 格式不对（{self.push_umo!r}），"
                "应形如 平台id:FriendMessage:QQ号；拒绝发送"
            )
            return False
        _platform, mtype, target = parsed
        lowered = mtype.lower()
        if "group" in lowered:
            logger.error(
                f"[{PLUGIN_NAME}] push_umo 指向群聊（{self.push_umo}），拒绝发送："
                "报告永不发回群里"
            )
            return False
        if lowered not in self._PRIVATE_TYPES:
            logger.error(
                f"[{PLUGIN_NAME}] push_umo 的消息类型 {mtype} 不像私聊，拒绝发送：{self.push_umo}"
            )
            return False
        if not target.isdigit():
            logger.error(
                f"[{PLUGIN_NAME}] push_umo 的收件人 {target!r} 不是数字 QQ 号，拒绝发送"
            )
            return False
        return True

    async def _push(self, path: Path) -> bool:
        """只发报告图，不带任何说明文字。"""
        if not self._push_target_ok():
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
        if not self._push_target_ok():
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
                        logger.info(f"[{PLUGIN_NAME}] 定时任务触发 {self.daily_time}")
                        if not self._claim():
                            # 忙碌时排队，别把今天这一份直接丢掉
                            if self._enqueue(("daily",)):
                                self._last_run_date = today
                                logger.warning(
                                    f"[{PLUGIN_NAME}] 上一轮还没跑完，今天的定时已排队，等它结束接着跑"
                                )
                            else:
                                logger.error(
                                    f"[{PLUGIN_NAME}] 队列满了，今天的定时跳过（明天 0 点后重新计）"
                                )
                        else:
                            self._last_run_date = today
                            await self._execute(("daily",))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 定时任务异常: {e}", exc_info=True)
            self._release_stale_claim()
            await asyncio.sleep(30)

    async def _run_daily_once(self) -> None:
        """定时档的一次执行：过去 24 小时、所有群、非增量、直接推送。

        23:00 触发就是「昨天 23:00 到现在」，而不是「上次之后的新消息」——
        后者一旦漏跑一次，那段就永远补不回来了。
        """
        since, until, label = self._daily_window(time.time())
        umo = self.push_umo or ""
        logger.info(f"[{PLUGIN_NAME}] 定时范围：{label}（{datetime.fromtimestamp(since):%m-%d %H:%M} 起）")
        # 带上收件会话：定时报告也要进索引，否则「刚才那份」只能靠全局最新去猜
        results = await self.generate_all(since, until, label, False, None, umo)
        for _gid, path, info, is_error in results:
            if path is None:
                logger.info(f"[{PLUGIN_NAME}] 定时跳过：{info}")
                if is_error:
                    await self._push_text(f"群聊情报出错：{info}")
                continue
            pushed = await self._push(path)
            rid = reports.report_id_from_image(path.name)
            if pushed:
                reports.mark_delivered(self.state, umo, rid)
            else:
                reports.mark_send_failed(self.state, umo, rid, reason="定时推送失败")
            self._save_state()

    @staticmethod
    def _daily_window(now: float) -> tuple[float, float, str]:
        """定时档的时间窗口：截至现在，往前整 24 小时。"""
        return now - 24 * 3600, now, "最近 24 小时"

    async def initialize(self) -> None:
        self._prune_old_files()
        if not self.owner_uids:
            logger.warning(
                f"[{PLUGIN_NAME}] 没有配置 owner_uids：现在任何私聊都不会生成报告"
                "（只会收到一条配置提示）。要用人，就在设置里填上自己的 QQ 号"
            )
        missing_key = [
            str(row.get("gid") or "?")
            for row in (self.config.get("chats") or [])
            if isinstance(row, dict) and not row.get("__template_key")
        ]
        if missing_key:
            logger.warning(
                f"[{PLUGIN_NAME}] 这些群的行缺少 __template_key（插件设置里会显示"
                f"「未指定模板」）：{missing_key}；建议在界面上删掉重新添加"
            )
        for gid in self.group_ids:
            try:
                await self._group_name(gid)  # 预热群名，免得刚启动时按名字找不到群
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 预热群名失败 {gid}: {e}")
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
    # 用户话里出现这些，才算"在要群聊总结"。实测模型会把"群里今天有什么好玩的"
    # 也当成要报告，所以工具入口再判一次，别让兜底变成乱跑。
    _TOOL_CUES = (
        "总结",
        "日报",
        "情报",
        "聊了啥",
        "聊了什么",
        "聊了些啥",
        "讨论了什么",
        "跑一份",
        "来一份",
        "出一份",
        "生成一份",
        "群聊",
    )

    def _tool_cue(self, event: AstrMessageEvent) -> bool:
        text = str(getattr(event, "message_str", "") or "")
        return any(cue in text for cue in self._TOOL_CUES)

    def _tool_allowed(self, event: AstrMessageEvent) -> bool:
        """只在私聊、且只有 owner_uids 里的人能用。群里一律拒绝，避免把报告发到群里。

        owner_uids 为空时一律拒绝（fail-closed）：群聊内容不是公开内容，宁可不能用，
        也不要默认让所有能私聊机器人的人都能拉报告。
        """
        try:
            if event.get_group_id():
                return False
        except Exception:
            return False
        if not self.owner_uids:
            return False
        try:
            return str(event.get_sender_id()) in self.owner_uids
        except Exception:
            return False

    @filter.llm_tool(name="group_digest")
    async def tool_group_digest(
        self, event: AstrMessageEvent, when: str = "", group: str = "", again: bool = False
    ) -> str:
        """生成一份新的群聊报告图并发给用户。

        只有在用户**明确要一份新的总结/日报**时才调用（"群里今天聊了啥"、"总结一下昨天的群"、
        "看看极东群"、"给我出个日报"、"群聊情报跑一份"）。如果用户是在讨论**已经发过的报告**
        （"第二条什么意思"、"这份靠谱吗"），改用 get_group_report；如果要查**原始消息**
        （"他原话怎么说的"、"后来有人验证了吗"），改用 query_group_records。
        随口问"群里有什么好玩的"这类也不要调用。报告图由本工具负责发送，调用后不要再输出文字。

        Args:
            when(string): 时间范围，直接用用户原话即可，例如 今天 / 昨天 / 前天 / 最近3天 / 上周 / 2026-11-05；用户没提时间就留空，表示只看没看过的消息
            group(string): 群名的一部分或群号，例如 考古 / 极东；用户没指定群就留空，表示所有群
            again(boolean): 用户要求"重跑一遍""再看一次"时传 true，忽略增量位重新总结该时段
        """
        if not self._tool_allowed(event):
            return "这个工具只能在私聊里由主人使用；群里不会发任何报告。"
        if not self._tool_cue(event):
            return "用户这句话不是在要群聊总结，不要调用本工具，正常回答即可。"
        if not self.group_ids:
            return "插件还没配置要监控的群，先让主人在「要总结的群」里加一行。"

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
        if again:
            incremental = False
        if group.strip() and not gid:
            # 模型给了一个群名却没匹配上：明说没有这个群，别默默改成「全部群」
            return (
                f"没有「{group}」这个群，这次不生成（免得白跑一遍浪费额度）。"
                f"现在配好的群：{await self._known_groups_text()}"
            )

        # 解析和拒绝都做完，才决定「立刻跑」还是「排队」——两种状态参数完全一样
        request = (
            "digest",
            event.unified_msg_origin,
            since,
            until,
            label,
            incremental,
            gid,
            True,
        )
        if not self._claim():
            if self._enqueue(request):
                return "上一份还在生成，这份已经排队，跑完接着发。"
            return "已经在跑一份、后面还排着队了，等它们跑完再叫我。"

        # 记下「这个会话在等哪份报告」：用户回头问「跑完了吗」才有依据
        target = "全部群" if gid is None else (
            self.group_name_map.get(gid) or f"群 {gid}"
        )
        reports.mark_generating(
            self.state,
            event.unified_msg_origin,
            gid=gid or "",
            group=target,
            label=label,
        )
        self._save_state()

        # 生成可能要好几分钟，先立刻回话，图由后台任务直接发到聊天里。
        # 执行位已经在这行之前同步占上了，所以连点两次也不会同时起两份。
        event.set_extra("chat_intel_tool_sent", True)
        try:
            asyncio.create_task(self._execute(request))
        except Exception:
            self._running = False
            raise
        logger.info(
            f"[{PLUGIN_NAME}] 工具调用：范围={label} 群={target} 原始「{when} {group}」"
        )
        return (
            f"已在后台开始生成（范围：{label}，目标：{target}），"
            "报告图会直接发到聊天里。不要再用文字复述或总结，保持沉默即可。"
        )

    # ------------------------------------------------------------------ 读取已有报告
    def _format_topic(self, topic: dict, detailed: bool = False, report_id: str = "") -> str:
        head = (
            f"话题 {topic.get('no')}｜{topic.get('title')}"
            f"｜{topic.get('time') or '时间未知'}｜{topic.get('count')} 条"
        )
        if topic.get("members"):
            head += f"｜{topic['members']}"
        if topic.get("tags"):
            head += f"｜标签 {'、'.join(str(t) for t in topic['tags'])}"
        lines = [head, f"  概括：{topic.get('summary') or '（无）'}"]
        if detailed:
            refs = [str(r) for r in (topic.get("refs") or []) if r]
            if refs:
                shown = "、".join(refs[:12])
                lines.append(
                    f"  来源消息引用（共 {len(refs)} 条）：{shown}{' …' if len(refs) > 12 else ''}"
                )
                call = f'query_group_records(report_id="{report_id}", msg_ids="{refs[0]}")'
                if not report_id:
                    call = f'query_group_records(msg_ids="{refs[0]}")'
                lines.append(
                    "  报告里只有引用、不含原文（原话以消息库为准）："
                    f"要原话就调 {call}，群和时间范围会自动带上。"
                )
            else:
                lines.append("  这份报告没有记下来源引用，只能看概括。")
        return "\n".join(lines)

    def _refs_status(self, record: dict) -> str:
        """报告里的引用，现在还能在本地消息库里核对几条——过期就直说。"""
        refs = {str(r) for topic in (record.get("topics") or []) for r in (topic.get("refs") or [])}
        if not refs:
            return "引用核对：这份报告没记来源引用。"
        gid = str(record.get("gid") or "")
        rng = record.get("range") or {}
        covered = record.get("covered") or {}
        marks = [
            float(x)
            for x in (rng.get("since"), rng.get("until"), covered.get("first"), covered.get("last"))
            if x
        ]
        since = min(marks) if marks else 0.0
        until = max(marks) if marks else time.time()
        present: set[str] = set()
        try:
            for rec in self._load_records(gid, since, until, limit=False):
                ref = reports.ref_of(rec)
                if ref in refs:
                    present.add(ref)
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 引用核对失败: {e}")
            return "引用核对：本地库读取失败，暂时无法核对原话。"
        if len(present) == len(refs):
            return f"引用核对：{len(refs)} 条引用都还能在本地消息库里查到原文。"
        if not present:
            return (
                f"引用核对：{len(refs)} 条引用一条都查不到了（原文已过保留期 {self.keep_days} 天），"
                "只能讨论报告里的概括，原话无法核对。"
            )
        return (
            f"引用核对：{len(present)}/{len(refs)} 条还能查到原文，"
            f"其余已过保留期或没采到（当前保留 {self.keep_days} 天）。"
        )

    def _format_report(self, record: dict, topic_no: int = 0) -> str:
        topics = [t for t in (record.get("topics") or []) if isinstance(t, dict)]
        rng = record.get("range") or {}
        covered = record.get("covered") or {}
        rid = str(record.get("id") or "")
        head = [
            f"报告 {rid}",
            f"群：{record.get('group') or '?'}（{record.get('gid') or '?'}）",
            f"范围：{rng.get('label') or '?'}｜实际覆盖 {covered.get('first_time') or '?'}"
            f" ~ {covered.get('last_time') or '?'}｜{record.get('msg_count')} 条消息",
        ]
        if record.get("platform"):
            head.append(f"读取账号：{record['platform']}")
        if record.get("status") == "incomplete":
            chunks = record.get("chunks") or {}
            failed = "、".join(str(x) for x in (chunks.get("failed") or []))
            head.append(
                f"生成：{record.get('generated_time')}｜⚠ 不完整（第 {failed}/{chunks.get('total')} 段失败，"
                "那部分内容没有被总结进去）"
            )
        else:
            head.append(f"生成：{record.get('generated_time')}｜完整")
        if record.get("truncated"):
            head.append("注意：这个时段消息太多，只取了最近一批，更早的部分可能没覆盖")
        if record.get("headline"):
            head.append(f"总览：{record['headline']}")
        head.append(self._refs_status(record))
        if not topics:
            return "\n".join(head + ["（这份报告里没有可用话题）"])
        if topic_no:
            picked = [t for t in topics if int(t.get("no") or 0) == int(topic_no)]
            if not picked:
                return "\n".join(
                    head + [f"这份报告只有 {len(topics)} 个话题，没有第 {topic_no} 个。"]
                )
            return "\n".join(head + ["", self._format_topic(picked[0], detailed=True, report_id=rid)])
        lines = head + ["", f"共 {len(topics)} 个话题："]
        lines += [self._format_topic(t, detailed=False) for t in topics]
        lines.append("")
        lines.append(
            f"要看某个话题的来源消息，用 get_group_report(report_id=\"{rid}\", topic_no=N)；"
            "要查后续或原话，用 query_group_records。"
        )
        return "\n".join(lines)

    def _pick_report(self, umo: str, report_id: str, gid: str) -> tuple[str, str]:
        """选一份报告：显式 id > 指定群 > 当前讨论焦点 > 会话最近一份 > 全局最新。"""
        if report_id.strip():
            return reports.report_id_from_image(report_id.strip()), ""
        entries = [e for e in reports.entries(self.state, umo) if e.get("id")]
        if gid:
            for entry in reversed(entries):
                if entry.get("gid") == gid and entry.get("status") != "expired":
                    return str(entry.get("id") or ""), ""
            found = reports.newest(self.reports_dir, gid, 1)
            if found:
                return found[0], ""
            return "", f"群 {gid} 还没有保存过的报告（报告与原文都保留 {self.keep_days} 天）。"
        focus = reports.get_focus(self.state, umo)
        focused = str(focus.get("report_id") or "")
        if focused and (
            reports.load(self.reports_dir, focused) or reports.image_of(self.reports_dir, focused)
        ):
            return focused, ""
        usable = [e for e in entries if e.get("status") != "expired"]
        if usable:
            return str(usable[-1].get("id") or ""), ""
        found = reports.newest(self.reports_dir, None, 1)
        if found:
            return found[0], ""
        return "", "还没有可以读取的报告：先让我生成一份（group_digest），之后才能讨论它。"

    @filter.llm_tool(name="get_group_report")
    async def tool_get_group_report(
        self, event: AstrMessageEvent, report_id: str = "", topic_no: int = 0, gid: str = ""
    ) -> str:
        """读取已经生成过的群聊报告正文（不是重新生成报告）。

        用户讨论「刚才那份报告」「第二个话题」「这条结论靠谱吗」时用这个。返回报告的范围、
        完整性、总览和各话题概括；指定 topic_no 时返回该话题的概括和来源消息引用。
        报告里不含原文，要原话或报告之后的原始消息（「后来有人验证了吗」），
        把引用传给 query_group_records 去消息库里取。

        Args:
            report_id(string): 报告 ID，提示词里会给出（例如 chat_intel_220001_20261108_120000_今天）；留空表示这个会话最近收到的那份
            topic_no(int): 只看第几个话题（从 1 开始）；0 表示返回全部话题
            gid(string): 群号或群名片段；想读某个群更早的报告时填
        """
        if not self._tool_allowed(event):
            return "这个工具只能在私聊里由主人使用；群里不会输出任何报告内容。"
        umo = event.unified_msg_origin
        wanted_gid = ""
        if gid.strip():
            hit, _ = self._match_group(gid)
            if not hit:
                # 写错群名不许悄悄回退到别的群：那会答错对象
                return f"没有「{gid}」这个群。现在配好的群：{await self._known_groups_text()}"
            wanted_gid = hit
        rid, why = self._pick_report(umo, report_id, wanted_gid)
        if not rid:
            return why
        record = reports.load(self.reports_dir, rid)
        if record is None:
            if reports.image_of(self.reports_dir, rid):
                return (
                    f"报告 {rid} 是升级前生成的：只有图片，没有结构化正文，所以没法讨论它的细节。"
                    "需要细节的话让我重新生成一份。"
                )
            return (
                f"找不到报告 {rid}：可能已过保留期（报告与原文都保留 {self.keep_days} 天），"
                "或者这个 id 不对。"
            )
        text = self._format_report(record, int(topic_no or 0))
        # 记下讨论焦点：下一轮省略 ID 时接着聊这份，而不是跳回最新一份
        reports.set_focus(self.state, umo, rid, int(topic_no or 0))
        self._save_state()
        return text

    async def _scan_records(self, gid: str, since: float, until: float) -> list[dict]:
        """查询专用：先取全再匹配，不按「总结上限」截断。

        总结时截断是合理的（模型吃不下那么多），但按引用找人时若先截断，
        早于最近 max_messages 条的消息就永远查不到了。
        """
        records = (
            self._load_records(gid, since, until, limit=False) if self.use_local_store else []
        )
        if len(records) < 20:
            records = await self._merge_history(gid, since, until, records)
        records.sort(key=lambda r: int(r.get("t") or 0))
        if len(records) > QUERY_SCAN_LIMIT:
            records = records[-QUERY_SCAN_LIMIT:]
        return records

    async def _merge_history(
        self, gid: str, since: float, until: float, records: list[dict]
    ) -> list[dict]:
        """回平台补拉一段历史，和不截断地并进现有记录。"""
        try:
            history = await self._fetch_history(gid, since, until)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 查询补拉 {gid} 失败: {e}")
            return records
        if not history:
            return records
        return self._merge_records(records, history, limit=False)

    @staticmethod
    def _hit_kind(rec: dict, wanted: list[str], words: list[str]) -> bool:
        if wanted:
            ref = reports.ref_of(rec)
            return str(rec.get("mid") or "") in wanted or bool(ref and ref in wanted)
        if words:
            text = str(rec.get("text") or "")
            return any(word in text for word in words)
        return False

    async def _collect_hits(
        self,
        targets: list[str],
        since: float,
        until: float,
        wanted: list[str],
        words: list[str],
        browse: bool = False,
    ) -> tuple[list[tuple[str, list[dict], int]], int, list[int], bool]:
        hits: list[tuple[str, list[dict], int]] = []
        scanned = 0
        covered: list[int] = []
        truncated = False
        for cid in targets:
            try:
                records = await self._scan_records(cid, since, until)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 查询 {cid} 记录失败: {e}")
                continue
            found = (
                list(range(len(records)))
                if browse
                else [i for i, rec in enumerate(records) if self._hit_kind(rec, wanted, words)]
            )
            if wanted and not found:
                # 报告里的引用可能早于本地留存：本地没命中就回平台补拉一次
                merged = await self._merge_history(cid, since, until, records)
                if len(merged) > len(records):
                    records = merged
                    found = [
                        i for i, rec in enumerate(records) if self._hit_kind(rec, wanted, words)
                    ]
            scanned += len(records)
            if len(records) >= QUERY_SCAN_LIMIT:
                truncated = True
            covered += [int(rec.get("t") or 0) for rec in records if rec.get("t")]
            hits += [(cid, records, i) for i in found]
        return hits, scanned, covered, truncated

    @filter.llm_tool(name="query_group_records")
    async def tool_query_records(
        self,
        event: AstrMessageEvent,
        report_id: str = "",
        msg_ids: str = "",
        keyword: str = "",
        gid: str = "",
        when: str = "",
        offset: int = 0,
    ) -> str:
        """查被监控群里的原始消息（带时间、发送者、引用），用来查证原话和后续。

        适合「他原话怎么说的」「后来有人验证成功了吗」「这是实测还是猜测」「今天还有人讨论吗」。
        命中消息会带上一条和下一条作上下文：命中本身带编号，上下文标「·」，同一句不会重复出现；
        结果会说明覆盖范围、是否被截断、还有多少条没显示。
        从报告里看到引用时把 report_id 一起传进来，群和时间范围会自动取，不用自己算日期。

        Args:
            report_id(string): 报告 ID（读报告时会给）；填了就用这份报告的群和时间范围
            msg_ids(string): 消息引用（mid 或「发送者@时间」），逗号或空格分隔；填了就按引用精确取，忽略 keyword
            keyword(string): 关键词，空格分隔，命中任意一个就算
            gid(string): 群号或群名片段；有 report_id 时不用填，留空表示所有被监控的群
            when(string): 时间范围，用用户原话（今天/昨天/前天/最近3天/本周/上周/2026-11-05）；有 report_id 时默认用它自己的范围；都没有则最近 24 小时
            offset(int): 从第几条命中开始显示（默认 0），用于继续读取
        """
        if not self._tool_allowed(event):
            return "这个工具只能在私聊里由主人使用；群里不会输出任何群聊原文。"
        if not self.group_ids:
            return "插件还没配置要监控的群。"
        for _gid in self.group_ids:  # 预热群名，否则按名字匹配不到
            await self._group_name(_gid)

        record = None
        if report_id.strip():
            record = reports.load(
                self.reports_dir, reports.report_id_from_image(report_id.strip())
            )
            if record is None:
                return (
                    f"找不到报告 {report_id}（可能已过保留期），拿不到它的时间范围。"
                    "可以直接用 when= 指定时间再查。"
                )

        targets = list(self.group_ids)
        if record is not None:
            rid_gid = str(record.get("gid") or "")
            if rid_gid in self.group_ids:
                targets = [rid_gid]
        elif gid.strip():
            hit, _ = self._match_group(gid)
            if not hit:
                return f"没有「{gid}」这个群。现在配好的群：{await self._known_groups_text()}"
            targets = [hit]

        if record is not None and not when.strip():
            rng = record.get("range") or {}
            covered_range = record.get("covered") or {}
            since = float(rng.get("since") or covered_range.get("first") or 0)
            until = float(rng.get("until") or covered_range.get("last") or 0)
            if covered_range.get("first"):
                since = min(x for x in (since, float(covered_range["first"])) if x)
            if covered_range.get("last"):
                until = max(until, float(covered_range["last"]))
            if not since or not until:
                now = time.time()
                since, until = now - 24 * 3600, now
            label = f"报告 {record.get('id')} 的范围（{rng.get('label') or '未标注'}）"
        elif when.strip():
            rng, _matched = self._match_range(when)
            if rng is None:
                return (
                    f"看不懂「{when}」这个时间范围。现在只认：今天 / 昨天 / 前天 / 最近3天 / 本周 / "
                    "上周 / 2026-11-05 / 11-05；要查更早的消息请直接说日期。"
                )
            since, until, label, _incremental = rng
        else:
            now = time.time()
            since, until, label = now - 24 * 3600, now, "最近 24 小时"

        wanted = [x for x in re.split(r"[,\s，、]+", msg_ids.strip()) if x]
        words = [w for w in re.split(r"[\s，,]+", keyword.strip()) if w]
        # 只给时间范围也放行：当成「这段时间都聊了什么」，按时间列最近一页
        browse = not wanted and not words

        hits, scanned, covered, truncated = await self._collect_hits(
            targets, since, until, wanted, words, browse
        )

        if not hits:
            scope = f"{label}" + (
                f"，群 {targets[0]}" if len(targets) == 1 else f"，{len(targets)} 个群"
            )
            note = ""
            if not scanned:
                note = (
                    f"（这段时间没有取到任何记录：可能已过保留期——当前保留 {self.keep_days} 天，"
                    "也可能当时没采集到）"
                )
            elif wanted:
                note = (
                    "（这些引用在本地库里和平台历史里都没找到：可能已过保留期，或当时没采到。"
                    "别据此推断「他没说过」）"
                )
            else:
                note = "（这些关键词在这段时间的记录里没有命中，不代表没聊过，只代表没有字面匹配）"
            return f"{scope}：没有找到符合条件的消息{note}"

        if browse:
            # 浏览模式：默认给最近的一页，offset 从末尾往前数
            end = max(0, len(hits) - max(0, int(offset or 0)))
            start = max(0, end - QUERY_PAGE)
            page = hits[start:end]
        else:
            start = max(0, int(offset or 0))
            page = hits[start : start + QUERY_PAGE]
        lines = [
            f"原始消息（{label}，{'全部群' if len(targets) > 1 else targets[0]}）"
            f"：{'共 ' + str(len(hits)) + ' 条，' if browse else '命中 ' + str(len(hits)) + ' 条，'}"
            f"显示第 {start + 1}–{start + len(page)} 条"
        ]
        if covered:
            first = datetime.fromtimestamp(min(covered)).strftime("%m-%d %H:%M")
            last = datetime.fromtimestamp(max(covered)).strftime("%m-%d %H:%M")
            lines.append(f"本地覆盖：{first} ~ {last}，共扫过 {scanned} 条")
        if truncated:
            lines.append(f"注意：单群只扫最近 {QUERY_SCAN_LIMIT} 条，更早的部分可能没扫到")
        if browse:
            lines.append("没给关键词，按时间顺序列出这段时间的消息")
        else:
            lines.append("带编号的是命中的消息，「·」是它前后的上下文")
        lines.append("")
        # 先把要打印的行规划出来：命中优先，上下文不吞掉命中（否则命中行会丢掉编号和 id）
        num_of = {(cid, index): start + i + 1 for i, (cid, _records, index) in enumerate(page)}
        rows: dict[tuple[str, int], tuple[bool, dict]] = {}
        for cid, records, index in page:
            if browse:
                rows[(cid, index)] = (True, records[index])
                continue
            after = QUERY_CTX_AFTER if wanted else QUERY_CTX_BEFORE
            lo = max(0, index - QUERY_CTX_BEFORE)
            hi = min(len(records) - 1, index + after)
            for j in range(lo, hi + 1):
                is_hit = (cid, j) in num_of
                if is_hit or (cid, j) not in rows:
                    rows[(cid, j)] = (is_hit, records[j])
        group_rank = {cid: i for i, cid in enumerate(targets)}
        for key in sorted(rows, key=lambda k: (group_rank.get(k[0], 99), k[1])):
            cid, j = key
            is_hit, rec = rows[key]
            ts = int(rec.get("t") or 0)
            stamp = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "--:--"
            name = sanitize_name(rec.get("name")) or str(rec.get("uid") or "?")
            text = str(rec.get("text") or "").strip() or "[图]"
            if rec.get("imgs") and "[图]" not in text:
                text += " [图]"
            tag = f"群{cid}｜" if len(targets) > 1 else ""
            if is_hit:
                pos = num_of.get((cid, j), "")
                lines.append(
                    f"{pos}. [{stamp}] {tag}{name}：{text}"
                    f"（id={reports.ref_of(rec) or '无'}）"
                )
            else:
                lines.append(f"   · [{stamp}] {name}：{text}")
        if browse:
            if start > 0:
                lines.append("")
                lines.append(
                    f"这段更早还有 {start} 条，用 offset={int(offset or 0) + len(page)} 接着往前看。"
                )
        elif len(hits) > start + len(page):
            lines.append("")
            lines.append(
                f"还有 {len(hits) - start - len(page)} 条，用 offset={start + len(page)} 继续读，"
                "或换个更窄的关键词。"
            )
        lines.append("")
        lines.append("要看他后面还说了什么，把 when 往后挪（例如 when=最近3天）再查一次。")
        return "\n".join(lines)

    def _last_report_desc(self, gid: str) -> str:
        """状态行里「上次生成」的口径：看真实生成时间，不看增量游标。"""
        group_state = self._group_state(gid)
        ts = int(float(group_state.get("last_report_ts") or 0))
        if not ts:
            return "上次生成 还没生成过"
        when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        rng = str(group_state.get("last_range") or "").strip()
        desc = f"上次生成 {when}" + (f"（{rng}）" if rng else "")
        if group_state.get("last_incomplete"):
            desc += " · 内容不完整"
        return desc

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
                    f"{self._last_report_desc(gid)}"
                )
        parsed = self._parse_umo(self.push_umo)
        send_desc = (
            f"{parsed[0]} → QQ {parsed[2]}"
            if parsed
            else (self.push_umo or "未配置（不会自动推送）")
        )
        lines.append(
            f"发送账号 {send_desc} · 静默账号 "
            f"{'、'.join(self.silence_platforms) or '无（不做静音）'}"
            f"（拦下事件 {self._silenced} / LLM {self._llm_blocked} / 回复 {self._result_blocked}）"
        )
        latest = self._latest_report()
        lines.append(f"最近报告：{latest.name if latest else '无'}")
        push_state = f"开（{send_desc}）" if self.push_enabled and self.push_umo else "关"
        if self.push_enabled and not self._push_target_ok():
            reason = self._push_cfg_error or f"{self.push_umo or '目标为空'} 不是合法的私聊会话"
            push_state = f"⚠ 配置有问题（{reason}），不会发送"
        daily_desc = self.daily_time or "关闭"
        if self._daily_cfg_error:
            daily_desc = f"⚠ {self._daily_cfg_error}"
        lines.append(
            f"定时生成 {daily_desc} · 自动推送 {push_state} · "
            f"上限 {self.max_messages} 条 · 每个话题最多 {self.max_images} 张图 · "
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
        announce: bool = False,
    ) -> None:
        try:
            results = await self.generate_all(since, until, label, incremental, only_gid, umo)
            for _gid, path, info, is_error in results:
                if path is None:
                    logger.info(f"[{PLUGIN_NAME}] 跳过：{info}")
                    if is_error:
                        await self.context.send_message(
                            umo, MessageChain().message(f"出错了：{info}")
                        )
                    elif announce:
                        # 用户主动问的，即使没东西也得回一句，不能当没听见
                        await self.context.send_message(umo, MessageChain().message(info))
                    continue
                delivered = await self.context.send_message(
                    umo, MessageChain([AstrImage.fromFileSystem(str(path))])
                )
                # 索引要跟真实投递结果对齐：生成出来了 ≠ 发出去了
                rid = reports.report_id_from_image(path.name)
                if delivered is False:
                    logger.warning(f"[{PLUGIN_NAME}] {rid} 发送接口返回失败，保留为未投递")
                    reports.mark_send_failed(self.state, umo, rid, reason="发送接口返回失败")
                else:
                    reports.mark_delivered(self.state, umo, rid)
                self._save_state()
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 生成流程异常: {e}", exc_info=True)
            reports.mark_failed(self.state, umo, reason=str(e)[:120])
            try:
                await self.context.send_message(umo, MessageChain().message(f"生成失败：{e}"))
            except Exception:
                pass

    def _migrate_legacy_config(self) -> None:
        """把老配置里隐藏的旧值搬进可见字段，只做一次。"""
        if self.state.get("config_migrated_v224"):
            return
        before = (
            str(self.config.get("push_platform") or ""),
            str(self.config.get("push_uid") or ""),
        )
        self.migrate_push_target(self.config, self.state)
        after = (
            str(self.config.get("push_platform") or ""),
            str(self.config.get("push_uid") or ""),
        )
        target = after[0] and after[1]
        filled = self.migrate_chat_platforms(
            self.config.get("chats"), getattr(self, "legacy_platform_id", "")
        )
        if filled:
            logger.warning(
                f"[{PLUGIN_NAME}] 这些群原来靠全局 platform_id 读历史，已补进「读取账号」："
                f"{filled}；建议在设置里改成明确的账号"
            )
        migrated = (after != before) or bool(filled)
        if migrated and not self._save_config():
            logger.error(
                f"[{PLUGIN_NAME}] 迁移结果写不回配置文件，下次启动会再试一次"
            )
        # 迁移可能改了 config，重新按最终值算一遍
        if filled:
            self._apply_chats(self.config.get("chats"))
        self.push_umo, self._push_cfg_error = self.build_push_target(self.config)
        try:
            self._save_state()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 迁移标记写入失败: {e}")

    def _save_config(self) -> bool:
        """把内存里的 config 落盘（AstrBot 的 AstrBotConfig 才有 save_config）。"""
        saver = getattr(self.config, "save_config", None)
        if saver is None:
            return True  # 普通 dict：内存里改完就算数
        try:
            saver()
            return True
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 配置保存失败: {e}")
            return False

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
        # 报告存档跟原文同寿：原文没了，「他原话怎么说的」就答不了，留着报告只会
        # 指向一片空白。以前给「每组最新一份」开了永久保护，结果报告比原文活得久，
        # 引用全部失效——现在两边一起过期。
        removed_reports, removed_report_imgs = reports.prune(
            self.reports_dir, self.keep_days, set()
        )
        reports.drop_expired(self.state, self.reports_dir)

        if removed_msgs or removed_imgs or removed_reports or removed_report_imgs:
            logger.info(
                f"[{PLUGIN_NAME}] 清理过期数据（保留 {self.keep_days} 天）："
                f"消息文件 {removed_msgs} 个、图片 {removed_imgs} 张、"
                f"报告 {removed_reports} 份、报告图 {removed_report_imgs} 张"
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
            # 缓存键是「兴趣指纹:文件名」，alive 里只有文件名，比对时要去掉前缀
            stale = [k for k in cache if str(k).split(":", 1)[-1] not in alive]
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
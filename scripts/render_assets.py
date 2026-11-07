"""渲染 logo 与 README 样张（离线，不需要 AstrBot / 不需要模型）。

    python scripts/render_assets.py --fonts ./fonts
    python scripts/render_assets.py --fonts ./fonts --scene light
"""

import argparse
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import typst

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATE = ROOT / "templates" / "report.typ"
LOGO = ROOT / "assets" / "logo.typ"

FOOTER = f"生成于 {datetime.now():%Y-%m-%d %H:%M} · astrbot_plugin_chat_intel · 本报告不会发到群里"

# 群里贴的报错截图（样张占位，用 Typst 画，避免往仓库塞真实群图）
SHOT_TERMINAL = """
#set page(width: 720pt, height: auto, margin: 0pt, fill: rgb("#0f172a"))
#set text(font: ("ShareTechMono", "Noto Sans SC"), size: 12pt, fill: rgb("#cbd5e1"))
#block(inset: 18pt)[
  #text(fill: rgb("#67e8f9"), "$ npm run build")
  #linebreak()
  #text(fill: rgb("#fca5a5"), "ERROR in ./src/app.tsx:42:18")
  #linebreak()
  #text(fill: rgb("#fca5a5"), "TS2345: Argument of type 'string' is not assignable to parameter of type 'number'.")
  #linebreak()
  #text(fill: rgb("#94a3b8"), "    42 |   const total = sum(items)")
  #linebreak()
  #text(fill: rgb("#94a3b8"), "       |                  ^")
  #linebreak()
  #v(8pt)
  #text(fill: rgb("#94a3b8"), "webpack 5.91.0 compiled with 1 error in 3142 ms")
]
"""

SHOT_CHART = """
#set page(width: 720pt, height: auto, margin: 0pt, fill: rgb("#ffffff"))
#set text(font: ("Inter", "Noto Sans SC"), size: 12pt, fill: rgb("#334155"))
#let bars = ((38, "a"), (52, "b"), (120, "c"), (46, "d"))
#block(inset: 20pt)[
  #text(size: 14pt, weight: "bold", fill: rgb("#0f172a"), "接口耗时 p99（ms）")
  #v(16pt)
  #grid(
    columns: (1fr,) * 4,
    align: center + bottom,
    gutter: 12pt,
    ..bars.map(((h, lab)) => block(
      width: 46pt,
      height: h * 2.4pt,
      fill: if h > 100 { rgb("#f97316") } else { rgb("#3b82f6") },
      radius: 3pt,
    )),
  )
  #v(8pt)
  #grid(
    columns: (1fr,) * 4,
    align: center,
    gutter: 12pt,
    ..bars.map(((h, lab)) => text(size: 10pt, fill: rgb("#64748b"), lab)),
  )
  #v(10pt)
  #text(size: 10pt, fill: rgb("#94a3b8"), "c 版本回归到 120ms，定位在缓存 key 少了版本号")
]
"""

# 样张 1：内容较满的一群（MeowShelter 的四个兴趣点命中三个）
SCENE_FULL = {
    "title": "群聊情报",
    "date": "2026-11-07",
    "group": "MeowShelter",
    "stats": {"line": "1286 条消息 · 采纳 9 条 · 上限截断 0 条 · 今天"},
    "headline": "今天群里主要在修 CI、聊模型评测，以及暑期实习投递。",
    "sections": [
        (
            "计算机与编程（语言、框架、报错原文、方案讨论）",
            [
                {
                    "topic": "CI 报错与持续集成",
                    "detail": "A 同学的流水线在 lint 阶段挂掉，报错是 eslint 9 与 @typescript-eslint "
                    "版本不匹配；B 同学指出报错要落到具体步骤名上才好定位，最后拆成 lint / test "
                    "两个 job（有配图）。",
                    "kind": "讨论",
                },
                {
                    "topic": "接口 p99 回归",
                    "detail": "E 同学发现 c 环境 p99 从 52ms 涨到 120ms，最后定位在缓存 key 少了版本号，"
                    "命中率掉到 0；补上版本号后回到 46ms（有配图）。",
                    "kind": "排障",
                },
            ],
        ),
        (
            "AI 与模型（发布、评测、用法、踩坑）",
            [
                {
                    "topic": "长上下文实测",
                    "detail": "群里在传某模型新版本支持 200k 上下文，F 同学拿三份 PDF 做了检索测试，"
                    "发现长文档中段的召回明显变差，结论是还不如分块。",
                    "kind": "评测",
                },
                {
                    "topic": "日报流程自动化",
                    "detail": "G 同学用 Agent 框架把日报从拉取到排版全自动跑通，成本比人工低一个量级，"
                    "但提醒模型偶尔会把两个群的内容混在一起。",
                    "kind": "玩法",
                },
            ],
        ),
        (
            "工作与就业（招聘、面试、简历、行业动向）",
            [
                {
                    "topic": "暑期实习开投",
                    "detail": "某厂 2026 暑期实习开始投递，后端岗位要求熟悉 Go 与 MySQL；"
                    "H 同学说简历投出去两天就有笔试邀请。",
                    "kind": "招聘",
                },
                {
                    "topic": "面试手写题",
                    "detail": "I 同学面试反馈：手写 LRU 是高频题，先把双向链表 + 哈希写完再谈优化。",
                    "kind": "面试",
                },
            ],
        ),
    ],
    "quotes": [
        {
            "content": "这个报错不是我的问题，是流水线的问题。",
            "sender": "A同学",
            "reason": "经典甩锅句式，后面被打脸说 yml 里少写了一步，技术性打脸。",
        },
        {
            "content": "缓存不是玄学，是你把顺序写错了。",
            "sender": "E同学",
            "reason": "一句话把缓存命中的因果讲清楚，比教程好记。",
        },
    ],
    "footer": FOOTER,
}

# 样张 2：另一群、另一套兴趣（复古硬件），并演示「上限截断」
SCENE_LIGHT = {
    "title": "群聊情报",
    "date": "2026-11-08",
    "group": "極東電子考古學結社",
    "stats": {"line": "412 条消息 · 采纳 15 条 · 上限截断 2 条 · 昨天"},
    "headline": "昨天在推进 x86 单板机：从选型、面包板原型到跑通 NetBSD。",
    "sections": [
        (
            "复古计算机与老式硬件（当年的机器、芯片、接口、外设）",
            [
                {
                    "topic": "80386EX 选型",
                    "detail": "J 同学打算用集成度高的 80386EX 做 x86 单板机，K 同学提到 X86SoC 没用过，"
                    "但电梯控制板里有类似方案。",
                    "kind": "选型",
                }
            ],
        ),
        (
            "维修、改装与自制经验（焊接、替换件、调试踩坑）",
            [
                {
                    "topic": "面包板原型跑通",
                    "detail": "J 同学先做面包板飞线原型，跑完裸机程序后移植 RTOS，最后试 NetBSD；"
                    "期间发现 RAM 芯片选型是最大的坑。",
                    "kind": "自制",
                }
            ],
        ),
        (
            "老系统、老软件与文献档案（旧系统、老游戏、手册、扫描件）",
            [
                {
                    "topic": "F22 上的 80386",
                    "detail": "L 同学提到 F22 子系统的主处理器是 80386，属于冷门硬件逸闻，记录里没有进一步核实。",
                    "kind": "逸闻",
                }
            ],
        ),
    ],
    "quotes": [
        {
            "content": "1G 以内分配参考面，线宽算一下，不要割裂参考面就行。",
            "sender": "M同学",
            "reason": "简短的高速布线经验，信息量足。",
        }
    ],
    "footer": FOOTER,
}

# 样张 3：这段时间没有命中兴趣的内容
SCENE_QUIET = {
    "title": "群聊情报",
    "date": "2026-11-09",
    "group": "MeowShelter",
    "stats": {"line": "233 条消息 · 采纳 0 条 · 上限截断 0 条 · 最新消息"},
    "headline": "",
    "sections": [],
    "quotes": [],
    "footer": FOOTER,
}

SCENES = {
    "full": (SCENE_FULL, (SHOT_TERMINAL, SHOT_CHART)),
    "light": (SCENE_LIGHT, ()),
    "quiet": (SCENE_QUIET, ()),
}


def _compile_typst_source(source: str, out: Path, fonts: str, ppi: int) -> None:
    job = Path(tempfile.mkdtemp(prefix="chat_intel_shot_"))
    try:
        (job / "main.typ").write_text(source, encoding="utf-8")
        kwargs = {"font_paths": [fonts]} if fonts else {}
        typst.compile(
            str(job / "main.typ"),
            output=str(out),
            root=str(job),
            format="png",
            ppi=ppi,
            **kwargs,
        )
    finally:
        shutil.rmtree(job, ignore_errors=True)


def render_report(scene: str, out: Path, fonts: str, ppi: int) -> None:
    payload, shots = SCENES[scene]
    job = Path(tempfile.mkdtemp(prefix="chat_intel_"))
    try:
        shutil.copyfile(TEMPLATE, job / "main.typ")
        img_dir = job / "img"
        img_dir.mkdir(exist_ok=True)
        payload = dict(payload)
        payload["images"] = []
        for i, source in enumerate(shots):
            dst = img_dir / f"{i}.png"
            _compile_typst_source(source, dst, fonts, 140)
            payload["images"].append(
                {"path": f"img/{dst.name}", "caption": f"示例成员{i + 1} · 11-07 1{i + 2}:04"}
            )
        (job / "data.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        kwargs = {"font_paths": [fonts]} if fonts else {}
        typst.compile(
            str(job / "main.typ"),
            output=str(out),
            root=str(job),
            format="png",
            ppi=ppi,
            **kwargs,
        )
    finally:
        shutil.rmtree(job, ignore_errors=True)


def render_logo(out: Path, fonts: str) -> None:
    kwargs = {"font_paths": [fonts]} if fonts else {}
    typst.compile(
        str(LOGO), output=str(out), root=str(ROOT), format="png", ppi=144, **kwargs
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fonts", default="", help="字体目录")
    parser.add_argument("--ppi", type=int, default=160)
    parser.add_argument("--out-dir", default=str(ROOT / "docs"))
    parser.add_argument(
        "--scene",
        default="all",
        choices=["all", "logo", *SCENES.keys()],
        help="要渲染哪个场景",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.scene in ("all", "logo"):
        render_logo(out_dir / "logo.png", args.fonts)
        print(f"  logo      -> {out_dir / 'logo.png'}")
    targets = list(SCENES.keys()) if args.scene == "all" else [args.scene]
    for scene in targets:
        if scene == "logo":
            continue
        out = out_dir / f"sample-{scene}.png"
        render_report(scene, out, args.fonts, args.ppi)
        print(f"  {scene:9} -> {out}  ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

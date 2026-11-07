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

FOOTER = f"生成于 {datetime.now():%Y-%m-%d %H:%M} · astrbot_plugin_tech_digest · 本报告不会发到群里"

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

SCENE_FULL = {
    "title": "技术情报日报",
    "date": "2026-11-07",
    "group": "MeowShelter",
    "stats": {"line": "1286 条消息 · 34 人参与 · 今天"},
    "headline": "今天群里主要在修 CI、聊论文 novelty，以及远程开发机怎么配。",
    "topics": [
        {
            "title": "CI 报错与持续集成",
            "detail": "A 同学的流水线在 lint 阶段挂掉，报错是 eslint 9 与 @typescript-eslint 版本不匹配；"
            "B 同学指出持续集成只管「每次提交都跑一遍」，报错要落到具体步骤名上才好定位，"
            "最后改成拆成 lint / test 两个 job（有配图）。",
            "people": "A同学、B同学",
        },
        {
            "title": "论文审稿与 novelty",
            "detail": "C 同学论文二审被审稿人指出 novelty 不足。D 同学给出「逐条 rebuttal + 补一个消融实验」的方案，"
            "并提醒公开评审下意见是公开的，别硬刚。",
            "people": "C同学、D同学",
        },
        {
            "title": "接口 p99 回归",
            "detail": "E 同学发现 c 环境 p99 从 52ms 涨到 120ms，最后定位在缓存 key 少了版本号，"
            "命中率掉到 0；补上版本号后回到 46ms（有配图）。",
            "people": "E同学、F同学",
        },
    ],
    "tools": [
        {"name": "Typst", "url": "https://typst.app", "note": "现代排版系统，编译快，适合生成报表长图"},
        {"name": "Tailscale", "url": "https://tailscale.com", "note": "基于 WireGuard 的组网，穿透内网不用配端口映射"},
        {"name": "astrbot_plugin_tech_digest", "url": "https://github.com/Blue-radish/astrbot_plugin_tech_digest", "note": "本插件：把群聊压成一份技术简报"},
    ],
    "jobs": [
        {"text": "某厂 2026 暑期实习开始投递，后端岗位要求熟悉 Go 与 MySQL", "who": "G同学"},
        {"text": "面试反馈：手写 LRU 是高频题，先把双向链表 + 哈希写完再谈优化", "who": "H同学"},
    ],
    "ai_news": [
        {"text": "群里在传某模型新版本支持 200k 上下文，实测长文档检索效果一般"},
        {"text": "有人用 Agent 框架把日报流程全自动化，成本比人工低一个量级"},
    ],
    "quotes": [
        {
            "content": "这个报错不是我的问题，是流水线的问题。",
            "sender": "A同学",
            "reason": "经典甩锅句式，后面被打脸说 yml 里少写了一步，技术性打脸。",
        },
        {
            "content": "novelty 不够就补实验，不要跟审稿人对喷。",
            "sender": "D同学",
            "reason": "科研圈的实用主义，比任何情绪输出都值钱。",
        },
    ],
    "footer": FOOTER,
}

SCENE_LIGHT = {
    "title": "技术情报日报",
    "date": "2026-11-08",
    "group": "極東電子考古學結社",
    "stats": {"line": "412 条消息 · 21 人参与 · 昨天"},
    "headline": "今天只有零星技术讨论：一个 Docker 构建缓存问题，加一个开源项目推荐。",
    "topics": [
        {
            "title": "Docker 构建缓存失效",
            "detail": "J 同学发现 CI 里每次 build 都重新拉依赖，原因是 Dockerfile 里先 COPY . . 再 "
            "npm ci，缓存层被源码变更打穿；改成先 COPY package*.json 再 npm ci 后恢复。",
            "people": "J同学",
        }
    ],
    "tools": [
        {"name": "uv", "url": "https://github.com/astral-sh/uv", "note": "Rust 写的 Python 包管理器，装依赖比 pip 快得多"},
    ],
    "jobs": [],
    "ai_news": [],
    "quotes": [
        {
            "content": "缓存不是玄学，是你把顺序写错了。",
            "sender": "J同学",
            "reason": "一句话把 Docker 层缓存的因果讲清楚，比教程好记。",
        }
    ],
    "footer": FOOTER,
}

SCENE_QUIET = {
    "title": "技术情报日报",
    "date": "2026-11-09",
    "group": "MeowShelter",
    "stats": {"line": "233 条消息 · 15 人参与 · 最新消息"},
    "headline": "",
    "topics": [],
    "tools": [],
    "jobs": [],
    "ai_news": [],
    "quotes": [],
    "footer": FOOTER,
}

SCENES = {
    "full": (SCENE_FULL, (SHOT_TERMINAL, SHOT_CHART)),
    "light": (SCENE_LIGHT, ()),
    "quiet": (SCENE_QUIET, ()),
}


def _compile_typst_source(source: str, out: Path, fonts: str, ppi: int) -> None:
    job = Path(tempfile.mkdtemp(prefix="tech_digest_shot_"))
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
    job = Path(tempfile.mkdtemp(prefix="tech_digest_"))
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

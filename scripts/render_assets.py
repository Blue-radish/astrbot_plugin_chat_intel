"""渲染 logo 与 README 样张（离线，不需要 AstrBot / 不需要模型）。

    python scripts/render_assets.py --fonts ./fonts
    python scripts/render_assets.py --fonts ./fonts --scene light
"""

import argparse
import json
import sys
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import typst

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(ROOT))
from interest import split_names  # noqa: E402  与插件用同一套「群友 ID」切分
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
# 样张 1：内容较满的一群（多个话题，带标签、时间与参与人）
SCENE_FULL = {
    "title": "群聊情报",
    "date": "2026-11-07",
    "group": "MeowShelter",
    "stats": {"line": "1286 条消息 · 4 个话题 · 今天"},
    "headline": "今天群里主要在修 CI、测长上下文，顺带把暑期实习投递通道聊了一遍。",
    "topics": [
        {
            "title": "eslint 9 与 typescript-eslint 版本打架",
            "names": ["阿越", "摩顺卡", "蛋蛋"],
            "tags": ["计算机与编程"],
            "time": "10:12-10:41",
            "members": "阿越、摩顺卡、蛋蛋",
            "summary": "阿越先说流水线在 lint 阶段挂了，报错是 eslint 9 与 @typescript-eslint "
            "版本不匹配，一开始以为是缓存没清，重跑两次一样。摩顺卡看了日志说真正的问题是根目录 "
            "package.json 里两个包的主版本号差了一个大版本，并且建议报错要落到具体步骤名上才好定位。"
            "中间 蛋蛋插话说他们项目已经升到 flat config，配置文件要换名字。最后 A 把流水线拆成 "
            "lint / test 两个 job，各自钉版本，跑通了（有配图）。共识是：这类问题先看版本对齐，"
            "再谈缓存。",
            "count": 26,
        },
        {
            "title": "接口 p99 从 52ms 涨到 120ms，锅在缓存 key",
            "names": ["悠嫣咪", "老K"],
            "tags": ["计算机与编程"],
            "time": "11:03-11:58",
            "members": "悠嫣咪、老K",
            "summary": "悠嫣咪贴了监控截图，说 c 环境 p99 从 52ms 涨到 120ms，前端同事已经开始骂了。"
            "两个人先怀疑慢查询，把 SQL 拉出来看没变化；老K注意到命中率掉到 0，翻提交记录发现"
            "缓存 key 少了版本号，发布时新旧 key 不一致，等于每次全穿透。补上版本号后回到 46ms（有配图）。",
            "count": 31,
        },
        {
            "title": "200k 上下文实测：中段召回不如分块",
            "names": ["老K", "小满", "青柠"],
            "tags": ["AI 与模型"],
            "time": "14:20-15:02",
            "members": "老K、小满、青柠",
            "summary": "群里在传某模型新版本支持 200k 上下文。老K拿三份 PDF 做了检索测试，把答案放在"
            "文档中段，发现召回明显变差，越靠中间越容易漏。小满补充说这是长上下文的老问题，"
            "位置编码插值救不了，结论是还不如老老实实分块 + 重排。青柠问那长文档摘要能不能一次丢进去，"
            "被两个人劝退，说成本也不划算。",
            "count": 24,
        },
        {
            "title": "2026 暑期实习开投，后端仍要 Go + MySQL",
            "names": ["青柠", "阿吉"],
            "tags": ["工作与就业"],
            "time": "16:40-17:15",
            "members": "青柠、阿吉",
            "summary": "某厂 2026 暑期实习开始投递，后端岗位要求熟悉 Go 与 MySQL，青柠说简历投出去两天"
            "就有笔试邀请，流程比去年快。阿吉提醒手写 LRU 是高频题，先把双向链表 + 哈希写完再谈优化；"
            "两个人还聊到简历上项目要写清自己负责的那部分，别写成团队总结。",
            "count": 18,
        },
    ],
    "quotes": [
        {
            "content": "这个报错不是我的问题，是流水线的问题。",
            "sender": "阿越",
            "reason": "经典甩锅句式，后面被打脸说 yml 里版本没钉，技术性打脸。",
        },
        {
            "content": "缓存不是玄学，是你把 key 写错了。",
            "sender": "悠嫣咪",
            "reason": "一句话把缓存命中的因果讲清楚，比教程好记。",
        },
    ],
    "footer": FOOTER,
}

# 样张 2：另一群、另一套标签（复古硬件），话题更少但更深
SCENE_LIGHT = {
    "title": "極東電子考古 · 群聊情报",
    "date": "2026-11-06",
    "group": "極東電子考古學結社",
    "stats": {"line": "212 条消息 · 2 个话题 · 最近 24 小时"},
    "headline": "一台 386 主板换完电容还没点亮，顺手聊了 487 主板怎么改。",
    "topics": [
        {
            "title": "386 主板换完电容还是不亮机",
            "names": ["老周", "一只雀斑猪", "31415"],
            "tags": ["老式硬件", "维修改装"],
            "time": "20:11-21:26",
            "members": "老周、一只雀斑猪、31415",
            "summary": "老周翻出一块 386 主板，说换完所有电解电容还是不亮机。一只雀斑猪让他先量 -12V，"
            "结果确实没有负压，怀疑 7912 稳压块坏了；31415 提醒这类板子的电池漏液会腐蚀走线，"
            "要在放大镜下顺着电池座附近看。老周后来补了一张腐蚀点的照片（有配图），"
            "两个人建议先用细铜丝跳线飞过断点再上电，别急着换芯片。",
            "count": 41,
        },
        {
            "title": "487 主板改 DX2-66 的跳线",
            "names": ["31415", "一只雀斑猪"],
            "tags": ["维修改装"],
            "time": "21:40-22:03",
            "members": "31415、一只雀斑猪",
            "summary": "31415 说自己手上有一块 487 主板想上 DX2-66，问跳线怎么设。一只雀斑猪给出倍频"
            "5× 的设置表和电压要求，并提醒这颗 U 的散热片必须贴住；两个人还聊到老主板上的钽电容"
            "极性反了会炸，换的时候别只看丝印。",
            "count": 16,
        },
    ],
    "quotes": [
        {
            "content": "先把负压量出来，没负压你换十个电容也不亮。",
            "sender": "一只雀斑猪",
            "reason": "排查顺序一句话点透，省一整晚时间。",
        },
    ],
    "footer": FOOTER,
}

# 样张 3：这段时间没有命中标签的内容
SCENE_QUIET = {
    "title": "群聊情报",
    "date": "2026-11-09",
    "group": "MeowShelter",
    "stats": {"line": "233 条消息 · 0 个话题 · 最新消息"},
    "headline": "",
    "topics": [],
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
        # 正文里的昵称也要切出来，跟插件一样把它们排成浅一点的色
        payload["topics"] = [
            dict(t, parts=split_names(t.get("summary", ""), t.get("names", [])))
            for t in payload.get("topics", [])
        ]
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

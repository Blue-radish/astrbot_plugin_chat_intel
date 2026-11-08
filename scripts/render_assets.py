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
from imaging import compose_tall_image, plan_tall_file  # noqa: E402  与插件共用同一套长图处理
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

# 群里常见「一张截屏聊到底」的长图：样张里也要有，才能看出它是被切段横排的
SHOT_LONG = """
#set page(width: 420pt, height: auto, margin: 0pt, fill: rgb("#f7f8fa"))
#set text(font: ("Inter", "Noto Sans SC"), size: 11pt, fill: rgb("#334155"))
#let bubbles = (
  ("阿越", lorem(28)),
  ("摩斯卡", lorem(34)),
  ("阿越", lorem(22)),
  ("蛋蛋", lorem(40)),
  ("摩斯卡", lorem(30)),
  ("阿越", lorem(36)),
  ("蛋蛋", lorem(26)),
  ("摩斯卡", lorem(32)),
  ("阿越", lorem(24)),
  ("蛋蛋", lorem(38)),
  ("摩斯卡", lorem(28)),
  ("阿越", lorem(34)),
)
#block(inset: 16pt)[
  #for (who, body) in bubbles [
    #block(width: 100%, above: 9pt)[
      #text(size: 9pt, fill: rgb("#7c8aa0"), who)
      #v(3pt)
      #block(width: 88%, fill: rgb("#ffffff"), radius: 6pt, inset: 9pt, stroke: 0.5pt + rgb("#e2e8f0"))[
        #text(size: 10.5pt, body)
      ]
    ]
  ]
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

# 样张 1：内容较满的一群（多个话题，带标签、时间、参与人、话题配图）
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
            "tags": ["计算机"],
            "time": "10:12-10:41",
            "members": "阿越、摩顺卡、蛋蛋",
            "summary": "阿越 先说流水线在 lint 阶段挂了，报错是 eslint 9 与 @typescript-eslint 版本不匹配，"
            "一开始以为是缓存没清，清了两遍、重跑三次，报错一模一样。摩顺卡 看了眼日志说问题不在缓存，"
            "根目录 package.json 里这两个包的主版本号差了一个大版本，而且强调「报错要落到具体步骤名上，"
            "不然你永远不知道是哪一步在骂你」。中间 蛋蛋 插话说他们项目上个月已经升到 flat config，"
            "配置文件要从 .eslintrc 换成 eslint.config.js，老写法直接不认。最后 阿越 把流水线拆成 lint / "
            "test 两个 job，各自钉版本，跑通了（有配图）。共识是：这类问题先对齐版本，再谈缓存。",
            "count": 26,
        },
        {
            "title": "接口 p99 从 52ms 涨到 120ms，锅在缓存 key",
            "names": ["悠嫣咪", "老K"],
            "tags": ["计算机"],
            "time": "11:03-11:58",
            "members": "悠嫣咪、老K",
            "summary": "悠嫣咪 贴了监控截图，说 c 环境 p99 从 52ms 涨到 120ms，前端同事已经开始骂人了。"
            "两个人先怀疑慢查询，把 SQL 拉出来看执行计划没变化；老K 注意到缓存命中率掉到 0，翻了最近的"
            "提交记录，发现缓存 key 少了版本号：发布时新旧 key 不一致，等于每次请求都穿透到底层。"
            "补上版本号后 p99 回到 46ms（有配图）。",
            "count": 31,
        },
        {
            "title": "200k 上下文实测：中段召回不如分块",
            "names": ["老K", "小满", "青柠"],
            "tags": ["AI"],
            "time": "14:20-15:02",
            "members": "老K、小满、青柠",
            "summary": "群里在传某模型新版本支持 200k 上下文。老K 拿三份 PDF 做了检索测试，把答案故意放在"
            "文档中段，发现召回明显变差，越靠中间越容易漏；小满 补充说这是长上下文的老问题，位置编码插值"
            "救不了，结论是还不如老老实实分块加一轮重排。青柠 问那长文档摘要能不能一次性丢进去，"
            "被两个人劝退，说成本和延迟都不划算。",
            "count": 24,
        },
        {
            "title": "2026 暑期实习开投，后端仍要 Go + MySQL",
            "names": ["青柠", "阿吉"],
            "tags": ["工作与就业"],
            "time": "16:40-17:15",
            "members": "青柠、阿吉",
            "summary": "某厂 2026 暑期实习开始投递，后端岗位要求熟悉 Go 与 MySQL。青柠 说简历投出去两天就"
            "有笔试邀请，流程比去年快；阿吉 提醒手写 LRU 是高频题，先把双向链表加哈希写完再谈优化，"
            "还聊到简历上项目要写清自己负责的那部分，别写成团队总结。",
            "count": 18,
        },
    ],
    "footer": FOOTER,
}

# 样张 2：另一群、另一套标签（复古硬件），话题少而深
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
            "tags": ["硬件"],
            "time": "20:11-21:26",
            "members": "老周、一只雀斑猪、31415",
            "summary": "老周 翻出一块 386 主板，说换完所有电解电容还是不亮机。一只雀斑猪 让他先量 -12V，"
            "结果确实没有负压，怀疑 7912 稳压块坏了；31415 提醒这类板子的电池漏液会腐蚀走线，"
            "要在放大镜下顺着电池座附近看。老周 后来补了一张腐蚀点的照片（有配图），"
            "两个人建议先用细铜丝跳线飞过断点再上电，别急着换芯片。",
            "count": 41,
        },
        {
            "title": "487 主板改 DX2-66 的跳线",
            "names": ["31415", "一只雀斑猪"],
            "tags": ["硬件"],
            "time": "21:40-22:03",
            "members": "31415、一只雀斑猪",
            "summary": "31415 说自己手上有一块 487 主板想上 DX2-66，问跳线怎么设。一只雀斑猪 给出倍频 5× "
            "的设置表和电压要求，并提醒这颗 U 的散热片必须贴住；两个人还聊到老主板上的钽电容极性反了会炸，"
            "换的时候别只看丝印。",
            "count": 16,
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
    "footer": FOOTER,
}

# 每个样张里「第几个话题配哪张占位图」
SCENES = {
    "full": (SCENE_FULL, {0: (SHOT_LONG,), 1: (SHOT_CHART, SHOT_TERMINAL)}),
    "light": (SCENE_LIGHT, {}),
    "quiet": (SCENE_QUIET, {}),
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
        # 正文里的昵称也要切出来，跟插件一样把它们排成浅一点的色
        payload["topics"] = [
            dict(t, parts=split_names(t.get("summary", ""), t.get("names", [])))
            for t in payload.get("topics", [])
        ]
        # 占位图按话题挂上去：跟插件一样，图跟着话题走
        for ti, sources in (shots or {}).items():
            entries = []
            for si, source in enumerate(sources):
                dst = img_dir / f"{ti}_{si}.png"
                _compile_typst_source(source, dst, fonts, 140)
                entry = {
                    "path": f"img/{dst.name}",
                    "caption": f"示例成员{si + 1} · 11-07 1{si + 2}:04 · 截图",
                }
                # 与插件一致：长图铺满整行，离谱高的才分段横排
                plan = plan_tall_file(dst)
                if plan and plan.get("cols", 0) >= 2:
                    composed = img_dir / f"{ti}_{si}_tall.png"
                    if compose_tall_image(dst, composed) == composed:
                        entry["path"] = f"img/{composed.name}"
                        entry["wide"] = True
                elif plan:
                    entry["wide"] = True
                entries.append(entry)
            payload["topics"][ti]["images"] = entries
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

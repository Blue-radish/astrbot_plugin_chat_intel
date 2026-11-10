"""Copy the plugin logo and render README samples offline.

    python scripts/render_assets.py --fonts ./fonts
    python scripts/render_assets.py --fonts ./fonts --scene light
"""

import argparse
import json
import sys
import shutil
import tempfile
from pathlib import Path

import typst

from sync_logo import sync_logo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(ROOT))
from interest import split_names  # noqa: E402  与插件用同一套「群友 ID」切分
from imaging import compose_tall_image, plan_image_file  # noqa: E402  与插件共用同一套图片排版
TEMPLATE = ROOT / "templates" / "report.typ"
LOGO = ROOT / "logo.png"

FOOTER = "生成于 2026-11-10 23:30 · astrbot_plugin_chat_intel · 虚构演示 · 本报告不会发到群里"

# 群里贴的报错截图（样张占位，用 Typst 画，避免往仓库塞真实群图）
SHOT_TERMINAL = """
#set page(width: 720pt, height: auto, margin: 0pt, fill: rgb("#0f172a"))
#set text(font: ("ShareTechMono", "Noto Sans SC"), size: 12pt, fill: rgb("#cbd5e1"))
#block(inset: 18pt)[
  #text(fill: rgb("#67e8f9"), "$ npm run lint")
  #linebreak()
  #text(fill: rgb("#fca5a5"), "ESLint: configuration could not be loaded")
  #linebreak()
  #text(fill: rgb("#fca5a5"), "Multiple ESLint versions found in workspace dependencies.")
  #linebreak()
  #text(fill: rgb("#94a3b8"), "Check the dependency tree and configuration used by this job.")
  #linebreak()
  #text(fill: rgb("#94a3b8"), "Next step: compare package.json and the lockfile.")
  #linebreak()
  #v(8pt)
  #text(fill: rgb("#94a3b8"), "Illustrative output for the README demo")
]
"""

# 群里常见「一张截屏聊到底」的长图：样张里也要有，才能看出它是被切段横排的
SHOT_LONG = """
#set page(width: 420pt, height: auto, margin: 0pt, fill: rgb("#f7f8fa"))
#set text(font: ("Inter", "Noto Sans SC"), size: 11pt, fill: rgb("#334155"))
#let bubbles = (
  ("阿越", "CI 在 lint 阶段失败，清理缓存后仍是同一个错误。先贴出本次日志，后面再补依赖树。"),
  ("小莫", "依赖树里出现两个 ESLint 版本。请对比工作区配置和锁文件，先确认任务实际加载了哪一份。"),
  ("阿越", "已确认子项目的依赖与根目录不一致。接下来单独运行 lint，避免把测试失败混在一起。"),
  ("小满", "建议在同一条提交里更新配置和锁文件，保留失败日志与修改后的版本信息，便于复查。"),
  ("阿越", "正在做最小修改，还没有跑完。当前只能确认依赖不一致，暂时不能说问题已经修好。"),
  ("阿越", "补充结果，统一依赖后 lint 已通过。测试任务仍在排队，这张图只记录 lint 的排查过程。"),
  ("小莫", "把失败时与修复后的依赖树放在一起，能看清到底改了哪一项，也方便后来遇到同样问题的人核对。"),
  ("小满", "建议在记录中区分已经验证的结果和接下来要做的事，lint 通过只对应本次检查。"),
  ("阿越", "已经把版本信息补到排查记录里。这个截图中的日志是此前的失败现场，后面的文字是处理进展。"),
  ("小莫", "如果下一步测试报错，先看测试任务自己的日志。它和之前的 lint 配置错误可能没有直接关系。"),
  ("小满", "建议保留原始错误输出，摘要里只写关键线索，详细日志仍能从对应记录查回去。"),
  ("阿越", "测试仍在等待执行，暂时没有新的结果。等跑完再补充，当前先记录到这里。"),
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
  #text(size: 10pt, fill: rgb("#94a3b8"), "演示数据：基线 38 / 原版 52 / 异常 120 / 修复后 46ms")
]
"""

# 样张 1：内容较满的一群（多个话题，带标签、时间、参与人、话题配图）
SCENE_FULL = {
    "title": "开发茶话会 · 群聊情报",
    "date": "2026-11-10",
    "stats": {"line": "1286 条消息 · 4 个话题 · 今天"},
    "headline": "CI 的 lint 已跑通，接口延迟恢复；长文档测试仍只有小样本结果。",
    "topics": [
        {
            "title": "工作区依赖不一致，lint 排查后通过",
            "names": ["阿越", "小莫", "小满"],
            "tags": ["前端工程"],
            "time": "10:12-10:41",
            "members": "阿越、小莫、小满",
            "summary": "阿越 反馈 CI 的 lint 失败，清缓存后错误仍然存在。小莫 从依赖树发现工作区存在两份 ESLint，"
            "建议核对根目录与子项目实际加载的依赖。小满 建议一起检查配置和锁文件，并把 lint 与测试拆开观察。"
            "阿越 随后确认子项目依赖不一致，统一后反馈 lint 已通过；测试还在排队，尚无结果。"
            "配图为当时的报错与排查记录，不能据此认定整条流水线都已通过。",
            "count": 26,
        },
        {
            "title": "接口 p99 从 52ms 涨到 120ms，锅在缓存 key",
            "names": ["悠嫣咪", "老K"],
            "tags": ["性能排查"],
            "time": "11:03-11:58",
            "members": "悠嫣咪、老K",
            "summary": "悠嫣咪 贴了监控截图，说 c 环境 p99 从 52ms 涨到 120ms，前端同事已经开始骂人了。"
            "两个人先怀疑慢查询，把 SQL 拉出来看执行计划没变化；老K 注意到缓存命中率掉到 0，翻了最近的"
            "提交记录，发现读写路径拼接缓存 key 的顺序不一致，请求无法命中缓存。"
            "统一拼接规则后，悠嫣咪 反馈该环境 p99 回到 46ms，并贴出各版本对比图。",
            "count": 31,
        },
        {
            "title": "200k 上下文小样本漏答，建议补对照",
            "names": ["老K", "小满", "青柠"],
            "tags": ["模型评测"],
            "time": "14:20-15:02",
            "members": "老K、小满、青柠",
            "summary": "群里在传某模型新版本支持 200k 上下文。老K 拿三份 PDF 做了检索测试，把答案故意放在"
            "文档中段，这三份样本上出现了漏答。小满 建议对比分块加重排，青柠 提醒补充不同位置和文档类型的样本。"
            "目前没有模型版本、完整测试集和分块对照结果，只能记录这次观察，不能推成所有长上下文模型的结论。",
            "count": 24,
        },
        {
            "title": "后端实习投递经验，先核对岗位条件",
            "names": ["青柠", "阿吉"],
            "tags": ["工作与就业"],
            "time": "16:40-17:15",
            "members": "青柠、阿吉",
            "summary": "青柠 分享一份后端实习岗位信息，提到岗位要求 Go 与 MySQL，并说简历投出去两天就"
            "有笔试邀请，流程比去年快；阿吉 提醒手写 LRU 是高频题，先把双向链表加哈希写完再谈优化，"
            "还聊到简历上项目要写清自己负责的那部分。这些是两人的投递经验，实际要求仍需核对对应岗位。",
            "count": 18,
        },
    ],
    "footer": FOOTER,
}

# 样张 2：另一群、另一套标签（复古硬件），话题少而深
SCENE_LIGHT = {
    "title": "旧物修复小组 · 群聊情报",
    "date": "2026-11-10",
    "stats": {"line": "212 条消息 · 2 个话题 · 最近 24 小时"},
    "headline": "386 主板仍未点亮，群友提出检查线索；另一块板子的资料正在核对。",
    "topics": [
        {
            "title": "386 主板换完电容还是不亮机",
            "names": ["老周", "一只雀斑猪", "31415"],
            "tags": ["硬件"],
            "time": "20:11-21:26",
            "members": "老周、一只雀斑猪、31415",
            "summary": "老周 反馈一块 386 主板更换电容后仍未点亮。一只雀斑猪 建议先记录供电测量结果，"
            "31415 提醒检查电池漏液附近是否有腐蚀或断线。老周 尚未提供测量数据或后续反馈，"
            "目前只有排查建议，故障原因没有确认。",
            "count": 41,
        },
        {
            "title": "486 主板的跳线表需要核对版本",
            "names": ["31415", "一只雀斑猪"],
            "tags": ["硬件"],
            "time": "21:40-22:03",
            "members": "31415、一只雀斑猪",
            "summary": "31415 想确认一块 486 主板的跳线设置。一只雀斑猪 找到一份手册，但提醒板号与修订版还没对上。"
            "两人约定先补充板号和接口照片，再核对手册。记录里尚无可确认的设置方案。",
            "count": 16,
        },
    ],
    "footer": FOOTER,
}

# 样张 3：这段时间没有命中标签的内容
SCENE_QUIET = {
    "title": "开发茶话会 · 群聊情报",
    "date": "2026-11-10",
    "stats": {"line": "233 条消息 · 0 个话题 · 最新消息"},
    "headline": "",
    "topics": [],
    "footer": FOOTER,
}

SCENE_INCOMPLETE = {
    "title": "旧物修复小组 · 群聊情报",
    "date": "2026-11-10",
    "stats": {"line": "382 条消息 · 1 个话题 · 最近 24 小时"},
    "warn": "⚠ 第 2/3 段生成失败，本报告不完整",
    "headline": "已处理的记录中，386 主板仍在排查，尚未确认故障原因。",
    "topics": [dict(SCENE_LIGHT["topics"][0])],
    "footer": FOOTER,
}

# 每个样张里「第几个话题配哪张占位图」
SCENES = {
    "full": (SCENE_FULL, {0: (SHOT_TERMINAL, SHOT_LONG), 1: (SHOT_CHART,)}),
    "light": (SCENE_LIGHT, {}),
    "quiet": (SCENE_QUIET, {}),
    "incomplete": (SCENE_INCOMPLETE, {}),
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
                _compile_typst_source(source, dst, fonts, 120)
                entry = {
                    "path": f"img/{dst.name}",
                    "caption": f"{payload['topics'][ti]['names'][0]} · 11-10 "
                    f"{payload['topics'][ti]['time'].split('-')[0]} · 演示截图",
                }
                # 与插件一致：按像素比算宽度，太长的切段横排
                plan = plan_image_file(dst)
                entry["w"] = float(plan.get("w", 1.0))
                if plan.get("cols", 1) >= 2:
                    composed = img_dir / f"{ti}_{si}_tall.png"
                    if compose_tall_image(dst, composed) == composed:
                        entry["path"] = f"img/{composed.name}"
                        entry["w"] = 1.0
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


def render_logo(out: Path) -> None:
    """Copy the canonical plugin logo to the documentation output.

    Args:
        out: Destination PNG path.
    """
    sync_logo(LOGO, out.parent)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fonts", default="", help="字体目录")
    parser.add_argument("--ppi", type=int, default=180)
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
        render_logo(out_dir / "logo.png")
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

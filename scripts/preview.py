"""生成一张样张，用来预览排版 / 作为 README 截图。

用法：
    python scripts/preview.py                        # 输出到 ./preview.png，用系统字体
    python scripts/preview.py --fonts ./fonts        # 指定字体目录
    python scripts/preview.py --out docs/screenshot.png
"""

import argparse
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import typst

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "templates" / "report.typ"

SAMPLE = {
    "title": "技术情报日报",
    "date": "2026-11-07",
    "group": "示例群 · 123456789",
    "stats": {"line": "1286 条消息 · 34 人参与 · 采样最近 1 天"},
    "headline": "今天群里主要在修 CI、聊论文 novelty 和远程开发机怎么配。",
    "topics": [
        {
            "title": "CI 报错与持续集成",
            "detail": "A 同学的流水线在 lint 阶段挂掉，直接甩锅给 B 同学；B 反问 cli 和 cli 的区别、"
            "持续集成到底管什么。讨论最后落到「流水线把报错和部署串在一起，报错要定位到具体步骤」，"
            "没人真的去改 yml。",
            "people": "A同学、B同学",
        },
        {
            "title": "论文审稿与 novelty",
            "detail": "C 同学论文二审被审稿人指出 novelty 不足，担心被拒。D 同学给出「逐条 rebuttal + "
            "补一个消融实验」的方案，并提醒会议公开评审机制下审稿意见是公开的，别硬刚。",
            "people": "C同学、D同学",
        },
        {
            "title": "远程开发机方案",
            "detail": "E 同学提出用远程桌面 + 内网穿透的方案，把公司电脑当开发机用；"
            "有人补充 Tailscale 与 frp 的取舍，最后共识是先用 Tailscale，延迟和配置成本更低。",
            "people": "E同学、F同学",
        },
    ],
    "tools": [
        {"name": "Typst", "url": "https://typst.app", "note": "现代排版系统，编译快，适合生成报表长图"},
        {"name": "Tailscale", "url": "https://tailscale.com", "note": "基于 WireGuard 的组网，穿透内网不用配端口映射"},
        {"name": "astrbot_plugin_tech_digest", "url": "https://github.com/", "note": "本插件：把群聊压成一份技术简报"},
    ],
    "jobs": [
        {"text": "某厂 2026 暑期实习开始投递，后端岗位要求熟悉 Go 与 MySQL", "who": "G同学"},
        {"text": "面试反馈：手写 LRU 是高频题，建议先把双向链表 + 哈希写完再谈优化", "who": "H同学"},
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
    "footer": f"生成于 {datetime.now():%Y-%m-%d %H:%M} · astrbot_plugin_tech_digest · 本报告不会发到群里",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="preview.png")
    parser.add_argument("--fonts", default="")
    parser.add_argument("--ppi", type=int, default=180)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # 模板把数据读成同目录的 data.json，所以复制到临时任务目录里编译
    job = Path(tempfile.mkdtemp(prefix="tech_digest_"))
    shutil.copyfile(TEMPLATE, job / "main.typ")
    (job / "data.json").write_text(
        json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8"
    )

    kwargs = {}
    if args.fonts:
        kwargs["font_paths"] = [args.fonts]
    typst.compile(
        str(job / "main.typ"),
        output=str(out),
        root=str(job),
        format="png",
        ppi=args.ppi,
        **kwargs,
    )
    shutil.rmtree(job, ignore_errors=True)
    print(f"written: {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

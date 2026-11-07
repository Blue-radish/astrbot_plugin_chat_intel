"""长图处理：把「一张截屏聊到底」的超长竖图切成几段、横排拼回一张。

单独成文件是为了让出图脚本（scripts/render_assets.py）也能复用同一套实现，
不依赖 AstrBot 的运行时（main.py 需要 astrbot 才能导入）。
"""

from __future__ import annotations

import math
from pathlib import Path

TALL_RATIO = 2.2      # 高宽比超过这个就算长图：直接缩到 200pt 高只剩几十 pt 宽
TALL_MAX_SLICES = 4   # 最多切几段（再多每段就窄得看不清了）
TARGET_ASPECT = 1.7   # 切完希望长这样「横一条」，报告里不会一屏塞满


def compose_tall_image(src: Path, dst: Path) -> Path:
    """超长竖图切段横排；不需要切或任何一步失败都原样返回 src。"""
    try:
        from PIL import Image
    except ImportError:
        return src
    try:
        with Image.open(src) as im:
            width, height = im.size
            if not width or not height:
                return src
            ratio = height / width
            if ratio <= TALL_RATIO:
                return src
            # 切 k 段并排后，整张的高宽比会变成 k²/ratio；
            # 解 k²/ratio ≈ TARGET_ASPECT 得到段数，同时保证每段别太窄
            slices = math.ceil(math.sqrt(TARGET_ASPECT * ratio))
            slices = max(2, min(TALL_MAX_SLICES, slices))
            step = math.ceil(height / slices)
            panels = []
            for i in range(slices):
                top = i * step
                if top >= height:
                    break
                panels.append(
                    im.crop((0, top, width, min(height, top + step))).convert("RGB")
                )
            if len(panels) < 2:
                return src
            gutter = 6
            canvas_w = sum(p.width for p in panels) + gutter * (len(panels) - 1)
            canvas_h = max(p.height for p in panels)
            canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
            x = 0
            for panel in panels:
                canvas.paste(panel, (x, 0))
                x += panel.width + gutter
            canvas.save(dst, format="PNG", optimize=True)
            return dst
    except Exception:
        return src

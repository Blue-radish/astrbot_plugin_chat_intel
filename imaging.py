"""长图处理：文字截图铺满一行，超高的才横向分段（切在空白带上）。

单独成文件是为了让出图脚本（scripts/render_assets.py）也能复用同一套实现，
不依赖 AstrBot 的运行时（main.py 需要 astrbot 才能导入）。

取舍：报告最终是给人用手机看的，一列放的东西越少，字缩得越少。所以
- 高宽比 ≤ TALL_RATIO：当普通图，按高度限制（旧行为）
- 超过 TALL_RATIO：**铺满整行、不再压缩**，宁可让报告长一点
- 超过 STACK_RATIO（真的离谱，比如整页网页长截图）：才切成 2-3 段横排，
  切点尽量落在横向空白带上；找不到空白就上下各留一点重叠，免得一行字被劈成两半
"""

from __future__ import annotations

import math
from pathlib import Path

TALL_RATIO = 2.2  # 高宽比超过这个就不按高度压缩，改成铺满整行
STACK_RATIO = 12.0  # 超过这个实在长得没法看，才横向分段（2 列，再高 3 列）
SLICE_MAX = 3  # 最多分几段（再多每段就窄得看不清了）
GUTTER = 6  # 分段之间的白缝
OVERLAP = 24  # 切点找不到空白时上下重叠的像素（大约一行字）
BLANK_THRESHOLD = 235  # 每行最暗的像素亮于这个值就算「空白行」


def plan_tall(width: int, height: int) -> dict | None:
    """长图怎么排：返回 {"ratio":…, "cols": 1 或 n}；不需要特殊处理返回 None。"""
    if not width or not height or width <= 0 or height <= 0:
        return None
    ratio = height / width
    if ratio <= TALL_RATIO:
        return None
    if ratio <= STACK_RATIO:
        cols = 1
    elif ratio <= STACK_RATIO * 3:
        cols = 2
    else:
        cols = SLICE_MAX
    return {"ratio": ratio, "cols": cols}


def blank_rows(im, sample_width: int = 16) -> list[bool]:
    """逐行判断是不是空白：把图缩到很窄再逐行看最暗值，比逐像素扫快得多。"""
    grey = im.convert("L")
    width, height = grey.size
    if not width or not height:
        return []
    width_sample = max(1, min(sample_width, width))
    small = grey.resize((width_sample, height))
    data = list(small.getdata())
    return [
        min(data[y * width_sample : (y + 1) * width_sample]) >= BLANK_THRESHOLD
        for y in range(height)
    ]


def _find_blank(blank: list[bool], target: int, window: int, height: int) -> int | None:
    """在 target 附近找一行空白，越近越好；找不到返回 None。"""
    if not blank:
        return None
    for delta in range(0, max(0, window) + 1):
        for pos in (target - delta, target + delta):
            if 0 < pos < height and pos < len(blank) and blank[pos]:
                return pos
    return None


def cut_positions(
    height: int, cols: int, blank: list[bool] | None = None
) -> list[tuple[int, int | None]]:
    """算出每个切点：(理想位置, 实际找到的空白行或 None)。"""
    if cols <= 1 or height <= 0:
        return []
    step = height / cols
    window = max(1, int(step * 0.06))
    out: list[tuple[int, int | None]] = []
    for i in range(1, cols):
        target = int(round(i * step))
        found = _find_blank(blank or [], target, window, height)
        out.append((target, found))
    return out


def panel_bounds(
    height: int, cuts: list[tuple[int, int | None]], overlap: int = OVERLAP
) -> list[tuple[int, int]]:
    """把切点变成每段的上下边界；切在空白上就干净切开，切不开就两边各留一点重叠。"""
    points: list[tuple[int, bool]] = [(0, True)]
    points += [
        (found if found is not None else target, found is not None) for target, found in cuts
    ]
    points.append((height, True))
    bounds: list[tuple[int, int]] = []
    for i in range(len(points) - 1):
        top, top_blank = points[i]
        bottom, bottom_blank = points[i + 1]
        start = top if i == 0 or top_blank else max(0, top - overlap)
        end = bottom if i == len(points) - 2 or bottom_blank else min(height, bottom + overlap)
        if end > start:
            bounds.append((start, end))
    return bounds


def plan_tall_file(src: Path) -> dict | None:
    """按文件算排版方案；打不开就当普通图。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(src) as im:
            return plan_tall(*im.size)
    except Exception:
        return None


def compose_tall_image(src: Path, dst: Path) -> Path | None:
    """超高的图分段横排到 dst；不需要分段、或任何一步失败都返回 None（调用方用原图）。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(src) as im:
            width, height = im.size
            info = plan_tall(width, height)
            if not info or info["cols"] < 2:
                return None
            cuts = cut_positions(height, info["cols"], blank_rows(im))
            bounds = panel_bounds(height, cuts)
            panels = [
                im.crop((0, top, width, bottom)).convert("RGB")
                for top, bottom in bounds
                if bottom > top
            ]
            if len(panels) < 2:
                return None
            canvas_w = sum(p.width for p in panels) + GUTTER * (len(panels) - 1)
            canvas_h = max(p.height for p in panels)
            canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
            x = 0
            for panel in panels:
                canvas.paste(panel, (x, 0))
                x += panel.width + GUTTER
            canvas.save(dst, format="PNG", optimize=True)
            return dst
    except Exception:
        return None

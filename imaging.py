"""图在报告里占多大：先保证字看得清，再保证不占太多地方。

单独成文件是为了让出图脚本（scripts/render_assets.py）也能复用同一套实现，
不依赖 AstrBot 的运行时（main.py 需要 astrbot 才能导入）。

以前是「按固定高度摆」（并排 96pt、单图 200pt、比值 >2.2 铺满整行且不封顶），
结果两头都歪：1200 宽的截图只分到 180px（0.15 倍，糊），851×1920 的手机截图
铺满整行要 1480pt（占整份报告 18%）。

现在按像素比算：
- 版心 656pt、出图 180ppi → 1pt = 2.5px。想看清字，图上文字至少要按原像素的
  TARGET_SCALE(0.9) 显示，于是宽度 ≈ 图片像素宽 / 2.5（不超过版心）
- 单张这么摆会超过 MAX_TALL_PT(420pt) 时，切成 2-3 段横排：宽度翻倍、高度按列数
  平方下降，**每段的字仍是原尺寸**，面积却小得多
- 切点尽量落在横向空白带上；找不到空白就上下各留一点重叠，免得一行字被劈成两半
- 横图不切（切了每段只剩一点宽），整体缩到像素比够用为止
"""

from __future__ import annotations

import math
from pathlib import Path

CONTENT_PT = 656.0  # 版心宽度（页宽 700pt 减去左右各 22pt 边距）
PX_PER_PT = 2.5  # 出图 180ppi：1pt = 2.5px
TARGET_SCALE = 0.9  # 图上的字按原像素的这个比例显示就算看清了
MIN_SCALE = 0.55  # 再缩下去就没意义了（保留给将来的判断用）
MAX_TALL_PT = 420.0  # 任何一张图允许占的最大高度
SLICE_MAX = 3  # 最多分几段（再多每段就窄得看不清了）
GUTTER = 6  # 分段之间的白缝
OVERLAP = 24  # 切点找不到空白时上下重叠的像素（大约一行字）
BLANK_THRESHOLD = 235  # 每行最暗的像素亮于这个值就算「空白行」


def plan_image(width: int, height: int) -> dict:
    """这张图怎么摆：{"ratio", "cols", "w"}。

    w 是占版心宽度的比例（1.0 = 铺满整行）；cols≥2 表示先切成几段横排再铺满。
    尺寸不合法时退回「铺满整行」，调用方不用判 None。
    """
    if not width or not height or width <= 0 or height <= 0:
        return {"ratio": 0.0, "cols": 1, "w": 1.0}
    ratio = height / width
    # 想看清：显示宽度按原像素折算，且不超过版心；同时不许超过高度预算
    legible = min(1.0, TARGET_SCALE * width / PX_PER_PT / CONTENT_PT)
    budget = MAX_TALL_PT / ratio / CONTENT_PT
    if ratio <= 1.0:
        # 横图/方图：切段只会把每段切窄，整体缩到像素比够用为止
        return {"ratio": ratio, "cols": 1, "w": round(min(legible, budget), 4)}
    if legible * CONTENT_PT * ratio <= MAX_TALL_PT:
        return {"ratio": ratio, "cols": 1, "w": round(legible, 4)}
    # 竖图太长：横排切段，宽度翻倍、高度按列数平方下降
    for cols in range(2, SLICE_MAX + 1):
        if CONTENT_PT * ratio / (cols * cols) <= MAX_TALL_PT:
            return {"ratio": ratio, "cols": cols, "w": 1.0}
    return {"ratio": ratio, "cols": SLICE_MAX, "w": 1.0}


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


def plan_image_file(src: Path) -> dict:
    """按文件算排版方案；打不开就当普通图（铺满整行）。"""
    try:
        from PIL import Image
    except ImportError:
        return {"ratio": 0.0, "cols": 1, "w": 1.0}
    try:
        with Image.open(src) as im:
            return plan_image(*im.size)
    except Exception:
        return {"ratio": 0.0, "cols": 1, "w": 1.0}


def compose_tall_image(src: Path, dst: Path) -> Path | None:
    """超高的图分段横排到 dst；不需要分段、或任何一步失败都返回 None（调用方用原图）。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(src) as im:
            width, height = im.size
            info = plan_image(width, height)
            if info["cols"] < 2:
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

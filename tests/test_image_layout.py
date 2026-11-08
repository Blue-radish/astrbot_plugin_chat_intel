"""图在报告里占多大：按像素比定宽，太高就切段，任何一张都不超过允许高度。

以前是「按固定高度摆」：并排小图 96pt、单图 200pt、比值 >2.2 的长图铺满整行不封顶。
结果两头都歪——1200×900 的截图只分到 180px（0.15 倍，糊成一团），而 851×1920 的手机
截图铺满整行要 1480pt（占整份报告 18%）。
"""

import imaging

CONTENT = imaging.CONTENT_PT


def height_pt(width_px: int, height_px: int) -> float:
    """按排版方案算这张图在报告里占多高（pt）。"""
    plan = imaging.plan_image(width_px, height_px)
    ratio = height_px / width_px
    if plan["cols"] >= 2:
        return CONTENT * ratio / (plan["cols"] ** 2)
    return CONTENT * plan["w"] * ratio


def width_pt(width_px: int, height_px: int) -> float:
    plan = imaging.plan_image(width_px, height_px)
    return CONTENT * plan["w"]


def scale_of(width_px: int, height_px: int) -> float:
    """图上文字相对原图的像素比：1.0 = 原尺寸，0.3 = 缩到三成（基本看不清）。"""
    plan = imaging.plan_image(width_px, height_px)
    shown_px = CONTENT * imaging.PX_PER_PT * plan["w"] / max(1, plan["cols"])
    return shown_px / width_px


# ---------------------------------------------------------------- 太小的一头
def test_landscape_screenshot_is_big_enough_to_read():
    """1200×900 的截图：以前 200pt 高（0.31 倍），现在按像素比给宽度。"""
    assert scale_of(1200, 900) >= 0.85, "还得缩到 0.85 以上才看得清"
    assert height_pt(1200, 900) <= imaging.MAX_TALL_PT


def test_portrait_screenshot_is_sliced_not_shrunk():
    """1200×1600：单张按像素比要 575pt 高，超过预算 → 切两段，清晰度反而更好。"""
    plan = imaging.plan_image(1200, 1600)
    assert plan["cols"] == 2
    assert scale_of(1200, 1600) >= 0.6
    assert height_pt(1200, 1600) <= imaging.MAX_TALL_PT


def test_tiny_meme_is_not_blown_up():
    """300×200 的表情包不该被拉到整版心。"""
    assert width_pt(300, 200) <= 300 / imaging.PX_PER_PT * imaging.TARGET_SCALE + 1
    assert imaging.plan_image(300, 200)["cols"] == 1


# ---------------------------------------------------------------- 太大的一头
def test_phone_screenshot_is_stacked():
    """851×1920（比值 2.26）：铺满整行要 1480pt，现在切两段 371pt，像素比 0.96。"""
    plan = imaging.plan_image(851, 1920)
    assert plan["cols"] == 2 and plan["w"] == 1.0
    assert height_pt(851, 1920) <= imaging.MAX_TALL_PT
    assert scale_of(851, 1920) >= 0.85, "切两段不该把字切小"


def test_huge_landscape_is_not_sliced():
    """4000×3000 这种超宽图切段没意义（每段只有 0.14 倍），只能整体缩。"""
    plan = imaging.plan_image(4000, 3000)
    assert plan["cols"] == 1
    assert height_pt(4000, 3000) <= imaging.MAX_TALL_PT + 1


def test_height_budget_holds_for_ordinary_shapes():
    shapes = [
        (851, 1920),  # 手机截图
        (1200, 1600),  # 竖版截图
        (1200, 900),  # 横版截图
        (800, 644),  # 推文截图
        (1200, 552),  # 柱状图
        (1080, 1515),  # 手机截图
        (1200, 1936),  # 竖屏长截图
        (1120, 1815),  # 竖屏长截图
        (1000, 1000),  # 方图
        (4000, 3000),  # 超宽图
        (300, 200),  # 表情包
    ]
    for width_px, height_px in shapes:
        assert height_pt(width_px, height_px) <= imaging.MAX_TALL_PT + 1, (
            width_px,
            height_px,
            imaging.plan_image(width_px, height_px),
        )


def test_extreme_long_image_uses_max_columns():
    """真的长到没法看（比如整页网页），切到上限就不再切了。"""
    plan = imaging.plan_image(400, 20000)
    assert plan["cols"] == imaging.SLICE_MAX


def test_broken_dimensions_fall_back_to_full_width():
    plan = imaging.plan_image(0, 100)
    assert plan["cols"] == 1 and plan["w"] == 1.0

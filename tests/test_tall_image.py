"""长图处理：文字长图铺满整行，离谱高的才分段横排（切在空白带上）。"""

from pathlib import Path

import pytest
from PIL import Image

import imaging
from conftest import load_main


def _flat(path: Path, width: int, height: int, colour=(250, 250, 252)) -> Path:
    Image.new("RGB", (width, height), colour).save(path)
    return path


def _texty(path: Path, width: int = 400, height: int = 4000, line: int = 18) -> Path:
    """造一张「有文字行」的长图：每隔一行画一条深色文字带，中间留白，模拟截屏。"""
    from PIL import ImageDraw

    im = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(im)
    y = 10
    while y + line < height - 10:
        draw.rectangle([8, y, width - 8, y + line], fill=(20, 20, 20))
        y += line * 2
    im.save(path)
    return path


# ---------------------------------------------------------------- 排版方案
def test_plan_short_image_is_normal():
    assert imaging.plan_tall(800, 600) is None
    assert imaging.plan_tall(800, 1500) is None  # 比例 1.875 < 2.2


def test_plan_tall_image_takes_full_row():
    plan = imaging.plan_tall(400, 2400)  # 比例 6
    assert plan and plan["cols"] == 1, "能看清的长图应该独占整行，不该被切成几列"


def test_plan_absurdly_tall_image_is_stacked():
    assert imaging.plan_tall(400, 4000)["cols"] == 1, "比例 10 还是铺满一行更清楚"
    assert imaging.plan_tall(400, 6000)["cols"] == 2, "比例 15 才分段"
    assert imaging.plan_tall(400, 20000)["cols"] == imaging.SLICE_MAX


def test_plan_caps_column_count():
    assert imaging.plan_tall(400, 100000)["cols"] == imaging.SLICE_MAX


def test_broken_file_is_normal(tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not really an image")
    assert imaging.plan_tall_file(bad) is None


# ---------------------------------------------------------------- 切点
def test_blank_rows_recognises_white_gaps():
    im = Image.new("RGB", (40, 60), (255, 255, 255))
    for x in range(40):
        for y in range(20, 30):
            im.putpixel((x, y), (0, 0, 0))
    blank = imaging.blank_rows(im)
    assert blank[0] is True and blank[25] is False


def test_cuts_snap_to_blank_rows():
    height = 1000
    blank = [False] * height
    blank[500] = True
    blank[503] = True
    cuts = imaging.cut_positions(height, 2, blank)
    assert cuts[0][1] in (500, 503)


def test_panels_overlap_when_no_blank_row_found():
    height = 1000
    cuts = imaging.cut_positions(height, 2, [False] * height)
    bounds = imaging.panel_bounds(height, cuts)
    assert len(bounds) == 2
    assert bounds[0][1] > bounds[1][0], "找不到空白就上下重叠，别把一行字劈两半"
    assert bounds[0][1] - bounds[1][0] >= imaging.OVERLAP


def test_panels_split_cleanly_on_blank_row():
    height = 1000
    blank = [False] * height
    blank[500] = True
    bounds = imaging.panel_bounds(height, imaging.cut_positions(height, 2, blank))
    assert bounds == [(0, 500), (500, 1000)]


# ---------------------------------------------------------------- 合成
def test_compose_skips_image_that_fits_full_row(tmp_path):
    src = _flat(tmp_path / "tall.jpg", 400, 2400)
    dst = tmp_path / "out.png"
    assert imaging.compose_tall_image(src, dst) is None, "独占整行的长图不需要合成"
    assert not dst.exists()


def test_compose_stacks_absurdly_tall_image(tmp_path):
    src = _flat(tmp_path / "huge.jpg", 400, 6000)
    dst = tmp_path / "out.png"
    assert imaging.compose_tall_image(src, dst) == dst and dst.is_file()
    with Image.open(dst) as im:
        assert im.size[0] > 400, "分段后是并排的几列，应该比原图宽"
        assert im.size[1] < 6000 / 1.5, "分段后高度应该明显降下来"


def test_compose_never_splits_text_lines(tmp_path):
    """每段切口都必须落在空白带上：有文字的行不能被切到两段里。"""
    src = _texty(tmp_path / "text.png", 400, 6000)
    dst = tmp_path / "out.png"
    assert imaging.compose_tall_image(src, dst) == dst
    with Image.open(src) as im:
        blank = imaging.blank_rows(im)
        cuts = imaging.cut_positions(im.size[1], imaging.plan_tall(*im.size)["cols"], blank)
    bounds = imaging.panel_bounds(im.size[1], cuts)
    for (top_a, bottom_a), (top_b, _bottom_b) in zip(bounds, bounds[1:]):
        seam = bottom_a if bottom_a <= top_b else top_b
        assert blank[seam] or bottom_a > top_b, f"切口 {seam} 落在文字行上"


def test_broken_image_composes_to_none(tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"nope")
    assert imaging.compose_tall_image(bad, tmp_path / "out.png") is None


# ---------------------------------------------------------------- 出图集成
def _render_plugin(mod, tmp_path):
    p = object.__new__(mod.TechDigestPlugin)
    p.reports_dir = tmp_path / "reports"
    p.reports_dir.mkdir()
    p.font_dir = Path(r"C:\Users\Ray\astrbot\data\plugin_data\astrbot_plugin_chat_intel\fonts")
    p.template = Path(mod.__file__).parent / "templates" / "report.typ"
    p.ppi = 72
    return p


def _payload(tall_dir: Path, src: Path):
    return {
        "title": "群聊情报",
        "date": "2026-11-07",
        "group": "测试群",
        "headline": "一句话",
        "stats": {"line": "3 条消息 · 1 个话题"},
        "topics": [
            {
                "title": "话题",
                "tags": ["计算机"],
                "time": "10:00",
                "members": "2 人",
                "parts": [{"text": "讲了点东西", "name": False}],
                "images": [{"path": str(src), "caption": "示例 · 截图"}],
            }
        ],
    }


def test_render_marks_tall_image_wide(tmp_path):
    """文字长图在报告里要独占一行（wide），否则挤在两列里字只有一半大小。"""
    mod = load_main()
    p = _render_plugin(mod, tmp_path)
    tall = _flat(tmp_path / "tall.jpg", 300, 1800)
    payload = _payload(tmp_path, tall)
    out = p._render(payload, "1", "今天")
    assert out is not None and out.is_file()
    entry = payload["topics"][0]["images"][0]
    assert entry.get("wide") is True
    with Image.open(out) as im:
        assert im.size[1] > 100


def test_render_composes_absurdly_tall_image(tmp_path):
    mod = load_main()
    p = _render_plugin(mod, tmp_path)
    huge = _flat(tmp_path / "huge.jpg", 400, 6000)
    payload = _payload(tmp_path, huge)
    out = p._render(payload, "1", "今天")
    assert out is not None and out.is_file()
    entry = payload["topics"][0]["images"][0]
    assert entry.get("wide") is True
    assert entry["path"].endswith(".png"), "分段横排过的长图应该指向合成图"

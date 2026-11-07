"""长图（超长竖图）切段横排的回归测试。"""

from pathlib import Path

import pytest
from PIL import Image

from conftest import load_main


def _make_image(path: Path, width: int, height: int) -> Path:
    Image.new("RGB", (width, height), (250, 250, 252)).save(path)
    return path


def test_tall_image_is_sliced_into_columns(tmp_path):
    mod = load_main()
    src = _make_image(tmp_path / "tall.jpg", 400, 2400)  # 高宽比 6
    dst = tmp_path / "out.png"
    out = mod.TechDigestPlugin.compose_tall_image(src, dst)
    assert out == dst and dst.is_file()
    with Image.open(dst) as im:
        width, height = im.size
    assert width > height, "切段横排后应该是横的一条，不是竖的一条"
    assert height <= 2400 / 2, "至少切成两段"


def test_normal_image_is_left_untouched(tmp_path):
    mod = load_main()
    src = _make_image(tmp_path / "square.png", 800, 600)
    dst = tmp_path / "out.png"
    assert mod.TechDigestPlugin.compose_tall_image(src, dst) == src
    assert not dst.exists()


def test_slightly_tall_image_still_untouched(tmp_path):
    mod = load_main()
    src = _make_image(tmp_path / "mild.png", 800, 1500)  # 比例 1.875 < 2.2
    assert mod.TechDigestPlugin.compose_tall_image(src, tmp_path / "out.png") == src


def test_broken_image_falls_back_to_original(tmp_path):
    mod = load_main()
    src = tmp_path / "broken.jpg"
    src.write_bytes(b"not really an image")
    assert mod.TechDigestPlugin.compose_tall_image(src, tmp_path / "out.png") == src


def test_render_slices_tall_topic_image(tmp_path):
    """出图时话题卡里的长图会被替换成切段后的那张。"""
    mod = load_main()
    p = object.__new__(mod.TechDigestPlugin)
    p.reports_dir = tmp_path / "reports"
    p.reports_dir.mkdir()
    p.font_dir = Path(
        r"C:\Users\Ray\astrbot\data\plugin_data\astrbot_plugin_chat_intel\fonts"
    )
    p.template = Path(mod.__file__).parent / "templates" / "report.typ"
    p.ppi = 72

    tall = _make_image(tmp_path / "tall.jpg", 300, 1800)
    payload = {
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
                "images": [{"path": str(tall), "caption": "示例 · 截图"}],
            }
        ],
    }
    out = p._render(payload, "1", "今天")
    assert out is not None and out.is_file()
    with Image.open(out) as im:
        assert im.size[1] > 100

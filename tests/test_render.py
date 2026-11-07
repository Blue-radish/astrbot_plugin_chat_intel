import pathlib

from PIL import Image

from conftest import PLUGIN_DIR, make_plugin

FONTS = pathlib.Path(r"C:\Users\Ray\astrbot\data\plugin_data\astrbot_plugin_chat_intel\fonts")


def _plugin(tmp_path):
    plugin, _mod = make_plugin(
        reports_dir=tmp_path,
        ppi=72,
        report_title="群聊情报",
        keep_quotes=True,
        template=PLUGIN_DIR / "templates" / "report.typ",
        font_dir=str(FONTS),
    )
    return plugin


def _base():
    return {
        "title": "群聊情报",
        "date": "2026-11-07",
        "group": "测试群",
        "stats": {"line": "10 条消息 · 采纳 2 条 · 上限截断 0 条 · 今天"},
        "headline": "",
        "quotes": [],
        "footer": "t",
    }


def _sections(count):
    return [
        (f"主题{i}", [{"topic": f"事{i}", "detail": "细节" * 20, "kind": "讨论"}])
        for i in range(count)
    ]


def test_render_handles_zero_sections(tmp_path):
    p = _plugin(tmp_path)
    path = p._render(dict(_base(), sections=[]), [], "1", "今天")
    assert path is not None and path.exists()


def test_render_grows_with_sections(tmp_path):
    """分节是真的被渲染了：五节比零节明显更高。"""
    p = _plugin(tmp_path)
    empty = p._render(dict(_base(), sections=[]), [], "1", "空")
    many = p._render(dict(_base(), sections=_sections(5)), [], "1", "多")
    assert empty is not None and many is not None
    with Image.open(empty) as low, Image.open(many) as high:
        assert high.height > low.height + 100, (high.height, low.height)


def test_report_filename_uses_plugin_name(tmp_path):
    p = _plugin(tmp_path)
    path = p._render(dict(_base(), sections=_sections(1)), [], "1", "今天")
    assert path is not None
    assert path.name.startswith("chat_intel_")

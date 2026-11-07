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
        "stats": {"line": "10 条消息 · 2 个话题 · 今天"},
        "headline": "",
        "quotes": [],
        "footer": "t",
    }


def _topics(count):
    return [
        {
            "title": f"话题{i}",
            "tags": ["电脑"],
            "time": "16:23-16:34",
            "members": "甲、乙",
            "summary": "这是一段完整的讲述。" * 12,
            "count": 5,
        }
        for i in range(count)
    ]


def test_render_handles_zero_topics(tmp_path):
    p = _plugin(tmp_path)
    path = p._render(dict(_base(), topics=[]), [], "1", "今天")
    assert path is not None and path.exists()


def test_render_grows_with_topics(tmp_path):
    """话题卡是真的被渲染了：五个话题比零个话题明显更高。"""
    p = _plugin(tmp_path)
    empty = p._render(dict(_base(), topics=[]), [], "1", "空")
    many = p._render(dict(_base(), topics=_topics(5)), [], "1", "多")
    assert empty is not None and many is not None
    with Image.open(empty) as low, Image.open(many) as high:
        assert high.height > low.height + 100, (high.height, low.height)


def test_render_shows_tags_and_members(tmp_path):
    """话题卡里要有标签、时间、参与人——这些是报告的核心信息。"""
    p = _plugin(tmp_path)
    path = p._render(dict(_base(), topics=_topics(1)), [], "1", "今天")
    assert path is not None
    assert path.name.startswith("chat_intel_")

def test_payload_topics_keeps_tags_as_list():
    """模板按数组渲染标签：这里必须给数组，拼成字符串会渲染成一字一个标签。"""
    p, _ = make_plugin()
    out = p._payload_topics(
        [
            {
                "title": "本地跑大模型",
                "tags": ["电脑", "本地部署"],
                "time": "16:23-16:34",
                "members": "甲、乙",
                "summary": "完整讲述。",
                "count": 3,
            }
        ]
    )
    assert isinstance(out[0]["tags"], list)
    assert out[0]["tags"] == ["电脑", "本地部署"]
    assert out[0]["count"] == 3


def test_payload_topics_skips_rows_without_title():
    p, _ = make_plugin()
    assert p._payload_topics([{"title": "  ", "tags": ["x"]}]) == []
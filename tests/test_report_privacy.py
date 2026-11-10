"""报告图会被人转发出去，所以图里不许出现群号。

群名可以留（用户自己配的备注，就是给报告当标题用的），群号不行：
转发一张图等于把「哪个群」直接交出去。
"""

import json

import pytest
from conftest import PLUGIN_DIR, make_plugin

GID = "220001"


def test_template_does_not_print_group_number():
    """模板里不该再有任何群号位（以前是标题下面那行「群号 220001」）。"""
    text = (PLUGIN_DIR / "templates" / "report.typ").read_text(encoding="utf-8")
    assert "群号" not in text, "模板里还印着群号"
    assert 'd.at("group"' not in text, "模板还在读 group 字段"


def test_scrub_removes_gid_anywhere_in_the_payload():
    """不管谁把群号塞进哪个字段，出图前都要被抹掉。"""
    from conftest import load_main

    mod = load_main()
    payload = {
        "title": f"测试群 · 群聊情报",
        "date": "2026-11-10",
        "group": f"群号 {GID}",
        "stats": {"line": f"在 {GID} 里采到 382 条"},
        "headline": f"{GID} 今天很安静",
        "footer": f"生成于 2026-11-10 23:30 · {GID}",
        "topics": [{"title": f"来自 {GID} 的话题", "parts": [f"群友说 {GID} 好卡"]}],
    }
    out = mod._scrub_gid(payload, GID)
    dumped = json.dumps(out, ensure_ascii=False)
    assert GID not in dumped, dumped


def test_scrub_keeps_the_rest_of_the_content():
    """只抹群号，不许把正文弄坏。"""
    from conftest import load_main

    mod = load_main()
    payload = {
        "title": "测试群 · 群聊情报",
        "stats": {"line": "382 条消息 · 1 个话题 · 最近 24 小时"},
        "topics": [{"title": "386 主板换完电容还是不亮机", "parts": ["老周 反馈…"]}],
    }
    out = mod._scrub_gid(payload, GID)
    assert out["title"] == payload["title"]
    assert out["stats"]["line"] == payload["stats"]["line"]
    assert out["topics"][0]["title"] == payload["topics"][0]["title"]


def test_scrub_does_not_touch_other_numbers():
    """别把 220001 当子串去误伤正常数字（比如消息条数）。"""
    from conftest import load_main

    mod = load_main()
    payload = {"stats": {"line": "1220001 条消息"}, "headline": "2200010 次点击"}
    out = mod._scrub_gid(payload, GID)
    dumped = json.dumps(out, ensure_ascii=False)
    assert GID not in dumped
    # 这两个数字里含群号子串，但不该被整段吃掉到只剩空白
    assert "条消息" in dumped and "次点击" in dumped


def test_scrub_leaves_file_paths_alone():
    """图片路径不进正文，抹了只会把图弄坏。

    群号短的时候（测试里就是「1」），路径里随手一个数字就命中，于是
    `Path(被改坏的路径).is_file()` 为假，图片被静默丢弃——报告少图，
    但没有任何报错。
    """
    from conftest import load_main

    mod = load_main()
    payload = {
        "topics": [
            {
                "title": "话题 1",
                "images": [
                    {"path": r"C:\tmp\shot_1.jpg", "caption": "群 1 的截图", "w": 1.0}
                ],
            }
        ]
    }
    out = mod._scrub_gid(payload, "1")
    entry = out["topics"][0]["images"][0]
    assert entry["path"] == r"C:\tmp\shot_1.jpg", "文件路径不能改"
    assert "1" not in entry["caption"], "给人看的文案照旧要抹"
    assert "1" not in out["topics"][0]["title"]


def test_render_keeps_images_even_when_gid_looks_like_path_digits(tmp_path, monkeypatch):
    """回归：gid 是「1」这种短串时，路径里的 1 曾把图片变成"不存在的文件"。"""
    pytest.importorskip("typst")
    from PIL import Image as PILImage

    from conftest import load_main

    mod = load_main()
    p, _ = make_plugin()
    p.reports_dir = tmp_path
    p.template = PLUGIN_DIR / "templates" / "report.typ"
    p.font_dir = tmp_path
    p.ppi = 72

    src = tmp_path / "shot_1.jpg"  # 文件名里带群号，命中路径
    PILImage.new("RGB", (1200, 900), "white").save(src, "JPEG")

    import typst

    monkeypatch.setattr(typst, "compile", lambda *a, **k: None)
    payload = {
        "title": "测试群 · 群聊情报",
        "date": "2026-11-10",
        "stats": {"line": "1 条消息"},
        "headline": "",
        "warn": "",
        "topics": [
            {
                "title": "话题",
                "parts": [{"text": "讲了点东西", "name": False}],
                "images": [{"path": str(src), "caption": "示例"}],
            }
        ],
        "footer": "",
    }
    p._render(payload, "1", "今天")
    images = payload["topics"][0]["images"]
    assert images, "图片被静默丢弃了"
    assert images[0]["path"].startswith("img/")


def test_render_writes_no_gid_into_the_typst_data(tmp_path, monkeypatch):
    """真正的关卡在渲染前：写进 data.json 的内容已经没有群号了。"""
    typst = pytest.importorskip("typst")
    from conftest import load_main

    mod = load_main()
    p, _ = make_plugin()
    p.reports_dir = tmp_path
    p.template = PLUGIN_DIR / "templates" / "report.typ"
    p.font_dir = tmp_path
    p.ppi = 72

    seen: dict = {}

    def fake_compile(*args, **kwargs):
        root = kwargs.get("root") or ""
        from pathlib import Path

        seen["data"] = (Path(root) / "data.json").read_text(encoding="utf-8")
        return None

    monkeypatch.setattr(typst, "compile", fake_compile)

    payload = {
        "title": "测试群 · 群聊情报",
        "date": "2026-11-10",
        "group": f"群号 {GID}",
        "stats": {"line": f"{GID}"},
        "headline": "",
        "warn": "",
        "topics": [],
        "footer": f"{GID}",
    }
    p._render(payload, GID, "今天")

    assert "data" in seen, "渲染没有走到写 data.json 这一步"
    assert GID not in seen["data"], seen["data"]
    assert "测试群" in seen["data"], "正文不该被一起抹掉"

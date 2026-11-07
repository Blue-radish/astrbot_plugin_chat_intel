import asyncio
import inspect

from PIL import Image

from conftest import make_plugin


def test_image_dims_stays_staticmethod(tmp_path):
    """_image_dims 必须是 staticmethod：调用处传的是文件路径，不是 self。"""
    p, mod = make_plugin()
    assert isinstance(
        inspect.getattr_static(mod.TechDigestPlugin, "_image_dims"), staticmethod
    ), "_image_dims 丢了 @staticmethod"
    path = tmp_path / "a.png"
    Image.new("RGB", (640, 480), "white").save(path)
    assert p._image_dims(path) == (640, 480)


def test_image_dims_on_missing_file_returns_zeros(tmp_path):
    p, _ = make_plugin()
    assert p._image_dims(tmp_path / "nope.png") == (0, 0)


def test_alert_missing_tags_is_bound_method_and_warns_once():
    """缺 want 的告警是普通方法，且同一个群只提醒一次。"""
    p, mod = make_plugin()
    assert not isinstance(
        inspect.getattr_static(mod.TechDigestPlugin, "_alert_missing_tags"), staticmethod
    ), "_alert_missing_tags 被 @staticmethod 包住了"
    pushed: list[str] = []

    async def fake_push(text: str) -> None:
        pushed.append(text)

    p._push_text = fake_push
    p._save_state = lambda: None
    p.state = {}

    asyncio.run(p._alert_missing_tags("某群", "123"))
    asyncio.run(p._alert_missing_tags("某群", "123"))
    assert len(pushed) == 1
    assert "关心的标签" in pushed[0]

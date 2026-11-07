import asyncio
import json

from conftest import make_plugin


class FakeEvent:
    """够用的假事件：只实现 _record / silence_events 会碰到的方法。"""

    def __init__(self, platform="bot_b", gid="1", text="显卡降价了", uid="a"):
        self._platform = platform
        self._gid = gid
        self._uid = uid
        self.message_str = text
        self.message_obj = None
        self.stopped = False

    def get_platform_id(self):
        return self._platform

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return "甲"

    def stop_event(self):
        self.stopped = True


def _plugin(tmp_path, silence=("bot_b",)):
    p, _ = make_plugin(store_dir=tmp_path)
    p.group_ids = ["1"]
    p.silence_platforms = list(silence)
    p._silenced = 0
    p._diag_count = 0
    return p


def _rows(tmp_path):
    path = next(tmp_path.glob("*.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_happens_even_when_platform_not_silenced(tmp_path):
    """采集与禁言解耦：未禁言的账号在群里收到消息也要落盘。"""
    p = _plugin(tmp_path)
    ev = FakeEvent(platform="bot_a")
    asyncio.run(p.silence_events(ev))
    assert len(_rows(tmp_path)) == 1
    assert ev.stopped is False


def test_silenced_platform_records_and_stops(tmp_path):
    p = _plugin(tmp_path)
    ev = FakeEvent(platform="bot_b")
    asyncio.run(p.silence_events(ev))
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert ev.stopped is True


def test_record_carries_platform_field(tmp_path):
    p = _plugin(tmp_path)
    asyncio.run(p.silence_events(FakeEvent(platform="bot_b")))
    assert _rows(tmp_path)[0]["platform"] == "bot_b"


def test_load_records_dedupes_two_bots(tmp_path):
    p = _plugin(tmp_path)
    rows = [
        {"t": 100, "gid": "1", "uid": "a", "name": "a", "text": "显卡降价了", "platform": "bot_b"},
        {"t": 100, "gid": "1", "uid": "a", "name": "a", "text": "显卡降价了", "platform": "bot_a"},
    ]
    (tmp_path / "2026-11-07.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    assert len(p._load_records("1", 0, 10**12)) == 1


def test_load_records_ignores_rows_without_gid(tmp_path):
    """没有群号的旧行不能算到"第一个群"头上（老版本的回退写法会串数据）。"""
    p = _plugin(tmp_path)
    rows = [
        {"t": 100, "uid": "a", "name": "a", "text": "旧版本写的行", "platform": ""},
        {"t": 100, "gid": "1", "uid": "b", "name": "b", "text": "本群的行"},
    ]
    (tmp_path / "2026-11-07.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    got = p._load_records("1", 0, 10**12)
    assert len(got) == 1
    assert got[0]["text"] == "本群的行"


def test_load_records_keeps_distinct_messages(tmp_path):
    p = _plugin(tmp_path)
    rows = [
        {"t": 100, "gid": "1", "uid": "a", "name": "a", "text": "第一条", "platform": "bot_b"},
        {"t": 101, "gid": "1", "uid": "a", "name": "a", "text": "第二条", "platform": "bot_b"},
        {"t": 100, "gid": "1", "uid": "b", "name": "b", "text": "第一条", "platform": "bot_b"},
    ]
    (tmp_path / "2026-11-07.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    assert len(p._load_records("1", 0, 10**12)) == 3

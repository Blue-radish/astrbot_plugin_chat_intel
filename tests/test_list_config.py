"""多值配置字段：面板的 list 控件给真列表，老配置给逗号串，两种都得认。

面板上「谁可以要报告」「要静音的账号」「不统计的 QQ 号」改成 list 控件后，
配置文件里已经是列表；但升级上来的老配置、以及手改 config 文件的人写的还是
「110001,110002」这种字符串。以前代码里 `str(值)` 再切分，遇上列表会切出
"['110001'" 这种垃圾——所以两种形态必须都过同一个解析口。
"""

from conftest import make_plugin

THREE_KEYS = {"owner_uids", "silence_platforms", "ignore_uids"}


# ---------------------------------------------------------------- 解析口
def test_split_accepts_real_list():
    _, mod = make_plugin()
    assert mod.split_cfg_list(["110001", "110002"]) == ["110001", "110002"]


def test_split_accepts_tuple_and_set():
    _, mod = make_plugin()
    assert mod.split_cfg_list(("a", "b")) == ["a", "b"]
    assert sorted(mod.split_cfg_list({"a", "b"})) == ["a", "b"]


def test_split_accepts_legacy_comma_string():
    _, mod = make_plugin()
    assert mod.split_cfg_list("110001,110002") == ["110001", "110002"]


def test_split_accepts_chinese_comma_dunhao_and_whitespace():
    _, mod = make_plugin()
    assert mod.split_cfg_list("110001，110002") == ["110001", "110002"]
    assert mod.split_cfg_list("110001、110002") == ["110001", "110002"]
    assert mod.split_cfg_list("110001 110002\n110003") == ["110001", "110002", "110003"]


def test_split_strips_drops_empties_and_dedupes_in_order():
    _, mod = make_plugin()
    assert mod.split_cfg_list(" 110001 , ,110002,110001 ") == ["110001", "110002"]


def test_split_handles_none_and_empty():
    _, mod = make_plugin()
    assert mod.split_cfg_list(None) == []
    assert mod.split_cfg_list("") == []
    assert mod.split_cfg_list("   ") == []
    assert mod.split_cfg_list([]) == []


def test_split_does_not_mangle_a_list_of_one():
    """回归：以前 str(list) 会切出 "['110001'" 这种脏值。"""
    _, mod = make_plugin()
    assert mod.split_cfg_list(["110001"]) == ["110001"]


# ---------------------------------------------------------------- 三个字段取值
def test_apply_multi_cfg_reads_list_values():
    p, _ = make_plugin()
    p._apply_multi_cfg(
        {
            "owner_uids": ["110001", "110002"],
            "silence_platforms": ["bot_a"],
            "ignore_uids": ["9"],
        }
    )
    assert p.owner_uids == {"110001", "110002"}
    assert p.silence_platforms == ["bot_a"]
    assert p.ignore_uids == {"9"}


def test_apply_multi_cfg_reads_legacy_string_values():
    p, _ = make_plugin()
    p._apply_multi_cfg(
        {
            "owner_uids": "110001, 110002",
            "silence_platforms": "bot_a，bot_b",
            "ignore_uids": "9、8",
        }
    )
    assert p.owner_uids == {"110001", "110002"}
    assert p.silence_platforms == ["bot_a", "bot_b"]
    assert p.ignore_uids == {"9", "8"}


def test_apply_multi_cfg_missing_keys_are_empty_not_crash():
    p, _ = make_plugin()
    p.owner_uids = {"stale"}
    p._apply_multi_cfg({})
    assert p.owner_uids == set()
    assert p.silence_platforms == []
    assert p.ignore_uids == set()


# ---------------------------------------------------------------- 老配置搬家
def test_migrate_turns_strings_into_lists():
    _, mod = make_plugin()
    cfg = {
        "owner_uids": "110001,110002",
        "silence_platforms": "bot_a,bot_b",
        "ignore_uids": "9",
    }
    changed = mod.TechDigestPlugin.migrate_list_fields(cfg)
    assert set(changed) == THREE_KEYS
    assert cfg["owner_uids"] == ["110001", "110002"]
    assert cfg["silence_platforms"] == ["bot_a", "bot_b"]
    assert cfg["ignore_uids"] == ["9"]


def test_migrate_is_idempotent():
    _, mod = make_plugin()
    cfg = {"owner_uids": "110001", "silence_platforms": "", "ignore_uids": "9"}
    mod.TechDigestPlugin.migrate_list_fields(cfg)
    assert mod.TechDigestPlugin.migrate_list_fields(cfg) == [], "第二次不该再动"


def test_migrate_leaves_lists_and_absent_keys_alone():
    _, mod = make_plugin()
    cfg = {"owner_uids": ["110001"]}
    assert mod.TechDigestPlugin.migrate_list_fields(cfg) == []
    assert cfg == {"owner_uids": ["110001"]}, "缺的键不该被凭空补出来"
    assert "silence_platforms" not in cfg


def test_migrate_turns_empty_string_into_empty_list():
    """空字符串不搬到 []，面板那一栏会显示成「有值但空」，看着像配置坏了。"""
    _, mod = make_plugin()
    cfg = {"owner_uids": ""}
    assert mod.TechDigestPlugin.migrate_list_fields(cfg) == ["owner_uids"]
    assert cfg["owner_uids"] == []


def test_migrated_config_yields_the_same_values():
    """搬家不能改变语义：迁前迁后取到的账号集合必须一致。"""
    p, mod = make_plugin()
    before = {"owner_uids": "110001, 110002", "silence_platforms": "bot_a，bot_b", "ignore_uids": "9,8"}
    after = dict(before)
    mod.TechDigestPlugin.migrate_list_fields(after)

    p._apply_multi_cfg(before)
    legacy = (set(p.owner_uids), list(p.silence_platforms), set(p.ignore_uids))
    p._apply_multi_cfg(after)
    migrated = (set(p.owner_uids), list(p.silence_platforms), set(p.ignore_uids))
    assert legacy == migrated

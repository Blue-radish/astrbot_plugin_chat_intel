from conftest import load_main


def test_plugin_name_is_chat_intel():
    assert load_main().PLUGIN_NAME == "astrbot_plugin_chat_intel"

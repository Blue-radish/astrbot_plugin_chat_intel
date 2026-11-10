"""判图提示词的行为约束。

3.3 这轮把「按尺寸排除表情包」的启发式从提示词里删掉了（尺寸硬过滤已经由
`_image_worth_keeping` 在代码里做），并把负向标签第一次写进提示词。
这是判图策略的变化，必须有测试盯着，否则下次改文案时没人知道判断依据变了。
"""

import asyncio

from conftest import load_main, make_plugin


def cand(i: int = 0) -> dict:
    return {
        "rec": {"t": 1700000000 + i, "name": f"群友{i}", "text": f"第{i}条"},
        "prev": "上文在这",
        "next": "下文在那",
        "files": [f"fake{i}.jpg"],
    }


class Capture:
    """记下模型收到的提示词，并回一个合法 picks。"""

    def __init__(self, reply: str = '{"picks": [0]}') -> None:
        self.prompts: list[str] = []
        self.reply = reply

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None, **kwargs):
        self.prompts.append(prompt or "")

        class _Resp:
            completion_text = ""

        r = _Resp()
        r.completion_text = self.reply
        return r


def ask(cands, tags_pos=(), tags_neg=(), reply='{"picks": [0]}'):
    p, mod = make_plugin()
    prov = Capture(reply)
    p._get_provider = lambda: prov
    p._image_dims = lambda path: (800, 600)
    chat = mod.ChatInterest("220001", "bot_a", tuple(tags_pos), tuple(tags_neg), "测试群")
    out = asyncio.run(p._ask_which_images(cands, chat))
    return prov.prompts[0], out


def test_negative_tags_reach_the_model():
    """负向标签以前根本不进提示词，现在必须进去。"""
    prompt, _ = ask([cand(0)], tags_pos=("AI 工具",), tags_neg=("手机开箱", "二手交易"))
    assert "负向标签：手机开箱、二手交易" in prompt, prompt
    assert "正向标签：AI 工具" in prompt, prompt


def test_labels_fall_back_when_not_configured():
    prompt, _ = ask([cand(0)])
    assert "正向标签：有信息量的内容" in prompt, prompt
    assert "负向标签：无" in prompt, prompt


def test_prompt_says_it_only_sees_text():
    """这条路径确实不给图，所以文案不能暗示模型「看图判断」。"""
    mod = load_main()
    assert "没有收到图片本身" in mod.IMAGE_PICK_PROMPT
    prompt, _ = ask([cand(0)])
    assert "没有收到图片本身" in prompt


def test_size_is_no_longer_a_rejection_rule_in_the_prompt():
    """尺寸硬过滤在 _image_worth_keeping 里做，提示词不该再重复（也不该据此断定内容）。"""
    mod = load_main()
    text = mod.IMAGE_PICK_PROMPT
    assert "300 像素" not in text, text
    assert "1:1" not in text, text
    assert "尺寸只能作为辅助线索" in text, text


def test_candidate_lines_carry_the_fields_the_prompt_promises():
    """提示词说每行有编号、发送者、时间、尺寸、本条、上下文——组装就得真给。"""
    prompt, _ = ask([cand(0), cand(1)])
    for token in ("#0", "发送者=群友0", "时间=", "尺寸=800x600", "本条=第0条", "上文=上文在这", "下文=下文在那"):
        assert token in prompt, f"{token} 没出现在提示词里：{prompt}"
    assert "#1" in prompt


def test_empty_picks_are_allowed():
    """全都不相关时模型可以给空数组，插件不该崩。"""
    prompt, out = ask([cand(0)], reply='{"picks": []}')
    assert prompt
    assert out == []


def test_invalid_json_returns_none():
    _, out = ask([cand(0)], reply="我看这几个都还行")
    assert out is None

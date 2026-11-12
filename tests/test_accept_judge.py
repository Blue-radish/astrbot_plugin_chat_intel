"""验收脚本的判定：不许把「读一份 + 顺手重跑一遍」判成通过。

判定错了比模型答错更危险——它会被当成「模型能力边界」放过。
这里用固定的假响应复现审查者指出的那个漏洞：第一轮「读报告 + 多余生成」，
第二轮补对了 ID，旧逻辑 `problems = []` 把误操作一起清掉了。

不用网络：判定逻辑在 tools/accept_judge.py，纯函数。
"""

import importlib.util
import pathlib
import sys

import conftest  # noqa: F401  # 负责把插件目录塞进 sys.path

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "accept_judge", PLUGIN_DIR / "tools" / "accept_judge.py"
)
accept_judge = importlib.util.module_from_spec(_spec)
sys.modules["accept_judge"] = accept_judge
_spec.loader.exec_module(accept_judge)


class Req:
    def __init__(self, gid="220001", label="今天", again=False, incremental=False):
        self.gid, self.label, self.again, self.incremental = gid, label, again, incremental


def call(name, **args):
    return {"name": name, "args": args}


def fake_execute(*_a, **_kw):
    """默认执行结果：读到了期望那份。"""
    return "（已读报告 A｜群｜今天）\n正文", {}


def judge(calls, expect, execute=fake_execute, allow_extra=()):
    return accept_judge.judge_round(calls, expect, set(allow_extra), execute)


def test_read_with_extra_generation_is_never_cleared_by_round_two():
    """审查者指出的漏洞：第一轮附带生成，第二轮补齐 ID，最终仍判失败。"""
    expect = {"tool": "get_group_report", "report_id": "A"}
    first = judge([call("get_group_report"), call("group_digest", when="今天")], expect)
    assert any("group_digest" in p for p in first[0]), first
    assert first[1], "多余生成属于不可恢复问题"

    def ok_execute(_call):
        return "（已读报告 A｜群｜今天）\n正文", {}

    second = judge([call("get_group_report", report_id="A")], expect, ok_execute)
    assert second[0] == [], "第二轮本身是对的"

    merged = accept_judge.merge_rounds(first, second)
    assert any("group_digest" in p for p in merged), f"误操作不能因为第二轮补参就消失：{merged}"


def test_missing_id_alone_is_recoverable():
    """只缺 ID：第二轮补上就算过。"""
    expect = {"tool": "get_group_report", "report_id": "A"}
    first = judge([call("get_group_report")], expect)
    assert first[1] == [], "缺 ID 不该记成不可恢复"

    def ok_execute(_call):
        return "（已读报告 A｜群｜今天）\n正文", {}

    second = judge([call("get_group_report", report_id="A")], expect, ok_execute)
    assert accept_judge.merge_rounds(first, second) == []


def test_wrong_tool_is_not_recoverable():
    """第一轮选错工具（该读却调了状态），第二轮改对也不算整条通过。"""
    expect = {"tool": "get_group_report", "report_id": "A"}
    first = judge([call("digest_status")], expect)
    assert first[0] and first[1]

    def ok_execute(_call):
        return "（已读报告 A｜群｜今天）\n正文", {}

    second = judge([call("get_group_report", report_id="A")], expect, ok_execute)
    merged = accept_judge.merge_rounds(first, second)
    assert any("没有调用 get_group_report" in p for p in merged), merged


def test_side_effect_on_a_no_action_case_still_fails():
    """「夸一句」的样本里只要动手（生成/重发/取消）就是错。"""
    problems, hard, _out = judge([call("group_digest", when="今天")], {})
    assert problems and "group_digest" in problems[0]
    assert hard


def test_side_effect_in_allow_extra_is_tolerated():
    """读报告是允许的附带动作（用户只是评价时，模型先读再答很合理）。"""
    problems, hard, _out = judge([call("get_group_report", report_id="A")], {}, allow_extra={"get_group_report"})
    assert problems == [] and hard == []


def test_params_are_verified_against_the_request():
    """参数要真核对：群、时间、again 对不上就是错，不是「调了就算」。"""
    expect = {"tool": "group_digest", "gid": "220001", "label": "今天", "again": True}

    def wrong(_call):
        return "受理结果=accepted", {"request": Req(gid="220002", label="昨天", again=False)}

    problems, _hard, _out = judge([call("group_digest", when="昨天", group="别的群")], expect, wrong)
    joined = "；".join(problems)
    assert "gid=220002" in joined and "label=昨天" in joined and "again=False" in joined, joined


def test_reading_the_wrong_report_fails_even_if_tool_name_matches():
    """工具名对、ID 错一样算错——读错一份会把讨论焦点换掉。"""
    expect = {"tool": "get_group_report", "report_id": "A"}

    def other(_call):
        return "（已读报告 B｜群｜今天）\n正文", {}

    problems, _hard, _out = judge([call("get_group_report", report_id="B")], expect, other)
    assert any("report_id=B" in p for p in problems), problems

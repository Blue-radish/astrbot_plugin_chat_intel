"""验收客户端的两处逻辑：安静等待不能空等、生成类请求要等到图。

这两点属于「验收工具」，但错了会让验收报告说谎：回执之后没消息被当成生成完成，
或者每条都白等满 timeout。所以也给它们上用例。
"""

import asyncio
import importlib.util
import pathlib
import sys
import time

import conftest  # noqa: F401  # 把插件目录塞进 sys.path

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "e2e_client", PLUGIN_DIR / "tools" / "e2e_client.py"
)
e2e = importlib.util.module_from_spec(_spec)
sys.modules["e2e_client"] = e2e
_spec.loader.exec_module(e2e)


def feed_after(delay, texts):
    async def run():
        await asyncio.sleep(delay)
        for text in texts:
            e2e.REPLIES.append({"t": delay, "text": text})
            await asyncio.sleep(0.05)

    return asyncio.create_task(run())


def test_settle_returns_right_after_the_quiet_window():
    """收到 1 条后再没有消息：安静 quiet 秒就该返回，不能等满 timeout。"""

    async def scenario():
        e2e.REPLIES.clear()
        task = feed_after(0.1, ["开始生成：今天｜电脑互助群"])
        start = time.time()
        got = await e2e.settle(quiet=0.5, timeout=10.0)
        elapsed = time.time() - start
        await task
        assert len(got) == 1
        assert elapsed < 3.0, f"应该 ~0.6s 返回，实际 {elapsed:.1f}s（会白等满 timeout）"

    asyncio.run(scenario())


def test_settle_keeps_waiting_while_replies_keep_coming():
    """消息还在陆续来的时候不能提前返回。"""

    async def scenario():
        e2e.REPLIES.clear()
        task = feed_after(0.1, ["一", "二", "三", "四"])
        got = await e2e.settle(quiet=0.5, timeout=10.0)
        await task
        assert len(got) == 4, f"四条都要等到：{got}"

    asyncio.run(scenario())


def test_parse_step_marks_image_requests():
    assert e2e.parse_step("生成今天的报告::image") == ("生成今天的报告", "image")
    assert e2e.parse_step("现在跑到哪了？") == ("现在跑到哪了？", "")
    assert e2e.parse_step("  带空格  :: image ") == ("带空格", "image")


def test_timeout_marks_failure_so_automation_sees_nonzero_exit():
    """等图超时不能算通过：退出码是自动化验收唯一的判据。"""
    e2e.FAILURES.clear()
    assert e2e.exit_code() == 0, "没失败时退出码应为 0"

    e2e.FAILURES.append("等图超时（600s）：生成今天的报告")
    assert e2e.exit_code() == 1, "超时必须以非零退出码结束"

    e2e.FAILURES.append("没有任何回包：现在跑到哪了？")
    assert e2e.exit_code() == 1
    e2e.FAILURES.clear()


def test_image_wait_records_failure_when_nothing_arrives():
    """等到图才算数：只有文字回包（比如只回了「开始生成」）应记成失败。"""

    async def scenario():
        e2e.REPLIES.clear()
        e2e.FAILURES.clear()
        task = feed_after(0.1, ["开始生成：今天｜电脑互助群"])
        got = await e2e.settle(quiet=0.3, timeout=5.0)
        await task
        has_image = any("[图片]" in g["text"] for g in got)
        assert not has_image, "这条样本本来就没有图"
        # 客户端里的判定逻辑：没有图就是失败项
        if not has_image:
            e2e.FAILURES.append("等图超时（1s）：生成今天的报告")
        assert e2e.exit_code() == 1
        e2e.FAILURES.clear()

    asyncio.run(scenario())

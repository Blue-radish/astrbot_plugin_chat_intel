"""验收用的假 QQ 客户端：连反向 WS，替主人发私聊消息，打印回包。

用途：不用真 QQ，也能跑「会话级」验收——AstrBot、插件、模型、报告生成全是真的，
只有 QQ 传输这一层被这个脚本替换。

前置：
  1. 临时加一个 aiocqhttp 反向 WS 平台（见 tools/e2e_platform.py，端口换成 6239）；
  2. 重启 AstrBot，让它开始监听那个端口。

用法：
    python tools/e2e_client.py --port 6239 --token <ws_reverse_token> --self-id 2000000001 \
        --owner 1679956404 --say "现在跑到哪了？" --say "生成今天电脑互助群的报告::image"
（`::image` 表示这条要等到图片：回执之后没声音不等于生成完成。）

注意：
  * 报告图是 base64 传的，客户端**必须**开 max_msg_size=0，否则大图会把连接挤断，
    看起来像「插件发送失败」，其实是假客户端太小气；
  * 每条消息按「安静 quiet 秒且期间没有新增」判断说完；计数分「本轮开始」和「上次观察」
    两个，否则收到第一条之后会一直刷新活动时间，白等满 timeout；
  * 退出码：全部期望满足才 0。等图超时、某条没有任何回包都记为失败并以 1 退出——
    自动化验收只会看退出码，不能让它把超时当通过。
"""

import argparse
import asyncio
import json
import time

import aiohttp

T0 = time.time()
REPLIES = []
FAILURES = []  # 验收失败项：有内容就以非零退出码结束，别让自动化把超时当通过
_seq = [0]


def log(prefix, text):
    print(f"[{time.time() - T0:6.1f}s] {prefix} {text}", flush=True)


def make_event(text, self_id, owner):
    _seq[0] += 1
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": self_id,
        "target_id": self_id,
        "user_id": owner,
        "time": int(time.time()),
        "message_id": 3000 + _seq[0],
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": owner, "nickname": "e2e", "card": ""},
    }


async def reader(ws):
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        if payload.get("action") in ("send_msg", "send_private_msg"):
            parts = []
            for seg in (payload.get("params") or {}).get("message") or []:
                kind = seg.get("type")
                if kind == "text":
                    parts.append(seg["data"].get("text", ""))
                elif kind == "image":
                    parts.append("[图片]")
                else:
                    parts.append(f"[{kind}]")
            line = " ".join(parts)
            REPLIES.append({"t": time.time() - T0, "text": line})
            log("→ 机器人:", line)
        await ws.send_json(
            {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": _seq[0] + 7000},
                "echo": payload.get("echo"),
            }
        )


def exit_code():
    """有失败项就返回 1：退出码是自动化唯一会看的东西。"""
    return 1 if FAILURES else 0


def parse_step(raw):
    """把 `--say` 的一项拆成 (文字, 期望)。

    现在只有 `image` 一种期望：这条要等到图。抽成函数是为了能单测——
    「等图」的判断错了，验收报告就会把「回执之后安静下来」当成生成完成。
    """
    text, _, want = raw.partition("::")
    return text.strip(), want.strip()


async def settle(quiet, timeout):
    """等到「安静 quiet 秒」为止，返回这期间收到的消息。

    计数要分开记：`start` 是本轮开始时的条数，`seen` 是上一次观察到的条数。
    只有**新增**回复才刷新活动时间——以前用 `len(REPLIES) > start` 判断，
    收到第一条之后就永远成立，每轮都刷新，等于必然白等满 timeout。
    """
    start = len(REPLIES)
    seen = start
    begin = time.time()
    last = begin
    while time.time() - begin < timeout:
        await asyncio.sleep(0.5)
        if len(REPLIES) > seen:
            seen = len(REPLIES)
            last = time.time()
        elif time.time() - last >= quiet:
            break
    return REPLIES[start:]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--self-id", type=int, required=True)
    ap.add_argument("--owner", type=int, required=True)
    ap.add_argument("--say", action="append", default=[])
    ap.add_argument("--quiet", type=float, default=8.0)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument(
        "--image-timeout",
        type=float,
        default=600.0,
        help="带 ::image 的消息等图上限（生成要几分钟，默认 10 分钟）",
    )
    args = ap.parse_args()

    headers = {"X-Self-ID": str(args.self_id), "X-Client-Role": "Universal"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(
            f"ws://127.0.0.1:{args.port}/ws", headers=headers, max_msg_size=0
        ) as ws:
            log("连接", f"ws://127.0.0.1:{args.port}/ws")
            task = asyncio.create_task(reader(ws))
            await asyncio.sleep(1.0)
            for raw in args.say:
                # 写成 "……::image" 表示这条要等到图（生成类请求用）
                text, want = parse_step(raw)
                log("← 我:", text + ("（等图）" if want == "image" else ""))
                await ws.send_json(make_event(text, args.self_id, args.owner))
                start = len(REPLIES)
                got = await settle(args.quiet, args.timeout)
                if want == "image":
                    # 回执之后没声音 ≠ 生成完成：图（或明确的失败回执）才是终态
                    deadline = time.time() + args.image_timeout
                    while (
                        not any("[图片]" in g["text"] for g in got)
                        and time.time() < deadline
                    ):
                        await asyncio.sleep(1.0)
                        got = REPLIES[start:]
                    if not any("[图片]" in g["text"] for g in got):
                        FAILURES.append(f"等图超时（{args.image_timeout:.0f}s）：{text}")
                        log("   !! 没等到图", f"{args.image_timeout:.0f}s 内没有图片")
                if not got:
                    FAILURES.append(f"没有任何回包：{text}")
                log(
                    "   小结",
                    f"收到 {len(got)} 条，图片={any('[图片]' in g['text'] for g in got)}",
                )
            task.cancel()
            await asyncio.sleep(0.3)
            print("\n=== 全部回包 ===")
            for r in REPLIES:
                print(f"  {r['t']:6.1f}s {r['text'][:120]}")
            if FAILURES:
                print(f"\n=== 失败 {len(FAILURES)} 项（退出码 1）===")
                for item in FAILURES:
                    print(f"  - {item}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    # 超时/空回包不算通过：脚本的退出码就是自动化验收的判据
    raise SystemExit(exit_code())

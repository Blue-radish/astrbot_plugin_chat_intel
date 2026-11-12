"""验收用的假 QQ 客户端：连反向 WS，替主人发私聊消息，打印回包。

用途：不用真 QQ，也能跑「会话级」验收——AstrBot、插件、模型、报告生成全是真的，
只有 QQ 传输这一层被这个脚本替换。

前置：
  1. 临时加一个 aiocqhttp 反向 WS 平台（见 tools/e2e_platform.py，端口换成 6239）；
  2. 重启 AstrBot，让它开始监听那个端口。

用法：
    python tools/e2e_client.py --port 6239 --token <ws_reverse_token> --self-id 2000000001 \
        --owner 1679956404 --say "现在跑到哪了？" --say "做得真好，这个群总结"

注意：
  * 报告图是 base64 传的，客户端**必须**开 max_msg_size=0，否则大图会把连接挤断，
    看起来像「插件发送失败」，其实是假客户端太小气；
  * 每条消息之间按「安静 quiet 秒」判断一轮说完，避免用固定 sleep 猜时序。
"""

import argparse
import asyncio
import json
import time

import aiohttp

T0 = time.time()
REPLIES = []
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


async def settle(quiet, timeout):
    start = len(REPLIES)
    begin = last = time.time()
    while time.time() - begin < timeout:
        await asyncio.sleep(0.5)
        if len(REPLIES) > start:
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
            for text in args.say:
                log("← 我:", text)
                await ws.send_json(make_event(text, args.self_id, args.owner))
                got = await settle(args.quiet, args.timeout)
                log("   小结", f"收到 {len(got)} 条，图片={any('[图片]' in g['text'] for g in got)}")
            task.cancel()
            await asyncio.sleep(0.3)
            print("\n=== 全部回包 ===")
            for r in REPLIES:
                print(f"  {r['t']:6.1f}s {r['text'][:120]}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

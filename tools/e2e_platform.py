"""给 AstrBot 临时加一个只用于验收的 aiocqhttp 反向 WS 端口，并支持撤销。

为什么不复用 6199：那是真 NapCat 的连接。同一个端口再插一个客户端，回包会串到真实 QQ 上。
这里用 6239 单独开一个「假 QQ 客户端」通道：插件、模型、报告生成全是真的，只有 QQ 传输是假的。
"""

import json
import pathlib
import shutil
import sys
import time

CFG = pathlib.Path(r"C:\Users\Ray\astrbot\data\cmd_config.json")
TEST_ID = "sutora_e2e"
TEST_PORT = 6239


def load():
    return json.loads(CFG.read_text(encoding="utf-8-sig"))


def save(cfg):
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def add():
    cfg = load()
    backup = CFG.with_suffix(f".json.bak-{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(CFG, backup)
    base = next(p for p in cfg["platform"] if p["id"] == "sutora")
    if any(p["id"] == TEST_ID for p in cfg["platform"]):
        print("测试平台已存在，跳过；备份", backup.name)
        return
    entry = json.loads(json.dumps(base))
    entry["id"] = TEST_ID
    entry["ws_reverse_port"] = TEST_PORT
    entry["enable"] = True
    cfg["platform"].append(entry)
    save(cfg)
    print(f"已加测试平台 {TEST_ID} -> 127.0.0.1:{TEST_PORT}；备份 {backup.name}")


def remove():
    cfg = load()
    before = len(cfg["platform"])
    cfg["platform"] = [p for p in cfg["platform"] if p.get("id") != TEST_ID]
    save(cfg)
    print(f"已移除测试平台（{before} -> {len(cfg['platform'])}）")


if __name__ == "__main__":
    {"add": add, "remove": remove}[sys.argv[1]]()

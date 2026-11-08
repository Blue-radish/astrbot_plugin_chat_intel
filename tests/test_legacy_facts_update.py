"""旧数据经过一次状态更新后，事实不能变。

升级前索引里只有 `status`（generated / delivered / unreadable）。读的时候 `_facts()`
会把它还原成「正文存下来了没 / 发出去了没」，但写的时候如果直接写新字段、不先还原，
就会把旧事实覆盖掉：
- 旧 unreadable → 发送图片成功 → 变成 delivered（正文其实读不回来）
- 旧 delivered → 重发失败 → 变成 generated（用户手里明明有一份）
"""

import reports

UMO = "bot_a:FriendMessage:110001"
GID = "220003"


def _state(status: str):
    return {
        "conversations": {
            UMO: {
                "reports": [
                    {
                        "id": "r1",
                        "gid": GID,
                        "group": "电脑互助群",
                        "label": "今天",
                        "ts": 1,
                        "status": status,
                        "topics": ["话题"],
                    }
                ]
            }
        }
    }


def _entry(state):
    return reports.entries(state, UMO)[0]


def test_legacy_unreadable_survives_a_successful_send():
    state = _state("unreadable")
    reports.mark_delivered(state, UMO, "r1")
    entry = _entry(state)
    assert entry["stored"] is False, "旧数据的「正文不可读」要写进新字段"
    assert entry["delivered"] is True
    assert reports.status_of(entry) == "unreadable"


def test_legacy_delivered_survives_a_failed_resend():
    state = _state("delivered")
    reports.mark_send_failed(state, UMO, "r1", reason="网络抖了")
    entry = _entry(state)
    assert entry["stored"] is True
    assert entry["delivered"] is True, "重发失败不该改掉历史"
    assert reports.status_of(entry) == "delivered"
    assert entry["send_error"] == "网络抖了"


def test_legacy_generated_stays_generated_after_failed_send():
    state = _state("generated")
    reports.mark_send_failed(state, UMO, "r1", reason="超时")
    assert reports.status_of(_entry(state)) == "generated"


def test_new_entries_keep_their_own_facts():
    state = {
        "conversations": {
            UMO: {
                "reports": [
                    {
                        "id": "r1",
                        "gid": GID,
                        "group": "群",
                        "label": "今天",
                        "ts": 1,
                        "phase": "ready",
                        "stored": False,
                        "delivered": False,
                        "topics": [],
                    }
                ]
            }
        }
    }
    reports.mark_delivered(state, UMO, "r1")
    assert reports.status_of(_entry(state)) == "unreadable"

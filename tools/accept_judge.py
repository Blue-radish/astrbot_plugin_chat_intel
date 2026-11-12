"""验收脚本的判定逻辑（纯函数，可被测试直接覆盖）。

抽出来的原因：判定错了比模型答错更麻烦——它会让人以为「只是模型能力边界」。
所以这里只放「拿一组 tool_calls 和期望去比对」的纯逻辑，不碰网络、不读配置。

两条不变式：
1. 期望为空（这句话不该动手）时，任何生成/重发/取消都算错，除非列在 allow_extra。
2. 副作用误操作**不可恢复**：第二轮补对 ID 只能消掉「身份参数错误」，
   第一轮多调的那次生成/重发/取消必须留着，否则「读一份顺便重跑一遍」会判成通过。
"""

SIDE_EFFECT = ("group_digest", "resend_report", "cancel_digest")
RECOVERABLE = "身份参数"  # 缺 ID / ID 写错：给一轮机会补


def extra_side_effects(calls, expect, allow_extra):
    """本轮多余的副作用调用（读之外还去生成/重发/取消）。"""
    hits = 0
    extras = []
    for call in calls:
        if expect and call["name"] == expect["tool"] and hits == 0:
            hits += 1  # 期望的那一次不算多余
            continue
        if call["name"] in SIDE_EFFECT and call["name"] not in allow_extra:
            extras.append(call["name"])
    return extras


def judge_round(calls, expect, allow_extra, execute):
    """比对一轮调用。

    返回 (problems, hard_problems, out)：
      - problems：本轮全部问题；
      - hard_problems：跨轮不可恢复的问题（多余副作用、工具选错），第二轮补参不能洗掉；
      - out：期望那一次的真实执行结果（没有期望或没调则空串）。
    """
    extras = extra_side_effects(calls, expect, allow_extra)
    hard = [f"不该动手却调了 {name}" for name in extras]
    if not expect:
        return hard, hard, ""

    hits = [c for c in calls if c["name"] == expect["tool"]]
    if not hits:
        msg = f"没有调用 {expect['tool']}（实际 {[c['name'] for c in calls] or '无'}）"
        return hard + [msg], hard + [msg], ""

    problems = list(hard)
    out, trace = execute(hits[0])
    problems.extend(check_args(hits[0], expect, out, trace))
    return problems, hard, out


def check_args(call, expect, out, trace):
    """参数与执行结果核对：身份、话题、生成范围都要对得上。"""
    problems = []
    args = call["args"]

    if "report_id" in expect:
        got = str(args.get("report_id") or "")
        if got != expect["report_id"]:
            problems.append(f"report_id={got or '(空)'} ≠ {expect['report_id']}")
        elif f"已读报告 {expect['report_id']}" not in out and "受理结果=sent" not in out:
            problems.append("后端没有真的读到/发出那一份")
        delivered = trace.get("delivered")
        if delivered and delivered[0] != f"{expect['report_id']}.png":
            problems.append(f"实际发出的是 {delivered[0]}")

    if "topic_no" in expect:
        got = int(args.get("topic_no") or 0)
        if got != expect["topic_no"]:
            problems.append(f"topic_no={got} ≠ {expect['topic_no']}")
        elif expect["topic_no"] > 1 and f"话题 {expect['topic_no']}" not in out:
            problems.append("结果里没有那个话题")

    if expect["tool"] == "group_digest":
        req = trace.get("request")
        if req is None:
            problems.append("没有真的提交请求")
        else:
            if "gid" in expect and req.gid != expect["gid"]:
                problems.append(f"gid={req.gid} ≠ {expect['gid']}")
            if "label" in expect and req.label != expect["label"]:
                problems.append(f"label={req.label} ≠ {expect['label']}")
            if "again" in expect and bool(req.again) != expect["again"]:
                problems.append(f"again={req.again} ≠ {expect['again']}")
            if "incremental" in expect and bool(req.incremental) != expect["incremental"]:
                problems.append(f"incremental={req.incremental} ≠ {expect['incremental']}")
    return problems


def is_recoverable(problems):
    """这些问题能不能靠「第二轮补一次参数」补救。"""
    if not problems:
        return True
    markers = ("report_id=", "topic_no=", "后端没有真的读到/发出那一份", "结果里没有那个话题")
    return all(any(m in p for m in markers) for p in problems)


def merge_rounds(first, second):
    """两轮结果合并：第一轮的误操作留着，只允许丢掉可恢复的身份参数错误。"""
    first_problems, first_hard, _out = first
    second_problems, second_hard, _out2 = second
    kept = list(first_hard) + list(second_hard)
    if not is_recoverable(first_problems) or not is_recoverable(second_problems):
        kept = list(first_problems) + list(second_problems) + kept
        return _dedupe(kept)
    return _dedupe(kept + list(second_problems))


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

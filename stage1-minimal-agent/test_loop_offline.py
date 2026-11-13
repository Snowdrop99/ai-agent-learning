"""离线校验 agent.py 的 loop 逻辑（不联网、不花钱）。

用"假模型"回放固定剧本：第 1 步要读文件，第 2 步要算数，第 3 步给答案。
跑法：python test_loop_offline.py

这也是 Stage 7「评测」的雏形：把模型固定成剧本，就能稳定复现 loop 的行为。
"""
import json

import agent


class Box:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def tool_call(cid, name, args: dict):
    return Box(id=cid, function=Box(name=name, arguments=json.dumps(args, ensure_ascii=False)))


def message(content=None, calls=None):
    m = Box(content=content, tool_calls=calls)
    m.model_dump = lambda **_: {
        "role": "assistant",
        "content": content,
        **({"tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]} if calls else {}),
    }
    return m


def scripted(*messages):
    """按顺序返回预设消息；用完后一直重复最后一条（用来测 max_steps）。"""
    queue = list(messages)

    def create(**kwargs):
        m = queue.pop(0) if len(queue) > 1 else queue[0]
        return Box(choices=[Box(message=m, finish_reason="tool_calls" if m.tool_calls else "stop")])

    agent.client = Box(chat=Box(completions=Box(create=create)))


def check(name, got, want_contains):
    ok = want_contains in got
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    assert ok, f"{name} 期望包含 {want_contains!r}"


# 场景 1：正常的 2 步工具链 → 最终答案
scripted(
    message(calls=[tool_call("c1", "read_file", {"path": "agent_loop.txt"})]),
    message(calls=[tool_call("c2", "calculator", {"expression": "6*3+10"})]),
    message(content="答案是 28。"),
)
check("两步工具链", agent.run_agent("q", verbose=False), "28")

# 场景 2：工具抛异常（文件不存在）→ 错误变成文本回灌，不崩
scripted(
    message(calls=[tool_call("c1", "read_file", {"path": "nope.txt"})]),
    message(content="文件不存在，我没法读。"),
)
check("工具失败兜底", agent.run_agent("q", verbose=False), "不存在")

# 场景 3：参数是坏 JSON → 不崩，返回 ERROR
scripted(
    message(calls=[Box(id="c1", function=Box(name="calculator", arguments="{坏json"))]),
    message(content="参数解析失败。"),
)
check("坏参数兜底", agent.run_agent("q", verbose=False), "解析失败")

# 场景 4：模型无限要工具 → 撞 max_steps 停下
scripted(message(calls=[tool_call("c1", "search", {"query": "工具"})]))
check("最大步数", agent.run_agent("q", max_steps=3, verbose=False), "达到最大步数")

# 场景 5：超时预算
scripted(message(calls=[tool_call("c1", "search", {"query": "工具"})]))
check("超时预算", agent.run_agent("q", timeout_s=-1, verbose=False), "超过")

print("\n全部通过：loop 的分支都覆盖到了。")

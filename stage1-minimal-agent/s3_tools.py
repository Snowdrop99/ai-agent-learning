"""Stage 1 · 任务 ③④：定义一个工具 + 解析模型的 tool call

跑法：python s3_tools.py

这个脚本故意**不自动执行**工具，而是把模型返回的原始结构打印出来。
你要亲眼看到这四件事，tool call 的"魔法"就没了：

  1. tools 参数 = 一份 JSON Schema 说明书，模型只能照着填，不能自己发明参数
  2. 模型返回的 finish_reason 是 "tool_calls"，content 通常是 None
  3. tool_calls[i].function.arguments 是**字符串**，不是 dict，必须自己 json.loads
  4. 每个 tool call 有唯一 id，回灌结果时必须带同一个 id
"""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

# --------- 初始化客户端 ----------
load_dotenv()
client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    timeout=30.0,
)
# --------- 模型选择 ----------
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# ---------- 工具 1：真正干活的函数 ----------
def calculator(expression: str) -> str:
    return f"{expression} = 42（这里是假的实现，只为演示解析流程）"


# ---------- 工具 2：给模型看的说明书（JSON Schema）----------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算一个数学表达式。当用户要求算数时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "合法的四则运算表达式，例如 (128*3+6)/2",
                    }
                },
                "required": ["expression"],
            },
        },
    }
]

if __name__ == "__main__":
    messages = [
        {"role": "system", "content": "你可以使用工具。需要算数时必须调用 calculator，不要心算。"},
        {"role": "user", "content": "帮我算 (128 * 3 + 6) / 2"},
    ]

    resp = client.chat.completions.create(
        model=MODEL, messages=messages, tools=TOOLS, temperature=0
    )
    msg = resp.choices[0].message

    print("finish_reason =", resp.choices[0].finish_reason)  # 关键信号
    print("content       =", msg.content)                   # 通常是 None
    print("tool_calls    =", len(msg.tool_calls or []), "个")
    print(json.dumps(msg.model_dump(exclude_none=True), ensure_ascii=False, indent=2))

    # ---------- 解析：把字符串变成可执行的调用 ----------
    for tc in msg.tool_calls or []:
        print("\n--- 解析一个 tool call ---")
        print("id       :", tc.id)                    # 回灌时必须原样带回
        print("name     :", tc.function.name)
        print("raw args :", repr(tc.function.arguments))  # 注意：这是字符串
        try:
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError as e:  # 模型偶尔会吐空串或坏 JSON
            print("解析失败，兜底成空 dict：", e)
            args = {}
        print("parsed   :", args)
        print("执行结果 :", calculator(**args))

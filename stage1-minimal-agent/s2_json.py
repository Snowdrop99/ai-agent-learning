"""Stage 1 · 任务 ②：让模型输出结构化 JSON

跑法：python s2_json.py

为什么不能只靠 prompt 说"请输出 JSON"：
  - 它可能套一层 ```json 代码块
  - 它可能编一个 schema 里没有的字段
  - 它可能把 confidence 写成 "high" 而不是数字
所以正确姿势是：给 schema + 开 json_object 模式 + **自己校验** + 不合格就带着错误重试。

这个脚本做的事：把一句自然语言需求解析成 {"tool": ..., "args": {...}, "confidence": ...}
——它其实就是 tool call 的手工版，理解了这个，下一节的 tool_calls 就没有魔法了。
"""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    timeout=30.0,
)
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

SYSTEM = """你是意图解析器。把用户的自然语言需求解析成 JSON，只输出 JSON，不要解释。
格式：
{"tool": "calculator" | "read_file" | "unknown",
 "args": {...},          // calculator 用 {"expression": "..."}；read_file 用 {"path": "..."}
 "confidence": 0.0-1.0   // 数字，不是文字
}
用户需求不涉及任何工具时，tool 填 "unknown"，args 填 {}。"""


def ask(prompt: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        response_format={"type": "json_object"},  # 关键：强制输出合法 JSON
        temperature=0,
    )
    return resp.choices[0].message.content


def validate(data: dict) -> None:
    """手写校验（等价于 pydantic 的活）。校验不过就 raise，由调用方重试。"""
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    for key in ("tool", "args", "confidence"):
        if key not in data:
            raise ValueError(f"缺少字段 {key}")
    if data["tool"] not in ("calculator", "read_file", "unknown"):
        raise ValueError(f"tool 取值非法: {data['tool']}")
    if not isinstance(data["args"], dict):
        raise ValueError("args 必须是对象")
    if not isinstance(data["confidence"], (int, float)) or not 0 <= data["confidence"] <= 1:
        raise ValueError("confidence 必须是 0-1 的数字")


def parse(text: str, retries: int = 1) -> dict:
    prompt = text
    last_err = None
    for attempt in range(retries + 1):
        raw = ask(prompt)
        try:
            data = json.loads(raw)
            validate(data)
            return data
        except Exception as e:  # JSON 解析失败 or 校验失败，都重试
            last_err = e
            print(f"  [第 {attempt + 1} 次不合格] {e} → 把错误回灌给模型重试")
            prompt = f"{text}\n\n你上次的输出不合法：{e}。请只输出符合 schema 的 JSON。"
    raise RuntimeError(f"结构化输出连续失败：{last_err}")


if __name__ == "__main__":
    for q in [
        "帮我算一下 (128 * 3 + 6) / 2",   # 正常
        "读一下 agent_loop.txt 这份笔记",    # 正常
        "今天天气怎么样",                   # 应该落到 unknown
    ]:
        print(f"\n输入：{q}")
        print("输出：", parse(q))

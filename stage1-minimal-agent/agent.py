"""最小 Agent Loop —— Stage 1 的最终产出（约 140 行）

    observe → think → act → observe → ... → answer

和 s1_chat.py 的普通对话相比，只多了三件事：
  1. 请求里带上 tools
  2. 模型要工具时，我们真的执行，并用 role="tool" 把结果塞回 messages
  3. 上面两步放进循环，直到模型不再要工具

跑法：
    python agent.py "读一下 agent_loop.txt，把里面推荐的 max_steps 乘 3 再加 10，算出结果"
    python agent.py "读一下 nope.txt"          # 看错误是怎么被兜住的
"""
import ast
import json
import operator
import os
import sys
import time
from pathlib import Path
import datetime

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
API_KEY = os.getenv("LLM_API_KEY")
# 没有 key 时不创建客户端（离线回放测试会把它换掉），有 key 才真正联网
client = OpenAI(
    api_key=API_KEY,
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    timeout=30.0,
) if API_KEY else None
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
SANDBOX = (Path(__file__).parent / "sandbox").resolve()

# ============ 一、工具实现：真函数，且必须有边界 ============
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
        ast.USub: operator.neg}


def calculator(expression: str) -> str:
    """不用 eval()：这里用 ast 只放行数字和四则运算，防止 ".__class__" 之类注入。"""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp):
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp):
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError(f"不支持的语法 {type(n).__name__}")
    return str(ev(ast.parse(expression, mode="eval")))


def read_file(path: str, max_chars: int = 1200) -> str:
    """只允许读 sandbox/ 下：工具给模型用，就等于把权限给它了，边界要在代码里卡死。"""
    p = (SANDBOX / path).resolve()
    if p != SANDBOX and SANDBOX not in p.parents:
        raise PermissionError(f"无权访问 sandbox 之外的文件: {path}")
    text = p.read_text(encoding="utf-8")
    return text[:max_chars] + ("\n...[已截断]" if len(text) > max_chars else "")


def search(query: str) -> str:
    """离线关键词检索：真实场景换成 Tavily/Bing，接口形状一样（返回文本片段）。"""
    hits = []
    
    for f in sorted(SANDBOX.glob("*.txt")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if query.lower() in line.lower():
                hits.append(f"{f.name}:{i}: {line.strip()}")
    return "\n".join(hits[:5]) or f"没有找到包含 “{query}” 的内容"

def get_time() -> str:
    """获取当前时间，格式化为 YYYY-MM-DD HH:MM:SS"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ============ 二、工具说明书（给模型看）+ 分发表（给自己用）============
TOOLS = [
    {"type": "function", "function": {
        "name": "calculator", "description": "计算数学表达式，例如 (128*3+6)/2",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "四则运算表达式"}},
            "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "读取 sandbox 目录下的文本文件",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件名，如 agent_loop.txt"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search", "description": "在 sandbox 文档里按关键词检索",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词"}},
            "required": ["query"]}}},
            # 备注：无参工具要显式声明空 schema，记住【schema 是契约，缺字段 = 把解释权交给对方的实现】
    {"type":"function","function":{
        "name":"get_time","description":"查询真实的当前日期时间。凡问题涉及'现在/今天/最近'等需要当前时间才能回答的情况必须调用本工具，不要凭记忆猜日期。",
        "parameters":{"type":"object","properties":{}}}}
]
IMPL = {"calculator": calculator, "read_file": read_file, "search": search, "get_time": get_time}

# ============ 三、执行器：任何失败都变成"模型能看懂的错误字符串" ============
def run_tool(name: str, raw_args: str) -> str:
    fn = IMPL.get(name)
    if fn is None:
        return f"ERROR: 未知工具 {name}，可用：{list(IMPL)}"
    try:
        args = json.loads(raw_args or "{}")
        return str(fn(**args))
    except Exception as e:  # 参数坏、文件不存在、除零……全部兜住
        return f"ERROR: {type(e).__name__}: {e}"

# ============ 四、agent loop：最大步数 + 超时 + 兜底 ============
SYSTEM = (
    "你是一个会用工具的助手。无论用户说什么都必须先用工具。需要算数就调 calculator,需要读资料就调 read_file 或 search。"
    "工具返回 ERROR 时，先解释原因再决定要不要换个做法，不要假装成功。"
    "拿到足够信息后直接用中文给出最终答案。"
)


def run_agent(question: str, max_steps: int = 6, timeout_s: float = 90, verbose: bool = True) -> str:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    t0 = time.time()
    if client is None:
        return "[停止] 缺少 LLM_API_KEY：把 .env.example 复制成 .env 并填入 key"

    for step in range(1, max_steps + 1):
        if time.time() - t0 > timeout_s:                      # 兜底 ①：总时长预算
            return f"[停止] 超过 {timeout_s}s 预算，已用 {time.time() - t0:.1f}s"
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, temperature=0, timeout=30.0
            )
        except Exception as e:                                 # 兜底 ②：网络/限流
            return f"[停止] 模型调用失败：{type(e).__name__}: {e}"

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))     # assistant 消息必须入历史

        if not msg.tool_calls:                                 # 出口：不再要工具 = 最终答案
            if verbose:
                print(f"[step {step}] 无 tool call，结束")
            return msg.content or "(模型返回空)"

        for tc in msg.tool_calls:                              # 执行所有工具调用
            result = run_tool(tc.function.name, tc.function.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if verbose:
                print(f"[step {step}] act  {tc.function.name}({tc.function.arguments})")
                print(f"[step {step}] obs  {result[:160]}{'...' if len(result) > 160 else ''}")

    return f"[停止] 达到最大步数 {max_steps} 仍无最终答案（可能任务拆得太细或工具在互相打转）"  # 兜底 ③


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or (
        "读一下 agent_loop.txt，把里面推荐的 max_steps 乘 3 再加 10，用计算器算出结果。"
    )
    print(f"user >> {question}\n")
    t0 = time.time()
    print("\nfinal >>", run_agent(question))
    print(f"\n总耗时 {time.time() - t0:.1f}s")

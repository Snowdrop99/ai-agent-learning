"""Stage 1 · 任务 ①：用一个 LLM API 完成普通对话

跑法：python s1_chat.py

这个文件只有 30 行，但要看清三件事：
  1. messages 是一个 list，每条是 {"role": ..., "content": ...}
  2. role 有三种：system（设定） / user（你说） / assistant（模型说）
  3. API 是**无状态**的——模型不记得上一轮。所谓"多轮对话"，
     其实是每次把完整历史重新发一遍。这一点决定了后面 agent loop 的写法。
"""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # 从同目录 .env 读 key

client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    timeout=30.0,  # 单次请求超时，别用默认的无限等
)
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

messages = [
    {"role": "system", "content": "你是简洁的中文技术助教，每次回答不超过 3 句话。"},
    {"role": "user", "content": "解释一览无遗的意思"}, # 用一句话说清：agent 和普通 chatbot 的差别在哪？
]

# --- 第一轮 ---
resp = client.chat.completions.create(model=MODEL, messages=messages, temperature=0)
reply = resp.choices[0].message.content
print("第 1 轮回答：", reply)
print("用量：", resp.usage)

# --- 第二轮：证明"记忆"是我们手动拼进 messages 的 ---
messages.append({"role": "assistant", "content": reply})
messages.append({"role": "user", "content": "那它最容易在哪一步翻车？"})

resp2 = client.chat.completions.create(model=MODEL, messages=messages, temperature=0)
print("\n第 2 轮回答：", resp2.choices[0].message.content)
print("\n发给模型的完整历史长度：", len(messages), "条消息")

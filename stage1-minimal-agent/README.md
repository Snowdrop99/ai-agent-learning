# Stage 1：Build A Minimal Agent Loop

对应 `Agent-Learning-Hub` 的 Stage 1 六个任务，每个任务落到一个可运行文件。

| 任务 | 文件 | 一句话 |
| --- | --- | --- |
| ① 用一个 LLM API 完成普通对话 | `s1_chat.py` | 看清 messages / role，以及"多轮 = 重发全部历史" |
| ② 让模型输出结构化 JSON | `s2_json.py` | json_object + 自己校验 + 带错误重试 |
| ③ 定义工具函数 | `s3_tools.py` | calculator 的 JSON Schema 写法 |
| ④ 解析 tool call | `s3_tools.py` | arguments 是字符串，必须 json.loads |
| ⑤ 执行工具并喂回模型 | `agent.py` | role="tool" + tool_call_id 对齐 |
| ⑥ 最大步数 / 超时 / 错误处理 | `agent.py` | 三个兜底，缺一个都会挂住或静默跑飞 |

## 跑起来

```bash
cd stage1-minimal-agent
pip install -r requirements.txt
cp .env.example .env      # 填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

python s1_chat.py                      # ①
python s2_json.py                      # ②
python s3_tools.py                     # ③④ 看原始 tool_calls 结构
python agent.py                        # ⑤⑥ 默认演示：读文件→算数→回答
python agent.py "读一下 nope.txt"        # 故意失败，看错误怎么被兜住
python test_loop_offline.py            # 不花钱验证 loop 的 5 个分支
```

## 目录

```
agent.py                # 产出：最小 agent（~140 行）
s1_chat.py s2_json.py s3_tools.py   # 分步教学脚本
test_loop_offline.py    # 假模型回放测试（Stage 7 评测的雏形）
sandbox/                # agent 只能读这里，权限边界写在代码里
```

## 为什么这么设计

- **provider 无关**：只认 `LLM_BASE_URL` + `LLM_MODEL`，换 DeepSeek / GLM / Qwen 只改三行。
- **工具带边界**：`read_file` 卡死在 `sandbox/` 内。工具交给模型 = 把权限交给模型，边界必须写在代码里，不能写在 prompt 里。
- **错误不抛出**：工具异常转成 `ERROR: ...` 字符串喂回模型，同时在 trace 里打印，模型能自纠、你也看得见。
- **双超时**：单次请求 `timeout=30`，整体 `timeout_s=90`。只有一个都会挂住。
- **步数上限**：`max_steps=6`。上限太大等于把"卡死"伪装成"很努力"。

# Stage 2 · 资料研究助手

Stage 2 的产出物：输入一个主题 → agent 自己检索、判断够不够、不够就换策略 → 输出带 `[S1]` 引用的答案，并且引用会被逐条校验。

基于 `../stage1-minimal-agent/agent.py` 的 agent loop 扩展而来，核心增量是三件事：**RAG 成为工具**、**三层记忆**、**失败与幻觉有明确处理策略**。

## 快速开始

```bash
cp ../stage1-minimal-agent/.env .       # 复用同一套 GLM key
python ingest.py                        # 抓 6 篇公开文档 → docs/ → 分块 → 向量索引
python s1_retrieval.py "长期记忆和会话记忆的区别"   # 先验收检索质量（不花 LLM 的钱）
python agent.py "agent 的记忆该怎么分层？" --verbose
python s2_failures.py                   # 失败路径回归测试，6 条断言
```

产物：`docs/*.md`（人工可查的正文）、`index.json`（向量库）、`sessions/*.md`（每次运行留档）、`memory.jsonl`（长期记忆）、`trace.jsonl`（每步动作）。

## 文件职责

| 文件 | 作用 | 对应 Stage 2 勾选项 |
| --- | --- | --- |
| `store.py` | 分块 → embedding → 余弦检索 | RAG 的 chunk/embed/retrieve |
| `web.py` | 抓取 + 正文抽取 + 代码残渣过滤 | 让"文件/网页"成为可用工具 |
| `ingest.py` | 摄取流水线，幂等可重跑 | 检索的数据侧 |
| `s1_retrieval.py` | 只验检索、不调模型的体检脚本 | 把 RAG 的锅和模型的锅分开 |
| `agent.py` | 5 个工具 + 三层记忆 + 引用校验 | 工具化、记忆、引用、失败处理 |
| `s2_failures.py` | 空结果/重复调用/幻觉引用/误报/压缩 | 失败处理的可回归验证 |

## 关键设计决定（读代码前先读这几条）

**1. 只有 `search_docs` 的返回能当证据。**
`fetch_page` 抓回来的原文明确告知模型"不可直接引用"。原因是引用必须能被机器校验——如果允许引用任意抓取文本，`[S1]` 就退化成装饰品。

**2. 引用 ID 是本轮全局递增，不是每轮从 S1 重排。**
否则第二轮的 `[S1]` 会和第一轮的 `[S1]` 撞车，答案里的编号指哪条全凭运气。

**3. 三层记忆的分工**

| 层 | 载体 | 生命周期 | 压缩时会怎样 |
| --- | --- | --- | --- |
| 短期上下文 | `messages` | 单次运行 | 旧原文被替换成占位符 |
| 会话记忆 | `Memory.evidence` | 单次运行，落盘 `sessions/` | **不受影响**，所以压缩后仍能写 `[S..]` |
| 长期记忆 | `memory.jsonl` | 跨运行 | 启动时按问题语义召回，注入 system |

第三层的向量和语料库共用同一个 embedding 模型，召回用余弦 + 0.35 阈值。

**4. 检索阈值是标定出来的，不是拍出来的。**
见 `NOTES.md`：相关题 top1 最低 0.500，无关题 top1 最高 0.391，取 0.45。换语料/换 embedding 模型必须重新标定。

**5. 失败一律翻译成模型能读懂的字符串。**
`ERROR: ...` 附带"下一步该试什么"，`EMPTY` 附带三种降级路径。异常直接抛给模型，它只会重复同一个错误调用。

## 想扩展时改哪里

- **换向量库**：只实现 `Store.search()` 的同签名方法，`agent.py` 一行不用改。
- **换 embedding**：改 `.env` 的 `EMBED_MODEL`/`EMBED_DIM`，然后 `python ingest.py --reset`（维度变了旧索引作废）。
- **加工具**：在 `Tools` 里写方法 → 在 `TOOLS` 里加 schema → 在 `SYSTEM` 里说明什么时候用。三步缺一不可。
- **加语料**：往 `ingest.py` 的 `SEEDS` 里加 URL，或在 `docs/` 放自己的 md 后写个小脚本调 `store.add_document`。
- **真实搜索**：`web_search` 现在解析 Bing HTML，换 Tavily/Serper 只需改函数体，返回格式保持"候选标题+URL"。

## 已知限制

- **正文抽取是正则，不是 readability**：`mcp-intro` 那页正文藏在 JS payload 里，过滤后只剩 2288 字符（原本 34898）。生产环境应换 `trafilatura`。
- **`openai.com` 会 403**：需要更完整的浏览器头或代理。
- **引用校验只查存在性，查不了"这条证据是否真的支撑这句话"**。前者可判定，后者需要 LLM 逐句比对（见 NOTES 的"下一步"）。
- **单轮研究，没有多轮追问**：多轮需要把 `sessions/` 读回来做上下文，属 Stage 3 的 session store。
- **没有并发**：多篇抓取是串行的，慢。

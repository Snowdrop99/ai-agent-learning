"""Stage 2 产出：资料研究助手（RAG + 工具 + 三层记忆 + 引用校验）

    python agent.py "agent 的记忆该怎么分层，长期记忆用什么存？"
    python agent.py "..." --verbose        # 打印每一步 think/act/obs
    python agent.py "..." --max-steps 8

和 stage1 的 agent.py 相比，多了四件事（对应 Stage 2 的四个勾选项）：
  1. 工具从"算数/读文件"升级成"检索/搜索/抓网页"——RAG 成为 agent 的一种工具
  2. 只有 search_docs 的返回进入证据表，回答必须写 [S1] 这种引用，事后逐条校验
  3. 记忆分三层：短期上下文（会压缩）/ 会话证据表（本轮不丢）/ 长期记忆（跨运行）
  4. 工具失败、空结果、重复调用都有明确的处理策略，而不是让它自己瞎猜
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from store import Store, search_texts
from web import fetch, strip_html

load_dotenv()
ROOT = Path(__file__).parent
MEMORY = ROOT / "memory.jsonl"       # 长期记忆：一行一条事实
SESSIONS = ROOT / "sessions"          # 会话记忆：每次运行留档，可复盘
TRACE = ROOT / "trace.jsonl"          # 观测：每个动作一行，事后能回答"它为什么这么干"

API_KEY = os.getenv("LLM_API_KEY")
client = OpenAI(api_key=API_KEY, base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
                timeout=60.0) if API_KEY else None
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) research-agent-learning"}


# ============================================================
# 一、三层记忆
# ============================================================
class Memory:
    """第 2 层：会话记忆（证据表 + 本轮事实），进程内、压缩不丢。
       第 3 层：长期记忆（memory.jsonl），跨运行、启动时按问题召回。"""

    def __init__(self, store: Store):
        self.store = store
        self.evidence: dict[str, dict] = {}   # S1 -> {title,url,text,score}
        self._n = 0
        self.longterm: list[dict] = []
        if MEMORY.exists():
            self.longterm = [json.loads(l) for l in MEMORY.read_text(encoding="utf-8").splitlines() if l.strip()]

    # ---- 证据表：引用 ID 的唯一来源 ----
    def add_evidence(self, hit: dict) -> str:
        self._n += 1
        sid = f"S{self._n}"
        self.evidence[sid] = {"title": hit["title"], "url": hit["url"],
                              "text": hit["text"], "score": hit.get("score", 0)}
        return sid

    def format_for_prompt(self) -> str:
        """证据表给模型看的紧凑版：只给 ID + 来源 + 正文，正文才是能抄的东西。"""
        if not self.evidence:
            return "（暂无证据）"
        return "\n\n".join(
            f"[{sid}] {e['title']}\nURL: {e['url']}\n{e['text'][:900]}"
            for sid, e in self.evidence.items()
        )

    # ---- 长期记忆 ----
    def recall(self, query: str, k: int = 3, min_score: float = 1.0) -> list[dict]:
        if not self.longterm:
            return []
        # BM25 词面匹配：搜不到同义改写，但记忆条目本来就少，够用且零成本
        hits = search_texts(query, [m["fact"] for m in self.longterm],
                            k=k, min_score=min_score)
        return [{**self.longterm[h["index"]], "score": h["score"]} for h in hits]

    def remember(self, fact: str) -> str:
        if any(fact.strip() == m["fact"] for m in self.longterm):   # 幂等：别把同一句话存十遍
            return "已存在，跳过"
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M"), "fact": fact}
        self.longterm.append(entry)
        with MEMORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return f"已写入长期记忆（当前 {len(self.longterm)} 条）"


# ============================================================
# 二、工具：检索 / 搜索 / 抓取 / 记忆
# ============================================================
class Tools:
    def __init__(self, store: Store, mem: Memory, verbose=False):
        self.store, self.mem, self.verbose = store, mem, verbose
        self.cache: dict[str, str] = {}     # 去重：同一调用第二次直接命中
        self.stats = {"calls": 0, "dup": 0, "err": 0, "empty": 0}

    # ---- 1. 本地语料向量检索（唯一的"可引用"来源）----
    def search_docs(self, query: str, k: int = 4) -> str:
        hits = self.store.search(query, k=k)
        if not hits and re.search(r"[\u4e00-\u9fff]", query):
            # BM25 只认词面：中文查询打英文语料必然空手。
            # 用聊天模型译一次英文再试——这一步的成本远低于 embedding 全库。
            try:
                tr = client.chat.completions.create(
                    model=MODEL, temperature=0,
                    messages=[{"role": "user", "content":
                               f"把这句话翻译成适合检索英文技术文档的英文关键词（只输出关键词，不超过15个词）：{query}"}])
                q2 = tr.choices[0].message.content.strip().strip('"')
                hits = self.store.search(q2, k=k)
                if hits:
                    self.translated = q2
            except Exception:
                pass  # 翻译失败就按原样返回 EMPTY
        if not hits:
            self.stats["empty"] += 1
            # 空结果不是异常，是一种"需要换策略"的信号，必须告诉模型怎么办
            return ("EMPTY: 本地语料没有相关片段。可用做法：① 换英文关键词重试一次 "
                    "② 用 web_search 找新来源，再 fetch_page 抓回来入库 ③ 直接承认资料不足")
        out = []
        note = f"（查询已自动译为英文重检: {self.translated}）" if getattr(self, "translated", None) else ""
        self.translated = None
        for h in hits:
            sid = self.mem.add_evidence(h)
            out.append(f"[{sid}] {h['title']} (score {h['score']})\n{h['text']}")
        return "\n\n".join(out) + ("\n" + note if note else "")

    # ---- 2. 联网搜索（会挂，挂了要有台阶下）----
    def web_search(self, query: str, k: int = 5) -> str:
        url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "count": k})
        try:
            html = fetch(url)
        except Exception as e:
            self.stats["err"] += 1
            return (f"ERROR: 搜索不可用（{type(e).__name__}）。不要重试同一个查询，"
                    "改用 search_docs 查本地语料，或用 fetch_page 直接打开你已知的官方文档 URL。")
        items = re.findall(
            r'<h2><a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a></h2>.*?(?:<p[^>]*>(.*?)</p>)?',
            html, re.S)
        lines, seen = [], set()
        for u, t, snip in items:
            if "bing.com" in u or u in seen:
                continue
            seen.add(u)
            lines.append(f"- {re.sub(r'<[^>]+>', '', unescape(t)).strip()}\n  {u}\n  "
                         f"{re.sub(r'<[^>]+>', '', unescape(snip or ''))[:160]}")
            if len(lines) >= k:
                break
        if not lines:
            self.stats["empty"] += 1
            return "EMPTY: 搜索没解析出结果。换英文/更短的关键词，或直接用 search_docs。"
        return "\n".join(lines) + "\n\n（以上是候选 URL，正文需用 fetch_page 抓取、再用 search_docs 取可引用片段）"

    # ---- 3. 抓网页并入库（抓取 ≠ 可引用）----
    def fetch_page(self, url: str) -> str:
        try:
            text = strip_html(fetch(url))
        except Exception as e:
            self.stats["err"] += 1
            return f"ERROR: 抓取失败（{type(e).__name__}: {e}）。换一个来源，不要反复抓同一个 URL。"
        if len(text) < 600:
            self.stats["err"] += 1
            return f"ERROR: 只抽到 {len(text)} 字符，疑似 JS 渲染或反爬。换个来源。"
        doc_id = "web-" + hashlib.md5(url.encode()).hexdigest()[:8]
        n = self.store.add_document(doc_id, url.split("/")[-1] or url, url, text)
        self.store.save()
        return (f"已抓取 {len(text)} 字符，切分 {n} 块入库（doc_id={doc_id}）。\n"
                f"提醒：抓回来的原文不能直接引用，必须再调 search_docs 拿到 [S] 片段。\n"
                f"开头预览：{text[:400]}...")

    def remember(self, fact: str) -> str:
        return self.mem.remember(fact)

    def recall(self, query: str) -> str:
        hits = self.mem.recall(query)
        if not hits:
            return "EMPTY: 长期记忆里没有相关内容。"
        return "\n".join(f"- ({h['ts']}, 相似度 {h['score']}) {h['fact']}" for h in hits)

    # ---- 统一执行口：失败一律变成模型能读懂的文字 ----
    def run(self, name: str, raw_args: str) -> str:
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_"):
            return f"ERROR: 未知工具 {name}"
        key = name + "|" + raw_args
        if key in self.cache:                      # 重复调用 → 命中缓存，别烧 token
            self.stats["dup"] += 1
            return (self.cache[key] +
                    "\n\n[提示] 你刚才用完全相同的参数调用过这个工具，这是缓存结果。"
                    "如果信息不够，请换关键词或换工具，而不是重复同一次调用。")
        self.stats["calls"] += 1
        try:
            out = str(fn(**json.loads(raw_args or "{}")))
        except TypeError as e:
            self.stats["err"] += 1
            out = f"ERROR: 参数不对（{e}）。检查必填字段和类型。"
        except Exception as e:
            self.stats["err"] += 1
            out = f"ERROR: {type(e).__name__}: {e}"
        self.cache[key] = out
        return out


TOOLS = [
    {"type": "function", "function": {
        "name": "search_docs",
        "description": "在本地已入库的文档里做语义检索，返回带 [S1] 编号的片段。这是唯一可用于引用的信息来源。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索语句，用陈述句比关键词效果好"},
            "k": {"type": "integer", "description": "返回片段数，默认 4"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "联网搜索，只返回候选标题和 URL。本地语料不够时用；失败会返回 ERROR，此时不要重试同一查询。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "抓取一个公开网页并自动入库。抓回的原文不可直接引用，之后必须用 search_docs 取片段。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "remember",
        "description": "把一条值得跨会话保留的事实/偏好写入长期记忆。只存稳定信息，不要存临时检索结果。",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string", "description": "一句话事实，脱离上下文也能读懂"}},
            "required": ["fact"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "按语义从长期记忆里召回相关事实（用户偏好、既往结论）。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
]


# ============================================================
# 三、引用校验：把"幻觉引用"从靠感觉变成可判定
# ============================================================
CITE = re.compile(r"\[(S\d+)\]")


def check_citations(answer: str, mem: Memory) -> dict:
    """两级校验里最便宜的一级：编号存在性。

    坑：纯文本匹配分不清"引用 [S1]"和"提到 [S1] 这个编号"。
    模型自我纠正时会写"上一条回复里的 [S1] 是我笔误"，直接触发误报。
    所以先把引号/反引号里的内容剔掉——校验器必须容忍"谈论自己"的文本。
    """
    cleaned = re.sub(r'`[^`]*`|"[^"]*"|“[^”]*”|「[^」]*」|『[^』]*』', "", answer)
    used = CITE.findall(cleaned)
    bad = sorted({u for u in used if u not in mem.evidence})
    unused = sorted(set(mem.evidence) - set(used))
    return {"used": sorted(set(used)), "invalid": bad, "unused": unused,
            "ok": bool(used) and not bad}


# ============================================================
# 四、上下文压缩：短期记忆会满，但证据不能丢
# ============================================================
def compact(messages: list[dict], budget: int | None = None) -> bool:
    """超预算就把旧一轮的 tool 结果换成占位符。
    注意：压缩掉的只是"重复的原文"，可引用证据在 Memory 里另存一份，
    所以压缩之后模型仍然能写 [S3]——这是三层记忆真正的好处。"""
    budget = budget or int(os.getenv("CONTEXT_BUDGET", "14000"))
    size = sum(len(str(m.get("content") or "")) for m in messages)
    if size <= budget:
        return False
    keep = messages[-4:]                     # 最近两轮完整保留
    for m in messages[:-4]:
        if m.get("role") == "tool" and len(str(m.get("content"))) > 200:
            m["content"] = "[已压缩：原文见会话证据表，仍可用 [S..] 引用]"
    return True


# ============================================================
# 五、主循环
# ============================================================
SYSTEM = """你是资料研究助手。回答必须建立在检索到的证据上，并按下面的规矩工作。

【硬性规则】
1. 凡涉及事实、数字、他人观点，必须在句末标注来源编号，如 [S3]；没有证据就写"资料未覆盖"。
2. 只能用 search_docs 返回过的编号。fetch_page 抓回的原文不算证据。
3. 工具返回 EMPTY 或 ERROR 时，先换策略（换关键词/换来源/承认不足），不要用同样参数重试。
4. 结论先写"直接答案"，再写"支撑证据"，最后写"证据不足之处"。
5. 用户说"记住/以后都这样"时，必须调用 remember 把稳定偏好写入长期记忆；
   反之不要主动往长期记忆里塞临时内容。
6. 回答开头如需用到长期记忆，先 recall 一次确认，不要凭印象。

【本次会话的已有证据】
{evidence}

【跨会话长期记忆（可能与本问题无关，仅作背景）】
{longterm}
"""


def run(question: str, max_steps: int = 10, verbose: bool = False, timeout_s: float = 180) -> dict:
    if client is None:
        return {"answer": "[停止] 缺少 LLM_API_KEY", "citations": {}, "stats": {}}

    store = Store()
    mem = Memory(store)
    tools = Tools(store, mem, verbose)
    t0 = time.time()
    trace_path = TRACE

    def trace(event, **kw):
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time() - t0, 2), "event": event, **kw},
                               ensure_ascii=False) + "\n")

    recalled = mem.recall(question)
    system = SYSTEM.format(
        evidence="（尚未检索，用 search_docs 取证据）",
        longterm="\n".join(f"- {m['fact']}" for m in recalled) or "（空）",
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    trace("start", question=question, recalled=[m["fact"] for m in recalled])

    for step in range(1, max_steps + 1):
        if time.time() - t0 > timeout_s:
            return {"answer": f"[停止] 超过 {timeout_s}s 预算", "citations": {}, "stats": tools.stats}

        # 压缩后把证据表刷新进 system，保证引用永远有效
        if compact(messages):
            messages[0]["content"] = system.format(
                evidence=mem.format_for_prompt(),
                longterm="\n".join(f"- {m['fact']}" for m in recalled) or "（空）")
            trace("compact", size=sum(len(str(m.get("content") or "")) for m in messages))

        try:
            resp = client.chat.completions.create(model=MODEL, messages=messages,
                                                  tools=TOOLS, temperature=0)
        except Exception as e:
            trace("llm_error", err=f"{type(e).__name__}: {e}")
            return {"answer": f"[停止] 模型调用失败：{type(e).__name__}: {e}",
                    "citations": {}, "stats": tools.stats}

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            answer = msg.content or ""
            cite = check_citations(answer, mem)
            trace("answer", invalid=cite["invalid"], used=cite["used"])

            # 幻觉引用 → 给一次修复机会（只修一次，避免无限重写）
            if cite["invalid"]:
                messages.append({"role": "user", "content":
                                 f"你引用了不存在的编号 {cite['invalid']}，本轮只有 "
                                 f"{list(mem.evidence)}。请只使用存在的编号重写答案；"
                                 "证据不足的部分直接写'资料未覆盖'。"
                                 "注意：重写时不要在正文里讨论或提及被你删掉的编号。"})
                fix = client.chat.completions.create(model=MODEL, messages=messages, temperature=0)
                answer = fix.choices[0].message.content or answer
                cite = check_citations(answer, mem)
                trace("answer_repaired", invalid=cite["invalid"])

            _save_session(question, answer, cite, mem, tools)
            return {"answer": answer, "citations": cite, "stats": tools.stats}

        for tc in msg.tool_calls:
            res = tools.run(tc.function.name, tc.function.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": res})
            trace("tool", name=tc.function.name, args=tc.function.arguments,
                  ok=not res.startswith(("ERROR", "EMPTY")), out_len=len(res))
            if verbose:
                print(f"[step {step}] {tc.function.name}({tc.function.arguments})")
                print(f"          → {res[:200].replace(chr(10), ' ')}")

    _save_session(question, "[达到最大步数]", {"used": [], "invalid": [], "unused": []}, mem, tools)
    return {"answer": f"[停止] {max_steps} 步内没收敛（可能检索反复空手或任务太发散）",
            "citations": {}, "stats": tools.stats}


def _save_session(question, answer, cite, mem: Memory, tools: Tools):
    """会话记忆落盘：下次遇到同类问题，人和 agent 都能翻回去看当时用了什么证据。"""
    SESSIONS.mkdir(exist_ok=True)
    p = SESSIONS / f"{time.strftime('%Y%m%d-%H%M%S')}.md"
    lines = [f"# {question}", "",
             f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}",
             f"- 工具调用：{tools.stats['calls']} 次，重复命中 {tools.stats['dup']} 次，"
             f"失败 {tools.stats['err']} 次，空结果 {tools.stats['empty']} 次",
             f"- 引用：{cite['used']}；无效：{cite['invalid'] or '无'}", "",
             "## 答案", answer, "", "## 证据清单"]
    for sid, e in mem.evidence.items():
        lines.append(f"- [{sid}] {e['title']} — {e['url']}")
    p.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    q = " ".join(a.question) or "agent 的记忆该怎么分层？长期记忆和会话记忆的区别是什么？"
    print(f"user >> {q}\n")
    t = time.time()
    r = run(q, max_steps=a.max_steps, verbose=a.verbose)
    print(r["answer"])
    print(f"\n--- 引用校验：使用 {r['citations'].get('used')} / 无效 {r['citations'].get('invalid') or '无'}")
    print(f"--- 工具统计：{r['stats']}")
    print(f"--- 耗时 {time.time() - t:.1f}s")

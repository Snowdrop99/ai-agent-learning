"""Stage 2 · RAG 的存储层：分块 → embedding → 余弦检索

刻意只用标准库 + openai 客户端，把每一步都摊开给你看：
  chunk_text  —— 文本怎么切成"可引用"的片段
  embed       —— 文本怎么变成向量
  cosine      —— 向量怎么比相似度
  Store       —— 怎么存、怎么增量加、怎么检索

想换成 Chroma / FAISS / pgvector，只要保持 search() 的输入输出不变，
agent.py 一行都不用改——这就是把 RAG 单独抽出来的原因。
"""
import json
import re
import math
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
INDEX = ROOT / "index.json"
EMBED_MODEL = os.getenv("EMBED_MODEL", "embedding-3")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))  # GLM embedding-3 支持降维，越小越省、越丢细节


# ============ 一、分块：RAG 里最容易搞砸的一步 ============
def chunk_text(text: str, size: int = 700, overlap: int = 120) -> list[dict]:

    """按段落累加到 size 字符，相邻块重叠 overlap 字符。

    为什么不用固定字数硬切？——会把句子拦腰砍断，片段自己都读不懂，
    后面 embedding 和 LLM 自然也读不懂。
    为什么要 overlap？——答案正好落在切口两侧时，两块都还留着一半上下文。
    返回里带 start/end，是为了引用时能定位回原文位置。

    先以段落为单位切，再按标点符号切，是为了避免"段落本身就超长"的情况。
    """
    # 先拆段落；段落本身超长（网页抽取常见）就再按句号拆成句原子。
    # 少了这一步，一个 5000 字的段落会被整块塞进一个 chunk，检索粒度全废。
    atoms = []
    for p in (p.strip() for p in text.split("\n\n")):
        if not p:
            continue
        if len(p) <= size:
            atoms.append(p)
            continue
        for s in re.split(r"(?<=[。！？.!?])\s*", p):
            s = s.strip()
            while len(s) > size:              # 连句号都没有的极端情况，硬切
                atoms.append(s[:size])
                s = s[size - overlap:]
            if s:
                atoms.append(s)

    chunks, buf, start = [], "", 0

    def flush(end: int):
        nonlocal buf, start
        if buf.strip():
            chunks.append({"text": buf.strip(), "start": start, "end": end})
        # 下一块从尾部 overlap 处开始，形成重叠。
        # 关键细节：overlap 要"对齐到词/句边界往前推"，否则块首会留下 "tion:" "ppings:"
        # 这种断词残片，既拉低 embedding 质量，引用出来也不能看。
        tail = buf[-overlap:] if overlap else ""
        if tail:
            m = re.search(r"[\s。，！？、,;.!?]", tail)
            if m:
                tail = tail[m.end():]
        buf = tail
        start = max(end - len(tail), 0)

    for p in atoms:
        if len(buf) + len(p) + 2 > size and buf:
            flush(start + len(buf))
        buf += ("\n\n" if buf else "") + p

    if buf.strip():
        chunks.append({"text": buf.strip(), "start": start, "end": start + len(buf)})
    return chunks


# ============ 二、embedding + 余弦 ============
def embed(client, texts: list[str], batch: int = 16) -> list[list[float]]:
    """分批调 embedding 接口。批处理是省钱的关键：一次请求 16 条，不是 16 次请求。"""
    out = []
    for i in range(0, len(texts), batch):
        resp = client.embeddings.create(
            model=EMBED_MODEL,
            input=texts[i:i + batch],
            dimensions=EMBED_DIM,
        )
        out.extend([d.embedding for d in resp.data])
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。向量已归一化，所以这里算的是点积的语义版本。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ============ 二·五、BM25：不花一分钱的检索引擎 ============
# 2026-11-06 起，embedding 配额耗尽（429/1113），检索切到 BM25。
# BM25 是词频检索的经典算法：谁在文档里出现得多、这个词在整个语料里多稀有，
# 分就越高。它不懂同义词（"记忆"搜不到"memory"），但零成本、零依赖、结果可解释。
# 切回向量检索：恢复 search() 里注释掉的 embed 分支即可（需要可用的 embedding API）。

_CJK_START, _CJK_END = "\u4e00", "\u9fff"  # 汉字区间，用于判断单字是否是中文


def tokenize(text: str) -> list[str]:
    """无分词库条件下的轻量分词：拉丁词 + 相邻汉字 bigram。

    中文没有空格可切，两字组合（"记忆"/"检索"）是粒度和噪声的折中，
    也是 jieba 之外最常见的土办法。"""
    text = text.lower()
    units = re.findall(rf"[a-z0-9]+|[\u4e00-\u9fff]", text)
    out, cjk_run = [], []

    def flush_cjk():
        if cjk_run:
            out.extend(a + b for a, b in zip(cjk_run, cjk_run[1:]))  # bigram
            out.extend(cjk_run)  # 单字也留一份，短查询命中靠它
            cjk_run.clear()

    for u in units:
        if len(u) == 1 and _CJK_START <= u <= _CJK_END:
            cjk_run.append(u)
        else:
            flush_cjk()
            out.append(u)
    flush_cjk()
    return out


class BM25:
    """Okapi BM25。k1 控词频饱和（出现 10 次不等价于 10 倍相关），b 控长文惩罚。"""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(docs_tokens)
        self.avgdl = sum(len(t) for t in docs_tokens) / max(self.N, 1)
        self.tfs = [Counter(t) for t in docs_tokens]
        self.dls = [len(t) for t in docs_tokens]
        self.df = Counter()
        for tf in self.tfs:
            self.df.update(tf.keys())

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log((self.N - df + 0.5) / (df + 0.5) + 1.0)

    def scores(self, query: str) -> list[float]:
        q = [t for t in tokenize(query) if self.df.get(t)]  # 语料里没有的词直接跳过
        out = []
        for i in range(self.N):
            tf, dl, s = self.tfs[i], self.dls[i], 0.0
            for term in set(q):
                f = tf.get(term, 0)
                if f:
                    s += self._idf(term) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out.append(round(s, 4))
        return out


def search_texts(query: str, texts: list[str], k: int = 3, min_score: float = 1.0) -> list[dict]:
    """对任意一组文本做 BM25 检索（长期记忆等小语料用它，免建大索引）。"""
    idx = BM25([tokenize(t) for t in texts])
    ss = idx.scores(query)
    ranked = sorted(zip(ss, range(len(texts))), reverse=True)[:k]
    return [{"score": s, "index": i} for s, i in ranked if s >= min_score]


# ============ 三、Store：一个 json 文件就是你的向量库 ============
class Store:
    def __init__(self, path: Path = INDEX):
        self.path = path
        self.data = {"embed_model": EMBED_MODEL, "dim": EMBED_DIM, "chunks": []}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        self._bm25 = None                       # 惰性构建的 BM25 索引缓存

    def save(self):
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False), encoding="utf-8"
        )

    def have_doc(self, doc_id: str) -> bool:
        # 增量入库的判据：按 doc 去重，避免重复摄取把检索结果刷成同一篇文章
        return any(c["doc_id"] == doc_id for c in self.data["chunks"])

    def add_document(self, doc_id: str, title: str, url: str, text: str) -> int:
        """入库：只分块，不再调 embedding（省钱；切回向量时在这里恢复 embed()）。"""
        if self.have_doc(doc_id):
            return 0
        pieces = chunk_text(text)
        for i, p in enumerate(pieces):
            self.data["chunks"].append({
                "id": f"{doc_id}#{i}",          # 引用 ID 的底座：doc + 序号
                "doc_id": doc_id,
                "title": title,
                "url": url,
                "seq": i,
                "text": p["text"],
                "start": p["start"],
                "end": p["end"],
            })
        self._bm25 = None                       # 语料变了，检索索引重建
        return len(pieces)

    def _index(self) -> BM25:
        """BM25 索引按需构建并缓存。217 块的构建在毫秒级，不必持久化。"""
        if self._bm25 is None:
            self._bm25 = BM25([tokenize(c["text"]) for c in self.data["chunks"]])
        return self._bm25

    def search(self, query: str, k: int = 5, min_score: float = 4.0,
               per_doc: int = 3) -> list[dict]:
        """返回 top-k 片段（BM25 原始分）。

        min_score ：宁可空手而归，也别塞不相关上下文。BM25 没有统一量纲，
                    阈值必须用无关问题标定（方法同向量时代：负样本定闸门）。
        per_doc   ：单篇最多贡献几块，防一篇长文占满 top-k。
        """
        if not self.data["chunks"]:
            return []
        ss = self._index().scores(query)
        ranked = sorted(zip(ss, range(len(ss))), reverse=True)
        hits, used = [], {}
        for s, i in ranked:
            if s < min_score or len(hits) >= k:
                break
            c = self.data["chunks"][i]
            if used.get(c["doc_id"], 0) >= per_doc:
                continue
            used[c["doc_id"]] = used.get(c["doc_id"], 0) + 1
            hits.append({**c, "score": round(s, 4)})
        return hits

    def stats(self) -> str:
        docs = {c["doc_id"] for c in self.data["chunks"]}
        chars = sum(len(c["text"]) for c in self.data["chunks"])
        return f"{len(docs)} 篇 / {len(self.data['chunks'])} 块 / {chars} 字符"


def get_client():
    """和 stage1 一样的客户端，只是这里要它同时提供 chat 和 embedding。"""
    from openai import OpenAI
    key = os.getenv("LLM_API_KEY")
    if not key:
        raise SystemExit("缺少 LLM_API_KEY，先复制 .env.example 成 .env")
    return OpenAI(api_key=key, base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
                  timeout=60.0)

"""摄取：抓公开网页 → 抽正文 → 存 docs/*.md → 分块入库

跑法：
    python ingest.py            # 抓 SEEDS 里还没入库的
    python ingest.py --reset    # 清空索引重来

为什么先落成 docs/*.md？——入库前得用眼睛确认"抽出来的是正文还是导航栏"。
RAG 效果差，一半的锅在抽取和分块，不在模型。
"""
import sys
from pathlib import Path

from store import Store, get_client
from web import fetch, strip_html

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"

# 主题集中在 Stage 2 关心的四件事：RAG、记忆、工具调用、评测/引用
SEEDS = [
    ("building-effective-agents", "Anthropic · Building Effective Agents",
     "https://www.anthropic.com/engineering/building-effective-agents"),
    ("contextual-retrieval", "Anthropic · Contextual Retrieval",
     "https://www.anthropic.com/engineering/contextual-retrieval"),
    ("llm-agents-weng", "Lilian Weng · LLM Powered Autonomous Agents（记忆/规划/工具）",
     "https://lilianweng.github.io/posts/2023-06-23-agent/"),
    ("mcp-intro", "Model Context Protocol · 官方入门",
     "https://modelcontextprotocol.io/docs/getting-started/intro"),
    ("openai-agents-guide", "OpenAI · A Practical Guide to Building Agents",
     "https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/"),
    ("rag-survey", "arXiv · Retrieval-Augmented Generation for LLMs: A Survey",
     "https://arxiv.org/abs/2312.10997"),
]

def main():
    client = get_client()
    DOCS.mkdir(exist_ok=True)
    store = Store()
    if "--reset" in sys.argv:
        store.data["chunks"] = []
        for f in DOCS.glob("*.md"):
            f.unlink()
        print("已清空索引与 docs/")

    for doc_id, title, url in SEEDS:
        if store.have_doc(doc_id):
            print(f"[skip] {doc_id} 已在索引里")
            continue
        try:
            text = strip_html(fetch(url))
        except Exception as e:
            print(f"[fail] {doc_id} {type(e).__name__}: {e}")
            continue
        if len(text) < 800:
            print(f"[weak] {doc_id} 只抽到 {len(text)} 字符，疑似整站 JS 渲染，跳过")
            continue
        # 落盘，方便人工看抽取质量；也方便你之后改用本地 PDF/笔记当语料
        md = DOCS / f"{doc_id}.md"
        md.write_text(f"# {title}\n\nsource: {url}\n\n{text}", encoding="utf-8")
        n = store.add_document(client, doc_id, title, url, text)
        print(f"[ok] {doc_id:24s} {len(text):6d} 字符 → {n:3d} 块")

    store.save()
    print("\n索引统计：", store.stats())


if __name__ == "__main__":
    main()

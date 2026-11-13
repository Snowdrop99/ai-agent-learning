"""M1 验收：只看检索，不看模型

跑法：
    python s1_retrieval.py "agent 的长期记忆和会话记忆怎么区分"
    python s1_retrieval.py "chunk 大小怎么选" --k 3
    python s1_retrieval.py "上下文压缩" --show 1

为什么必须先做这一步？
  检索错了，后面 LLM 再聪明也只能基于垃圾作答，而且它还会"说得很像真的"。
  把检索单独验收，是把 RAG 的锅和模型的锅分开——这是 Stage 7 评测思维的提前练习。
"""
import argparse

from store import Store

ap = argparse.ArgumentParser()
ap.add_argument("query")
ap.add_argument("--k", type=int, default=5)
ap.add_argument("--min", type=float, default=4.0, dest="min_score")
ap.add_argument("--show", type=int, default=2, help="每块最多显示几行正文")
args = ap.parse_args()

store = Store()
hits = store.search(args.query, k=args.k, min_score=args.min_score)

print(f"索引：{store.stats()}")
print(f"查询：{args.query!r}  (k={args.k}, min_score={args.min_score})\n")

if not hits:
    print("没有任何片段超过阈值。可能是：① 语料里真没有 ② 查询用词太口语 "
          "③ min_score 设太高。先试英文关键词，再考虑补语料。")
for i, h in enumerate(hits, 1):
    body = "\n".join(h["text"].splitlines()[:args.show])
    print(f"[S{i}] score={h['score']:.3f}  {h['title']}  #{h['seq']}")
    print(f"     {h['url']}")
    print(f"     {body[:300]}")
    print()

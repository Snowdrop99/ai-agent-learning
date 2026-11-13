"""M4 失败演练：四类失败各来一次，全部可复现（几乎不烧 token）

跑法：python s2_failures.py

为什么要写成测试而不是"手动试一次看看"？
  因为失败处理最容易被下一次改动悄悄改坏。Stage 7 讲回归测试，
  这里就是最小版本：把"应该发生什么"写成断言。
  2026-11-06：检索从向量切换到 BM25（embedding 配额耗尽），阈值语义从
  余弦(0~1)变为 BM25 原始分（语料相关），断言随之更新，并新增双语降级用例。
"""
from store import Store, search_texts
from agent import Tools, Memory, check_citations, compact


class FakeMem:
    evidence = {}


def hr(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


store = Store()
mem = Memory(store)
tools = Tools(store, mem)

hr("1. 空结果：问题超出语料范围（且翻译后仍无命中）")
out = tools.search_docs("量子纠缠如何影响白酒板块估值")
print(out[:220])
assert out.startswith("EMPTY"), "空结果必须显式告诉模型换策略，而不是返回空字符串"
print("✓ 返回 EMPTY + 三种替代策略")

hr("2. 阈值拦截：无关问题必须得 0 分、进不了结果")
hits = store.search("量子纠缠如何影响白酒板块估值", k=5)
print(f"无关查询 → {len(hits)} 条")
assert hits == []
print("✓ BM25 词面不匹配 = 0 分，闸门生效（不是永远塞满 k 条）")

hr("3. 重复调用：同参数第二次命中缓存")
a = tools.run("search_docs", '{"query": "agent memory"}')
b = tools.run("search_docs", '{"query": "agent memory"}')
print("第一次：", a[:80].replace("\n", " "))
print("第二次：", b[-150:].replace("\n", " "))
assert "缓存结果" in b and tools.stats["dup"] == 1
print(f"✓ 缓存命中 {tools.stats['dup']} 次，提示模型换关键词而不是原地打转")

hr("4. 幻觉引用：编号不存在必须被抓出来")
r = check_citations("根据资料，答案是 42 [S1]，另见 [S7]。", FakeMem())
print(r)
assert r["invalid"] == ["S1", "S7"] and not r["ok"]
print("✓ 两个不存在的编号都被判为 invalid")

hr("5. 校验器误报：模型在正文里『谈论』引用编号")
r = check_citations('上一条回复里的 "[S1]" 是我笔误，本轮无可用编号。', FakeMem())
print(r)
assert r["invalid"] == [], "引号内的元讨论不应算作引用"
print("✓ 引号内编号被剔除，不再误报")

hr("6. 上下文压缩：截断旧原文，但保留结构")
msgs = [{"role": "system", "content": "s" * 200}]
for i in range(4):
    msgs.append({"role": "tool", "content": f"检索结果{i} " + "x" * 4000})
    msgs.append({"role": "assistant", "content": f"思考{i}"})
before = sum(len(m["content"]) for m in msgs)
hit = compact(msgs, budget=3000)
after = sum(len(m["content"]) for m in msgs)
print(f"压缩触发={hit}  {before} → {after} 字符")
assert hit and after < before
print("✓ 超预算会压缩；可引用证据仍在 Memory.evidence 里，压缩后照样能写 [S..]")

hr("7. 中文查询自动译英重检（BM25 跨语言补丁）")
out = tools.search_docs("如何降低检索失败率")
print(out[:260].replace("\n", " "))
assert not out.startswith("EMPTY"), "纯中文查询应经翻译后命中英文语料"
print("✓ 中文查询经一次翻译后命中英文语料")

hr("全部通过")
print("工具统计：", tools.stats)

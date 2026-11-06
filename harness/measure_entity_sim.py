"""测量：现有实体名的 embedding 余弦分布，判断 0.92 / 0.85 是否适用。

ADR 的 0.92 / 0.85 是拍的初值。016 明确担心：「中文实体名的余弦分布未必适用，
盲目接线可能比现在的 LLM 单级更容易误并」。

所以接线前先量。关注已知答案的对照组：
  应合并：豆包 / 豆包AI、微信 / WeChat
  不该合并：张三 / 张三丰
"""

from __future__ import annotations

import math

from neo4j import GraphDatabase

from memory.config import load_settings
from memory.services.embedding import embed_texts

#: embedding 服务的单批上限（实测 413: batch size 6 > maximum allowed 4）
EMBED_BATCH = 4


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def embed_all(texts: list[str], settings) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(embed_texts(texts[i : i + EMBED_BATCH], settings))
    return out


def main() -> None:
    s = load_settings()
    ns = "work"  # MEMORY_NAMESPACE 没设时默认空，这里显式指定
    drv = GraphDatabase.driver(s.NEO4J_URI, auth=(s.NEO4J_USER, s.NEO4J_PASSWORD))

    with drv.session() as sess:
        rows = sess.run(
            "MATCH (e:Entity) WHERE e.namespace = $ns "
            "RETURN e.name AS name ORDER BY name",
            ns=ns,
        ).data()
    drv.close()

    names = [r["name"] for r in rows if r.get("name")]
    print(f"namespace={ns}  库里实体数 = {len(names)}")
    print(f"  {names}")
    print()

    controls = ["豆包", "豆包AI", "张三", "张三丰", "微信", "WeChat"]
    pool = list(dict.fromkeys(names + controls))
    print(f"含对照组共 {len(pool)} 个名字参与测量")
    print()

    vecs = embed_all(pool, s)
    arr = dict(zip(pool, vecs))

    pairs = []
    for i, a in enumerate(pool):
        for b in pool[i + 1 :]:
            pairs.append((cosine(arr[a], arr[b]), a, b))
    pairs.sort(reverse=True)

    print("=== 全部对的余弦（降序，只列 >= 0.5）===")
    shown = 0
    for sim, a, b in pairs:
        if sim < 0.5:
            continue
        shown += 1
        flag = ""
        if {a, b} == {"豆包", "豆包AI"}:
            flag = "  <== 应合并"
        elif {a, b} == {"张三", "张三丰"}:
            flag = "  <== 不该合并"
        elif {a, b} == {"微信", "WeChat"}:
            flag = "  <== 应合并(跨语言)"
        if sim >= 0.92:
            zone = "②直接采用"
        elif sim >= 0.85:
            zone = "灰区→LLM"
        else:
            zone = "新实体  "
        print(f"  {sim:.4f}  {zone}  {a} / {b}{flag}")
    if shown == 0:
        print("  （没有任何对 >= 0.5）")

    sims = sorted(p[0] for p in pairs)
    n = len(sims)
    print()
    print("=== 分布 ===")
    print(f"  min={sims[0]:.4f}  中位={sims[n//2]:.4f}  max={sims[-1]:.4f}")
    for t in (0.80, 0.85, 0.88, 0.90, 0.92, 0.95):
        c = sum(1 for x in sims if x >= t)
        print(f"  >= {t}: {c} 对 ({c/n*100:.1f}%)")

    print()
    print("=== 关键判定（这三组决定阈值能不能直接用）===")
    doubao = cosine(arr["豆包"], arr["豆包AI"])
    zhangsan = cosine(arr["张三"], arr["张三丰"])
    wechat = cosine(arr["微信"], arr["WeChat"])
    print(f"  豆包 / 豆包AI   = {doubao:.4f}   期望：>= 阈值（应合并）")
    print(f"  张三 / 张三丰   = {zhangsan:.4f}   期望：< 阈值（不该合并）")
    print(f"  微信 / WeChat   = {wechat:.4f}   期望：>= 阈值（应合并，跨语言）")
    print()
    problems = []
    if zhangsan >= 0.85:
        problems.append(
            f"张三/张三丰 = {zhangsan:.4f} 在灰区或之上 —— 单靠向量会把两人混成一个"
        )
    if doubao < 0.92:
        problems.append(
            f"豆包/豆包AI = {doubao:.4f} < 0.92 —— 单靠向量合并不了"
        )
    if wechat < 0.92:
        problems.append(
            f"微信/WeChat = {wechat:.4f} < 0.92 —— 跨语言别名需要别名表，向量救不了"
        )
    if problems:
        print("⚠️ 发现的问题：")
        for p in problems:
            print(f"   - {p}")
    else:
        print("✓ 三组都符合期望")


if __name__ == "__main__":
    main()

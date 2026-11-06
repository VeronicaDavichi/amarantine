"""真库验证：规则层在真实实体名上的判定。

只读，不改任何数据。目的是看规则层在**真实数据**上的表现，
而不是只在构造的测试用例上通过。

同时对照「如果按向量 0.92 会怎样」，量化改动的收益。
"""

from __future__ import annotations

import math

from neo4j import GraphDatabase

from memory.config import load_settings
from memory.services.embedding import embed_texts
from memory.services.entity_match import RULING_SAME, RULING_UNKNOWN, match_entities

EMBED_BATCH = 4


def embed_all(texts, settings):
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(embed_texts(texts[i : i + EMBED_BATCH], settings))
    return out


def cosine(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def main() -> None:
    s = load_settings()

    # 把所有 namespace 的实体名都拿出来（含历史测试残留，正好当真实样本）
    drv = GraphDatabase.driver(s.NEO4J_URI, auth=(s.NEO4J_USER, s.NEO4J_PASSWORD))
    with drv.session() as sess:
        rows = sess.run(
            "MATCH (e:Entity) RETURN e.namespace AS ns, e.name AS name "
            "ORDER BY ns, name"
        ).data()
    drv.close()

    by_ns: dict[str, list[str]] = {}
    for r in rows:
        if r.get("name"):
            by_ns.setdefault(r.get("ns") or "(none)", []).append(r["name"])

    print("=== 真实图数据 ===")
    for ns, names in sorted(by_ns.items()):
        uniq = sorted(set(names))
        print(f"  {ns}: {len(uniq)} 个实体 {uniq[:8]}{'...' if len(uniq) > 8 else ''}")
    print()

    # 对每个 namespace 内的实体名两两跑规则层
    total_pairs = 0
    same_hits: list[tuple[str, str, str]] = []
    differences = 0
    unknowns = 0

    for ns, names in sorted(by_ns.items()):
        uniq = sorted(set(names))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1 :]:
                total_pairs += 1
                r = match_entities(a, b, settings=s)
                if r.ruling == RULING_SAME:
                    same_hits.append((a, b, r.rule))
                elif r.ruling == RULING_UNKNOWN:
                    unknowns += 1
                else:
                    differences += 1

    print(f"=== 规则层在 {total_pairs} 对真实实体名上的结果 ===")
    print(f"  判同一  : {len(same_hits)}")
    print(f"  判不同  : {differences}")
    print(f"  判不了  : {unknowns}  (交给 LLM)")
    print()
    if same_hits:
        print("  判为同一的对：")
        for a, b, rule in same_hits:
            print(f"    [{rule}] {a} / {b}")
    else:
        print("  （真实数据里没有规则能判为同一的对）")

    # 对照：如果按向量 0.92 会怎样
    print()
    print("=== 对照：若按向量 ENTITY_ALIAS_SIM=0.92 ===")
    all_names = sorted({n for names in by_ns.values() for n in names})
    all_names = all_names[:20]  # 控制请求量
    if len(all_names) >= 2:
        vecs = dict(zip(all_names, embed_all(all_names, s)))
        vec_same = []
        for i, a in enumerate(all_names):
            for b in all_names[i + 1 :]:
                sim = cosine(vecs[a], vecs[b])
                if sim >= s.ENTITY_ALIAS_SIM:
                    vec_same.append((sim, a, b))
        print(f"  参与对照的名字: {len(all_names)} 个")
        print(f"  向量 >= {s.ENTITY_ALIAS_SIM} 判同一: {len(vec_same)} 对")
        for sim, a, b in sorted(vec_same, reverse=True)[:6]:
            print(f"    {sim:.4f}  {a} / {b}")
        # 用规则层的裁定去检验向量判的对不对
        wrong = 0
        for sim, a, b in vec_same:
            r = match_entities(a, b, settings=s)
            if r.ruling != RULING_SAME and r.rule == "single_char_prefix":
                wrong += 1
        if wrong:
            print(f"  ⚠️ 其中 {wrong} 对会被规则层判为**不同实体**（向量误并）")


if __name__ == "__main__":
    main()

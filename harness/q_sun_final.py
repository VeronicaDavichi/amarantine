from neo4j import GraphDatabase

from memory.config import load_settings
from memory.services.recall import recall

s = load_settings()
NS = "njzq-dingtalk"

d = GraphDatabase.driver(s.NEO4J_URI, auth=(s.NEO4J_USER, s.NEO4J_PASSWORD))
try:
    with d.session() as x:
        print("=" * 70)
        print("现在图里有谁")
        print("=" * 70)
        rr = x.run(
            "MATCH (n:Person) WHERE n.namespace = $ns "
            "RETURN n.name AS name, size([(n)--() | 1]) AS deg ORDER BY deg DESC",
            ns=NS,
        ).data()
        for r in rr:
            flag = "  <== 补上了" if r["name"] == "陈柏林" else ""
            print(f"  {r['name']:12} 度 {r['deg']}{flag}")

        print()
        print("=" * 70)
        print("陈柏林的节点和边")
        print("=" * 70)
        node = x.run(
            "MATCH (n:Person {name: '陈柏林', namespace: $ns}) "
            "RETURN labels(n) AS lbs, size([(n)--() | 1]) AS deg",
            ns=NS,
        ).data()
        print(f"  节点: {node if node else '（还是没有）'}")

        edges = x.run(
            "MATCH (n:Person {name: '陈柏林', namespace: $ns})-[r]-(m) "
            "RETURN type(r) AS rel, labels(m)[0] AS lb, "
            "coalesce(m.summary, m.name) AS txt LIMIT 12",
            ns=NS,
        ).data()
        for e in edges:
            print(f"  -[{e['rel']}]- {e['lb']}: {str(e['txt'])[:56]}")

        print()
        print("=" * 70)
        print("陈柏林 和 王洁 有没有共同事件")
        print("=" * 70)
        both = x.run(
            "MATCH (a:Person {name: '陈柏林', namespace: $ns}) "
            "MATCH (b:Person {name: '王洁', namespace: $ns}) "
            "MATCH (e:Event {namespace: $ns}) "
            "WHERE (e)-[]-(a) AND (e)-[]-(b) "
            "RETURN e.summary AS sm",
            ns=NS,
        ).data()
        for r in both:
            print(f"  · {r['sm']}")
        print(f"  -> {len(both)} 条")
finally:
    d.close()

print()
print("=" * 70)
print("召回验证")
print("=" * 70)
for q in ("陈柏林问过什么", "分支机构怎么访问反洗钱系统", "陈柏林"):
    r = recall(q, namespace=NS, settings=s)
    print(f"  Q: {q}  ->  {len(r.materials)} 条")
    for m in r.materials[:2]:
        hit = " ★" if "陈柏林" in str(m.get("summary") or "") else ""
        print(f"      [{m.get('score', 0):.3f}] {str(m.get('summary'))[:56]}{hit}")
